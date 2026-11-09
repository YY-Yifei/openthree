"""A2A 频道：把 openthree 接到 A2A 网络上 —— 别的智能体能给它派任务。

判据与微信 / 钉钉完全同构：频道只负责**收**与**发**。收 = `POST /a2a` 进来一条
JSON-RPC，变成 `InboundMessage` 交给桥；发 = 桥跑完 `Runtime.run()` 把答案交回
`send_text()`，写进那个 task 的 artifact。内核（agent / runtime / llm / prompt）
一行都不用改 —— 一旦发现必须改它们，就说明选错了层。

为什么必须自己跑一个 HTTP 服务：ADP 规定 **registry 从不发 A2A 请求**（它只抓
`/.well-known/agent-card.json` 来发现我们），而 `POST /v1/agents` 交的只是一个
URL。想「让面板替我们收任务」是不成立的。

本文件里三处判据最容易在重写时丢掉：

1. **端口的两种互斥模式**（`_bind`）：人给了端口就只听那一个，被占**报错**；
   没给才按 `last_port` → 9000–9999 依次 bind。写成「候选序列 + 降级」等于把
   「我指定了 9000」变成一句谎话。
2. **`allow_reuse_address` 必须按平台分叉**（`_Server`）：Windows 的
   `SO_REUSEADDR` 允许抢占一个正在被监听的端口，自动探测会招招"成功"。
3. **答案必须进 `artifacts[].parts[].text`**：OACP 的卡片客户端只从 artifact 取
   文本（`oacp/adp/card.py`），只有 `status.message` 的 task 在面板里是一片空白。

限制（README 里也写了，不是 TODO）：
  * 没有流式（`message/stream` / `tasks/resubscribe` 一律拒）；
  * 任务共享本进程的会话（与钉钉一样），远端能通过提示读到最近的对话；
  * 任务表只在内存，进程重启后 `tasks/get` 是 `-32001`；
  * `tasks/cancel` 只是「不再等」，模型仍会把这一步跑完。

实现说明（不是接口）：本文件是频道实现，不做命令行入口；`openthree -A2A` 由
`openthree/cli.py` 接线，`openthree channel add --type a2a` 由 `channels/cli.py`
的通用向导生成配置。
"""
from __future__ import annotations

import collections
import errno
import json
import os
import queue
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from .. import __version__
from .base import Channel, InboundMessage

# --- 默认值（默认值只住在这一处；`config._SPECS` 的 Field 只写 hint） --------
DEFAULT_REGISTRY = "http://127.0.0.1:8000"
DEFAULT_HOST = "127.0.0.1"
PORT_RANGE = (9000, 9999)
DEFAULT_BLOCK_WAIT = 45.0
# 端口的第三个来源（比「多一个来源」更贵）：命令行与 channels.yaml 已经够了。
ENV_REGISTRY = "OPENTHREE_A2A_REGISTRY"

CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/a2a"
PROTOCOL_VERSION = "0.3.0"
USER_AGENT = f"openthree-a2a/{__version__}"

MAX_BODY_BYTES = 1 << 20        # 1 MiB：入站请求的硬闸
SOCKET_TIMEOUT = 15.0           # 单连接读写超时（别让一个哑连接占着线程）
TASK_LIMIT = 200                # 任务表最多留最近多少条（只在内存）
REGISTER_INTERVAL = 60.0        # 心跳间隔
REGISTER_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
TERMINAL_STATES = ("completed", "failed", "canceled")

# --- 回调（push notifications）的参数 --------------------------------------
PUSH_TIMEOUT = 5.0              # 单次 POST 的超时（对面是个 HTTP 服务，不是 registry）
PUSH_TRIES = 4                  # 最多试几次（首次 + 3 次重试）
PUSH_BACKOFF = (0.5, 1.0, 2.0)  # 两次之间的等待
PUSH_READ_LIMIT = 4096          # 只读这么多响应正文：我们只关心状态码
PUSH_STOP_GRACE = 5.0           # stop() 等投递线程排空的上限（秒）

# JSON-RPC 方法名：0.3 的斜杠写法与 1.x 的驼峰写法都收 —— 这是十行的事，
# 换来的是「面板想用哪套方言都通」。`message/stream` **故意不在**表里：它落到
# 「未知方法 -32601」，而那正是客户端换方言重试的信号。
_SEND_METHODS = ("message/send", "SendMessage")
_GET_METHODS = ("tasks/get", "GetTask")
_CANCEL_METHODS = ("tasks/cancel", "CancelTask")
# 回调（push notification）三个方法，两套方言都收：0.3 的斜杠写法，与 1.x 的
# `CreateTaskPushNotificationConfig` 驼峰写法。参数名同样有方言差（见
# `_push_from_params`），但**语义只有一套**：登记 `{url, token}`，终态时 POST 整个 Task。
_PUSH_SET_METHODS = ("tasks/pushNotificationConfig/set", "CreateTaskPushNotificationConfig",
                     "SetTaskPushNotificationConfig")
_PUSH_GET_METHODS = ("tasks/pushNotificationConfig/get", "GetTaskPushNotificationConfig")
_PUSH_DELETE_METHODS = ("tasks/pushNotificationConfig/delete", "DeleteTaskPushNotificationConfig")
# `list` 不收：我们的一个 task 只有一个回调地址（登记即替换），列出来永远是那一条。
# 拒它是**准确**的答复，不是实现缺口。
_UNSUPPORTED_METHODS = (
    "tasks/resubscribe", "ResubscribeTask",
    "tasks/pushNotificationConfig/list", "ListTaskPushNotificationConfig",
)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class A2aError(RuntimeError):
    """起不来（配置矛盾 / 端口被占 / 端口范围全满）—— 一律在 `run()` 里抛。"""


class _TransportError(RuntimeError):
    """连不上 registry / 超时。**不包含**「连上了但回 4xx」—— 那是一个要处理的
    答案（404 = 该重新注册，401 = 没权限删别人的条目），不是异常。"""


class _RpcError(Exception):
    """JSON-RPC 层的错误：带标准错误码，由 `handle_rpc` 转成 error 响应。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)


# --- 小工具 ---------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_port(raw: Any) -> Optional[int]:
    """空 → None（= 自动模式）；非法值直接报错，别静默当成「没给」。"""
    text = _as_text(raw)
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise A2aError(f"端口必须是整数，收到 {text!r}") from None
    if not 0 <= value <= 65535:
        raise A2aError(f"端口必须在 0–65535 之间，收到 {value}")
    return value


def _as_float(raw: Any, default: float) -> float:
    text = _as_text(raw)
    if not text:
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        raise A2aError(f"这个值必须是数字，收到 {text!r}") from None


def _client_waits(config: Dict[str, Any], *, push: bool = False) -> bool:
    """这次 `message/send` 该等到答案再回吗？—— 缺省 `not push`。

    A2A 规范里 `blocking` 缺省是 `false`（立刻回 ack，调用方自己轮询）。这里
    不照那个缺省来，但两种情况分开答，判据是同一条：**对方拿得到答案吗？**

    * **登记了回调地址（`push=True`）** —— 按规范缺省，立刻回 `working`。答案
      一会儿由我们 POST 到那个地址，对方不需要在响应里等，也不需要在别处轮询。
      挂住 45 秒等一个马上要单独送出去的结果，只会让长任务撞上对方的 HTTP 超时。
    * **没登记（`push=False`）** —— 按「等」处理。这是实测出来的：有的客户端
      收到 `working` 之后**再也不问** —— 面板既不发 `message/stream`，也不轮询
      `tasks/get`；它的「刷新」只重读自己账本里的一条记录，`GetTask` 只在用户
      点按钮时才发一次。对这类调用方，答案**只有**放进这个响应里才看得见；回得
      太早等于把结论丢进一个没人读的地方（曾经就是这样：任务 5 秒跑完，界面上
      挂着 10 分钟）。

    等的代价有界：最多 `block_wait` 秒（默认 45，必须小于对面客户端的超时，
    面板默认 60），到点照旧回 `working`，任务继续跑，之后一次 `tasks/get`
    仍能拿到结论。

    显式给了 `blocking` 的调用方知道自己要什么，一律照办 —— 包括「登记了回调
    但仍然要等」（`blocking: true`）。值读不懂（`"true"`、`""`、`1`…）时按
    `not push` 处理：错的另一边是对方永远看不到结果，而这一边只是慢一点。
    """
    raw = config.get("blocking")
    if isinstance(raw, str):
        return raw.strip().lower() not in {"false", "0", "no", "off"}
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    return not push


def _push_config(raw: Any) -> Optional[Dict[str, Any]]:
    """把一处回调配置收敛成 `{url, token?, authorization?}`；没给 → `None`。

    地址非法**当场拒**（`-32602`）：静默接受一个 `ftp://` / 相对路径 / 缺 host 的
    地址，等于承诺「结果会送到」却永远不会有人收到 —— 对方只会看到任务一直
    `working`，而这正是「回调」要消灭的那种症状。宁可让它在登记这一刻就失败。

    只收 `http` / `https`：这两个是我们唯一会真的去 POST 的协议。`token` 原样
    保留（它要走 `X-A2A-Notification-Token` 头），`authentication.credentials`
    按规范当作**完整的 Authorization 头值**使用。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    url = _as_text(raw.get("url"))
    if not url:
        raise _RpcError(-32602, "pushNotificationConfig.url 不能为空")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise _RpcError(-32602, f"回调地址必须是 http(s) 绝对地址，收到 {url!r}")
    config: Dict[str, Any] = {"url": url}
    token = _as_text(raw.get("token"))
    if token:
        config["token"] = token
    credentials = _as_text(_as_dict(raw.get("authentication")).get("credentials"))
    if credentials:
        config["authorization"] = credentials
    return config


def _push_from_configuration(configuration: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """`message/send` 里内联带过来的回调配置（0.3 与 1.x 两个位置都认）。

    内联是唯一**不会赛跑**的位置：任务在同一个请求里就被登记了回调地址，所以它
    刚跑完那一下（可能比我们回包还早）也知道该往哪儿送。
    """
    for key in ("pushNotificationConfig", "taskPushNotificationConfig"):
        if key in configuration:
            return _push_config(configuration.get(key))
    return None


def _push_from_params(params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """`tasks/pushNotificationConfig/set` 的参数（三套方言的参数名）。"""
    for key in ("pushNotificationConfig", "taskPushNotificationConfig", "config"):
        if params.get(key) is not None:
            return _push_config(params.get(key))
    return None


def _push_public(config: Dict[str, Any]) -> Dict[str, Any]:
    """回给调用方的配置视图：`url` + `token`（credentials 只进不出）。"""
    doc = {"url": config.get("url")}
    if config.get("token"):
        doc["token"] = config["token"]
    return doc


def _is_loopback(host: str) -> bool:
    host = (host or "").strip().lower().strip("[]")
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def _ports_desc(ports: List[int]) -> str:
    """「哪些端口」的人话版：少就逐个列，多就报首尾与个数。

    逐个列是给「候选只有几个」的场景看的（人的心理模型就是「它试了几个」）；
    自动模式扫 1000 个时把它们全打印出来只会把真正的原因挤到屏幕外。
    """
    if len(ports) <= 12:
        return ", ".join(str(p) for p in ports)
    return f"{ports[0]} 到 {ports[-1]} 共 {len(ports)} 个"


def _addr_in_use(exc: OSError) -> bool:
    """`EADDRINUSE` 才算「被占」——`EACCES`（低端口要权限）必须直接抛，
    否则 1000 个候选全失败，用户只看到「9000–9999 全占满了」而真实原因是权限。"""
    code = getattr(exc, "errno", None)
    in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048), 10048, 48}
    return code in in_use


def _opener() -> urllib.request.OpenerDirector:
    """不带代理的 opener。

    默认的 `urlopen` 会读 `HTTP_PROXY` / `http_proxy`，而我们要打的是 `127.0.0.1`
    与私有地址 —— 走代理只会 502，且症状（"registry 连不上"）与真实原因毫无关系。
    空 `ProxyHandler` 把环境变量彻底绕开。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：把 3xx 原样交回去当状态码（回调投递要的就是这个）。

    跟随重定向等于把「POST 到 A」变成「POST 到 A 指定的任何地方」——地址本来是
    调用方给的，再让它链式改一次，我们就成了替它打内网的跳板。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _redact(url: str) -> str:
    """人能看的地址：只留 `scheme://host/…`。

    两样东西都不该进日志 / 终端 / 异常信息：**path**（一次性 token 常常直接做路径段，
    例如 `/a2a/push/<token>`）与 **userinfo**（`https://user:secret@host/...` 里的凭据）。
    """
    parts = urlsplit(url)
    if not parts.netloc:
        return "回调地址"
    netloc = parts.netloc.rpartition("@")[2]      # 去掉 user:pass@
    return f"{parts.scheme}://{netloc}/…"


def http_call(opener: urllib.request.OpenerDirector, method: str, url: str,
              payload: Optional[Dict[str, Any]] = None, *,
              headers: Optional[Dict[str, str]] = None,
              timeout: float = 15.0,
              read_limit: Optional[int] = None) -> Tuple[int, str]:
    """发一个请求，返回 `(状态码, 正文)`。

    4xx/5xx **不抛异常**：这一层要读 404（重新注册）、401（无权删别人的条目）
    这些**答案**。只有连不上 / 超时才抛 `_TransportError`。

    `read_limit` 给了就最多读这么多字节（回调投递只关心状态码，不该被对面的
    响应正文拖着走 —— 一个几 GB 的正文能把内存吃干净）。
    """
    body: Optional[bytes] = None
    hdrs: Dict[str, str] = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if payload is not None:
        # `errors="replace"`：正文里可能带着客户端给的文本，而 `json.loads` 会忠实保留
        # 孤立代理项（`"\ud800"`），`ensure_ascii=False` 不转义它，直接 encode 会抛
        # `UnicodeEncodeError` —— 那会从 `_TransportError` 的except 旁边漏过去，表现成
        # 「这条回调永远发不出去，也不重试」。与 `_send_json` 同一处修法。
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(read_limit) if read_limit is not None else resp.read()
            return int(getattr(resp, "status", 200) or 200), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(read_limit) if read_limit is not None else exc.read()
            detail = detail.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读不出正文不影响状态码这个事实
            detail = ""
        return int(exc.code), detail
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        # 地址走 `_redact`：这个字符串会被 `_deliver` 原样打印到终端，而回调地址的
        # path 常常就是一次性 token（`/a2a/push/<token>`）。异常的用处是「连不上/
        # 超时」，不是「地址长什么样」。
        raise _TransportError(f"{method.upper()} {_redact(url)} 失败：{reason}") from exc


def message_text(message: Any) -> str:
    """从一条 A2A message 里取出文本。

    三套形状都得认：0.3 的 `parts[{"kind":"text","text":…}]`、1.x 的
    `parts[{"type":"TextPart","text":…}]`、以及老客户端直接给 `{"text": …}`。
    """
    if not isinstance(message, dict):
        return ""
    chunks: List[str] = []
    parts = message.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                chunks.append(part)
                continue
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            kind = str(part.get("kind") or part.get("type") or "").lower()
            if not kind or "text" in kind:
                chunks.append(text)
    if not chunks:
        direct = message.get("text")
        if isinstance(direct, str):
            chunks.append(direct)
    return "\n\n".join(c.strip() for c in chunks if c.strip())


def build_card(*, name: str, url: str, version: str = __version__) -> Dict[str, Any]:
    """构造 AgentCard。

    两拨人要同时喂饱：ADP 严格校验要 `supportedInterfaces` 非空、`capabilities`
    是对象、输入输出模式非空、`skills[]` 有 `id` + `name`；只看 `url` 的老客户端
    也要能选到端点 —— 所以两个字段都写、都指向同一地址。

    `capabilities.pushNotifications` 是**回调的门**：置 `true` 的客户端才会把
    `{url, token}` 递过来（内联或 `tasks/pushNotificationConfig/set`），我们也才会
    在任务终态时 POST 整个 Task 过去。不置它却收配置，等于宣告了一个对方看不见的
    能力 —— 而 A2A 里客户端就是照这张卡片决定要不要给地址的。

    `protocolVersion` 钉在 `0.3.0`：它决定对方用哪套方法名与 message 形状，
    而我们一律回 0.3 形状。`skills` 只有一条，也是故意的：卡片暴露的是**对外的
    接口**（"你能让它干什么"），不是实现里的工具清单 —— 把自造 functions 塞进去
    等于把本机提示词与工具名广播到 registry，还会随每次 `write_function` 抖一次
    `cardHash`。
    """
    return {
        "name": name or "openthree",
        "description": "openthree 内核上的智能体：接一条自然语言任务，跑完把结论写进 artifact。",
        "version": version,
        "protocolVersion": PROTOCOL_VERSION,
        "url": url,
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION},
        ],
        "capabilities": {"streaming": False, "pushNotifications": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "openthree.task",
                "name": "执行任务",
                "description": "把一句话交给这个智能体，它跑完 ReAct 循环后把结论写进 artifact。",
                "tags": ["openthree", "agent", "task"],
            },
        ],
    }


# --- 任务表 ---------------------------------------------------------------
class _TaskStore:
    """内存任务表：最多留最近 `limit` 条，一把可重入锁。

    锁不是防御性代码：HTTP 线程（`message/send` 建任务）与桥的工作线程
    （`send_text` 改状态）**必然**并发，而 `ThreadingHTTPServer` 每条连接还是
    独立线程。

    不做落盘（与微信的 token 文件不同）：落盘就要回答「重启后 task 还在、但那次
    ReAct 的上下文没了」，那会让 `input-required` 之类的状态变成谎话。宁可让它
    干净地消失（`tasks/get` 回 `-32001`，面板显示查询失败并保留它自己的本地记录）。
    """

    def __init__(self, limit: int = TASK_LIMIT) -> None:
        self.lock = threading.RLock()
        self.limit = max(1, int(limit))
        self._items: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
        # 被挤出表的那条任务也是「有结论了」（failed），一样要回调 —— 否则对方
        # 会一直盯着 `working`。频道构造时挂上，不传就什么也不做（纯数据表的用法）。
        self.on_terminal: Optional[Any] = None

    def new(self, *, task_id: str, context_id: str, text: str) -> Dict[str, Any]:
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "submitted", "timestamp": _now()},
            "artifacts": [],
            "history": [{"role": "user", "parts": [{"kind": "text", "text": text}]}],
            "_done": threading.Event(),
        }
        with self.lock:
            self._items[task_id] = task
            self._items.move_to_end(task_id)
            self._evict()
        return task

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        if not task_id:
            return None
        with self.lock:
            task = self._items.get(task_id)
            if task is not None:
                self._items.move_to_end(task_id)
            return task

    def all(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self._items.values())

    def _evict(self) -> None:
        while len(self._items) > self.limit:
            _, old = self._items.popitem(last=False)
            if _state_of(old) not in TERMINAL_STATES:
                # 被挤出表：还在跑的那次 ReAct 已经没法把答案交回来了，与其让它永远
                # `working`，不如说清「我丢了它」。artifact 也要写 —— 面板只从
                # `artifacts[].parts[].text` 取文本，只写 `status.message` 的失败原因
                # 在界面上是一片空白，等于让人对着空白猜。
                _set_terminal(old, "failed", "任务表已满，这条任务被挤出了")
                if self.on_terminal is not None:
                    # 挤出也是一次终态：登记过回调的任务同样该收到一条结论，
                    # 否则对方会一直等一个再也不会来的回调。只入队，不做 IO。
                    self.on_terminal(old)
            old["_done"].set()


def _state_of(task: Dict[str, Any]) -> str:
    return str(_as_dict(task.get("status")).get("state") or "")


def _set_terminal(task: Dict[str, Any], state: str, text: str) -> None:
    """置终态 + 写 artifact + 追加一条 agent 历史（**调用方持锁**）。

    面板只从 `artifacts[].parts[].text` 取文本 —— 没有 artifact 的 task 在界面上
    是一片空白，失败原因也一样（所以 `failed` / `canceled` 同样带 artifact）。
    三个入口共用这一处：正常回话、服务退出、任务表挤出。
    """
    task["status"] = {"state": state, "timestamp": _now(),
                      "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]}}
    task["artifacts"] = [{
        "artifactId": f"{task.get('id')}-answer",
        "name": "answer",
        "parts": [{"kind": "text", "text": text}],
    }]
    task["history"].append({"role": "agent", "parts": [{"kind": "text", "text": text}]})


def _public(task: Dict[str, Any], history_length: Any = None) -> Dict[str, Any]:
    """对外的 task 视图（去掉内部的 `_done`，并深拷贝一份，免得调用方看到半更新）。"""
    history = list(task.get("history") or [])
    if isinstance(history_length, int) and history_length >= 0:
        history = history[-history_length:] if history_length else []
    doc = {
        "kind": "task",
        "id": task.get("id"),
        "contextId": task.get("contextId"),
        "status": task.get("status") or {},
        "artifacts": task.get("artifacts") or [],
        "history": history,
    }
    return json.loads(json.dumps(doc, ensure_ascii=False))


# --- HTTP 层 --------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    """A2A 的 HTTP 服务端。

    `allow_reuse_address` 就是 `SO_REUSEADDR`，而**两个平台的语义不一样**：

    * POSIX —— 只允许 bind 处于 `TIME_WAIT` 的地址，**不让两个 listening socket
      共用端口**。所以要保留：关掉它，紧接着的重启会撞 `TIME_WAIT`，端口被迫漂移，
      白收尸一次。
    * Windows —— 它**允许 bind 一个正在被监听的端口**（要拒绝得用
      `SO_EXCLUSIVEADDRUSE`）。所以必须关掉：否则端口冲突永远不报错，自动探测
      招招"成功"，两个进程都以为自己在收任务，症状是「任务有时没响应」。

    一行、带注释，别想当然改成一刀切的 True 或 False。
    """

    daemon_threads = True
    allow_reuse_address = not sys.platform.startswith("win")
    # 另一件同源的事：`SO_REUSEPORT`（`allow_reuse_port`）。它是**继承来的默认值**
    # （3.11 才有这个属性，当前 CPython 里是 False），但写死它是因为后果与上面那条
    # 一样严重 —— 在支持 SO_REUSEPORT 的系统上，两个监听者可以**同时**绑同一个端口，
    # 于是「显式端口被占就报错」和自动探测都变成谎话，症状是「任务有时没响应」。
    allow_reuse_port = False

    def __init__(self, address: Tuple[str, int],
                 handler_cls: type, channel: "A2aChannel") -> None:
        self.channel = channel
        super().__init__(address, handler_cls)

    def server_bind(self) -> None:
        # 跳过 HTTPServer.server_bind 里的 socket.getfqdn()：反查 DNS 在离线机器上
        # 能卡好几秒，而这里只要一个端口号（`server_port` 也要，别处会读）。
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    """两个 GET 路径 + 一个 POST 路径。其余一律 404。"""

    protocol_version = "HTTP/1.1"
    server_version = "openthree-a2a"
    timeout = SOCKET_TIMEOUT          # StreamRequestHandler 会拿它 settimeout

    @property
    def channel(self) -> "A2aChannel":
        return self.server.channel          # type: ignore[attr-defined]

    # 访问日志走频道自己的 logger（`-q` 时没人想看每个请求一行）
    def log_message(self, fmt: str, *args: Any) -> None:
        self.channel.log.debug("a2a http: " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定名
        path = urlsplit(self.path).path.rstrip("/") or "/"
        # GET 带请求体时，正文没有被读走（GET 本来就没有正文要处理），而 `protocol_version`
        # 是 HTTP/1.1：那串字节会被当成**下一个请求行**去解析 —— 与 POST 侧 411 那条同一个
        # 洞（前置反向代理做鉴权时尤其危险）。400 会顺手关连接。
        # `Content-Length: 0` 是合法的（有些客户端习惯带上），只看 > 0。
        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = -1
        if declared > 0 or self.headers.get("Transfer-Encoding"):
            self._send_json(400, {"error": "GET 不应带请求体"})
            return
        if path == CARD_PATH:
            self._send_json(200, self.channel.card())
            return
        self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path != RPC_PATH:
            self._send_json(404, {"error": "not found", "path": path})
            return
        if self.headers.get("Transfer-Encoding"):
            # chunked 没有 `Content-Length`：不读走 body 就让 keep-alive 去解析它 =
            # 请求走私的口子（下一个请求行会是这一段二进制）。这个服务不收分块上传，
            # 411 是准确的答复；411 ≥ 400，`_send_json` 会顺手关掉连接。
            self._send_json(411, {"error": "不支持 Transfer-Encoding/chunked，"
                                           "请带 Content-Length"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "bad Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            # 先看长度再读：不能把一个 100 MB 的请求先吞进内存再拒绝。
            self._send_json(413, {"error": f"请求超过 {MAX_BODY_BYTES} 字节"})
            return
        # 跨站防线，不是洁癖：只听回环**挡不住浏览器**。一个任意网页可以用
        # `Content-Type: text/plain` 的「简单请求」直接 POST 到这里（没有 preflight），
        # 而 DNS rebinding 还能让它变成同源 —— 后果是「用你的工具链执行任务、并把结论
        # 读回来」。要求 `application/json` 就够：浏览器为它必定先发 **preflight**，
        # 而本服务不实现 OPTIONS（落到 501），请求根本不会发出来。
        # 判据要守住：**将来谁加了 CORS / OPTIONS 支持，就必须同时加 Host / Origin 校验**，
        # 否则这道防线会被自己拆掉。JSON-RPC 绑定本来就要求 application/json。
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {"error": "Content-Type 必须是 application/json"})
            return
        raw = self.rfile.read(length) if length > 0 else b""
        status, payload = self.channel.handle_rpc(raw, caller=self._caller())
        self._send_json(status, payload)

    def _caller(self) -> str:
        return str((self.client_address or ("",))[0])

    def _send_json(self, status: int, payload: Any) -> None:
        # `errors="replace"`：客户端可以用 JSON 转义塞进孤立代理项（`\ud800`），
        # `ensure_ascii=False` 不会转义它，直接 encode 会抛 UnicodeEncodeError ——
        # 响应里回显的就是这段 history，于是那个 task 此后的每次 `tasks/get` 都变成
        # 一条断连，重试同一个 taskId 只会反复撞上同一条记录。
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
        if status >= 400:
            # 错误响应**不读**正文（413 更是故意不读：1 MB 以上的请求不能先吞进内存，
            # 400/404 也没必要读）。但 `protocol_version` 是 HTTP/1.1：一条 keep-alive
            # 连接上没被读走的字节会被当成下一个请求的请求行。关连接是最确定的收尾。
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))   # HTTP/1.1 必须有
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):       # 对方先挂了，不必喊
            pass


# --- 频道 -----------------------------------------------------------------
class A2aChannel(Channel):
    """A2A 服务端频道：JSON-RPC 进来 → `emit()` → 桥 → `send_text()` 回答案。"""

    type = "a2a"

    def __init__(self, name: str, options: Dict[str, Any], **kwargs: Any) -> None:
        super().__init__(name, options, **kwargs)
        self._tasks = _TaskStore()
        self._state: Dict[str, Any] = {}
        self._state_path: Optional[Path] = None
        self._state_lock = threading.Lock()
        self._opener: Optional[urllib.request.OpenerDirector] = None
        self._server: Optional[_Server] = None
        self._serving = threading.Event()
        # `run()` 的「检查 stopping + 标记已服务」与 `stop()` 的 shutdown 互斥要用的锁
        self._lifecycle = threading.Lock()
        self._registrar: Optional[threading.Thread] = None
        # 回调投递：一条 FIFO 工作线程 + 一个无界队列。线程**按需启动**（第一次有
        # 任务登记了回调地址时才起），因为 handle_rpc 在没有 run() 的测试里也会被调。
        self._push_lock = threading.Lock()
        self._pusher: Optional[threading.Thread] = None
        self._push_stopped = False
        self._push_q: "queue.Queue[Any]" = queue.Queue()
        self._push_opener_obj: Optional[urllib.request.OpenerDirector] = None
        self._tasks.on_terminal = self._notify_push
        self._prepared = False
        self._notes: List[str] = []

        # 以下在 `_load_options()` 里定型（异常留到 run() 才抛：配置写错一条，
        # 不该连 `channel list` 一起炸掉）
        self.registry = DEFAULT_REGISTRY
        self.host = DEFAULT_HOST
        self.explicit_port: Optional[int] = None
        self.advertise = ""
        self.no_register = False
        self.block_wait = DEFAULT_BLOCK_WAIT
        self.port = 0
        self.base_url = ""
        # 两个地址不是一回事，混用会让注册稳定地失败在 422：
        #   card_url      = RPC 端点（`/a2a`），写进卡片的 `url` 字段，客户端调它；
        #   card_endpoint = 卡片地址（`/.well-known/agent-card.json`），交给 registry。
        # ADP 的 `POST /v1/agents {"url": …}` 是「这个 url 的卡片」——registry 会去 GET
        # 它。交 RPC 端点等于让对面 GET 一个只收 POST 的路径，必回 404。
        self.card_url = ""
        self.card_endpoint = ""

    # --- 配置 ---------------------------------------------------------
    def _load_options(self) -> None:
        """把 options 收敛成属性。**只在 run() / 测试里调**，不放进 `__init__`。"""
        if self._prepared:
            return
        self.registry = (_as_text(self.option("registry", "")
                                  or os.environ.get(ENV_REGISTRY, "")
                                  or DEFAULT_REGISTRY) or DEFAULT_REGISTRY).rstrip("/")
        if "://" not in self.registry:
            self.registry = "http://" + self.registry
        self.host = _as_text(self.option("host", DEFAULT_HOST)) or DEFAULT_HOST
        self.explicit_port = _as_port(self.option("port", ""))
        self.advertise = _as_text(self.option("advertise", ""))
        self.no_register = self.option_bool("no_register", False)
        self.block_wait = _as_float(self.option("block_wait", ""), DEFAULT_BLOCK_WAIT)
        if self.block_wait < 0:
            raise A2aError(f"block_wait 不能是负数：{self.block_wait}")
        self._prepared = True

    @property
    def mode(self) -> str:
        """`explicit`（人给了端口）或 `auto`（没给，按候选序列挑）。"""
        return "explicit" if self.explicit_port is not None else "auto"

    # --- 状态落盘 -----------------------------------------------------
    def state_path(self) -> Path:
        root = Path(self.workspace_dir) if self.workspace_dir else Path(".openthree")
        return root / "channels" / f"{self.name}.json"

    def _load_state(self) -> None:
        self._state_path = self.state_path()
        self._state = {}
        if not self._state_path.exists():
            return
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏档就当没注册过，重注册一次
            self.log.warning("a2a 状态文件读不出来，将重新注册：%s（%s）",
                             self._state_path, exc)
            return
        # 只认 dict：手改过的文件可能是列表 / 字符串，那样后面每个 `.get()` 都会炸
        self._state = loaded if isinstance(loaded, dict) else {}

    def _save_state(self, **updates: Any) -> None:
        if self._state_path is None:
            self._state_path = self.state_path()
        with self._state_lock:
            self._state.update({k: v for k, v in updates.items() if v is not None})
            try:
                from ..workspace import atomic_write
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(self._state_path,
                             json.dumps(self._state, ensure_ascii=False, indent=2) + "\n")
            except Exception as exc:  # noqa: BLE001
                self.log.warning("a2a 状态文件写入失败：%s", exc)

    # --- 端口 ---------------------------------------------------------
    def bind_server(self) -> _Server:
        """按两种互斥模式拿到一个**已经 bind 好**的服务端。

        显式模式**不构造候选列表**：一旦写成
        `ports = [explicit] + [last_port] + range(...)` 再循环，就把「降级」
        写进了结构里，下一个人顺手删掉循环里的 raise 就静默回归了 ——
        用户以为自己在 9000，registry 里注册的是 9001，两个地址、两个 `id`，
        而他手里只有「我指定了 9000」这一条线索。
        """
        self._load_options()
        if self.explicit_port is not None:
            return self._bind_one(self.explicit_port, explicit=True)

        try:
            last_port = _as_port(self._state.get("last_port"))
        except A2aError:
            # 手改坏的状态文件只该让**这一项**失效：`_load_state` 对坏 JSON 已经是
            # 「警告、当没写过」，这里多抛一个错就等于让整个自动模式起不来。
            self.log.warning("状态文件里的 last_port 不是整数，忽略：%r",
                             self._state.get("last_port"))
            last_port = None
        low, high = PORT_RANGE
        candidates: List[int] = []
        if last_port:
            candidates.append(int(last_port))      # 「别让自己漂」：正常重启复用上次的
        candidates.extend(p for p in range(low, high + 1) if p != last_port)
        skipped: List[int] = []
        for port in candidates:
            try:
                server = self._bind_one(port, explicit=False)
            except A2aError as exc:
                if not getattr(exc, "addr_in_use", False):
                    if port != last_port:
                        # 候选范围里的非「被占用」错误（EACCES：低端口要权限）必须
                        # 直接抛：换端口也白搭，而试完 1000 个只会让真正的原因消失。
                        raise
                    # `last_port` 是历史遗留值：上一次可能是**显式**指定的特权端口
                    # （80 / 443），今天不该因为它让整个自动模式起不来。
                    self._notes.append(f"上次用过的端口 {port} 现在绑不上（{exc}），换下一个")
                    continue
                skipped.append(port)
                continue
            if skipped:
                # 一台忙机器上 9000–9999 可能被占掉一大片：逐个打几百行会把紧跟其后
                # 的安全警告挤出屏幕，也会把「它换到哪儿了」这件正事淹掉。
                self._notes.append(f"跳过了被占用的端口 {_ports_desc(skipped)}，改用 {port}")
            return server
        raise A2aError(
            f"{low}–{high} 里没有可用端口（试过 {_ports_desc(candidates)}，全被占）。"
            f"用 `--a2a-port` 指定一个别的端口，或先停掉占着它们的进程。")

    def _bind_one(self, port: int, *, explicit: bool) -> _Server:
        try:
            return _Server((self.host, int(port)), _Handler, self)
        except OSError as exc:
            in_use = _addr_in_use(exc)
            reason = "被占用" if in_use else f"无法监听（{exc}）"
            if explicit:
                low, high = PORT_RANGE
                err = A2aError(
                    f"端口 {port} {reason}。**指定了端口就不会自动换一个** —— "
                    f"换一个端口，或先停掉占着它的进程"
                    f"（也可以省略 --a2a-port，让它从 {low}-{high} 里自动挑）。")
            else:
                err = A2aError(f"端口 {port} {reason}")
            err.addr_in_use = in_use        # 只有「被占用」才值得试下一个
            raise err from exc

    # --- 地址 ---------------------------------------------------------
    def resolve_base_url(self, port: int) -> str:
        """卡片里登记的地址基址。

        `--a2a-advertise` 是**跨机器注册时必须填的**：registry 抓的是「它自己眼里
        的地址」，卡片写 `127.0.0.1` 而 registry 在 10.0.0.5 上时，它抓的是它自己
        的回环，注册必然 `CARD_UNREACHABLE`。
        """
        raw = self.advertise
        if not raw:
            host = self.host
            if host in ("0.0.0.0", "::", ""):
                host = DEFAULT_HOST            # 监听全零地址时，卡片要给一个能连的
            return f"http://{host}:{port}"
        if "://" not in raw:
            raw = "http://" + raw
        parts = urlsplit(raw)
        if not parts.scheme or not parts.hostname:
            raise A2aError(f"--a2a-advertise 不是合法地址：{self.advertise!r}")
        try:
            adv_port = parts.port
        except ValueError as exc:
            # `http://host:abc` / `:99999` 会让 urlsplit 在取 .port 时抛 ValueError，
            # 而这里所有配置错误都该是 A2aError（带一句人话），不是裸的解析异常。
            raise A2aError(
                f"--a2a-advertise 里的端口不合法：{self.advertise!r}（{exc}）") from exc
        if adv_port is not None and adv_port != port:
            if self.explicit_port is not None:
                # 显式模式下不存在漂移，只有**自相矛盾**：两个值都是人写的，
                # 挑哪个都等于替用户改他自己的配置 —— 能做的只有指出来。
                raise A2aError(
                    f"--a2a-advertise 里的端口 {adv_port} 与 --a2a-port {port} 矛盾。"
                    f"要么去掉 advertise 里的端口，要么把它们改成同一个。")
            self._notice(f"警告：--a2a-advertise 里的端口 {adv_port} 与实际监听端口 {port} "
                         f"不一致 —— 卡片地址可能抓不到")
        host = parts.hostname
        if ":" in host:                        # IPv6
            host = f"[{host}]"
        netloc = f"{host}:{port}" if adv_port is None else parts.netloc
        return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))

    # --- 卡片 ---------------------------------------------------------
    def display_name(self) -> str:
        """读 `identity.json` 里的名字（每次现读）—— 智能体自己改名后，下一次
        registry 探测就会看到 `cardHash` 变化，这正是 ADP 的 `changedAt` 为之准备的。"""
        root = Path(self.workspace_dir) if self.workspace_dir else Path(".openthree")
        try:
            doc = json.loads((root / "identity.json").read_text(encoding="utf-8"))
            name = _as_text(_as_dict(doc).get("name"))
        except Exception:  # noqa: BLE001 - 没名字 / 文件坏了都只是「还没起名」
            name = ""
        return name or "openthree"

    def card(self) -> Dict[str, Any]:
        return build_card(name=self.display_name(), url=self.card_url)

    # --- 生命周期 -----------------------------------------------------
    def run(self) -> None:
        self._load_options()
        self._load_state()
        server = self.bind_server()
        self._server = server
        # bind 之后的**每一条**退出路径都要把端口还回去（地址配错、stdout 被关掉、
        # registrar 起不来……），所以从这里开始只留一个 finally 收尾：
        # 少一个出口，就少一处端口泄漏 —— 而端口泄漏在 manager 眼里只是一句
        # 「频道起不来」，没人能顺着它查到「端口其实还占着」。
        try:
            self.port = int(server.server_address[1])
            self.base_url = self.resolve_base_url(self.port)
            self.card_url = self.base_url.rstrip("/") + RPC_PATH
            self.card_endpoint = self.base_url.rstrip("/") + CARD_PATH
            # bind 成功就写 last_port（与注册成败无关）：`--a2a-no-register` 下也要能复用
            self._save_state(last_port=self.port, advertise=self.base_url,
                             registry=self.registry)
            self._print_startup()
            # 「检查 stopping」与「标记已开始服务」必须在**同一把锁**里：否则 stop()
            # 恰好落在两者之间时会看到 `_serving` 未置位而跳过 shutdown()，run() 随后
            # 阻塞在 serve_forever() 里再没人叫醒 —— 频道线程永不退出（manager 5 秒后
            # 放弃），端口一直占到进程结束。锁一对上，两条路就都收敛。
            with self._lifecycle:
                if self.stopping:
                    return
                self._serving.set()
            if not self.no_register:
                self._registrar = threading.Thread(
                    target=self._registrar_loop, name=f"a2a-register-{self.name}", daemon=True)
                self._registrar.start()
            server.serve_forever(poll_interval=0.2)
        finally:
            self._serving.clear()
            server.server_close()
            self._fail_all("A2A 服务退出，这个任务没有跑完")

    def stop(self) -> None:
        """请求退出（重复调用安全）。"""
        self._stop.set()
        # 与 run() 的「检查 stopping + 标记已服务」互斥：见 run() 里那段注释。
        # 只有 `_serving` 已置位时才 shutdown() —— 否则 serve_forever 还没跑起来，
        # 而 `BaseServer.shutdown()` 会一直等 `__is_shut_down`（死锁）。
        with self._lifecycle:
            server = self._server
            if server is not None and self._serving.is_set():
                try:
                    server.shutdown()      # 不能在本线程的 serve_forever 里调，但这里不是
                except Exception:  # noqa: BLE001 - 已经在退出了
                    pass
        # 先把自己名下未终态的任务置 failed，再让迟到的 send_text 落到终态上被忽略
        self._fail_all("A2A 服务退出，这个任务没有跑完")
        # 收尾顺序是刻意的：`_fail_all` 会把「服务退出」这条结论**入队**，随后的
        # `_push_stop` 才让它有机会真的送出去（有上限，别让退出卡在别人的网络上）。
        self._push_stop()
        registrar, self._registrar = self._registrar, None
        if registrar is not None:
            registrar.join(timeout=2.0)

    def _fail_all(self, reason: str) -> None:
        for task in self._tasks.all():
            if _state_of(task) in TERMINAL_STATES:
                continue
            self._finish(task, "failed", reason)

    def _print_startup(self) -> None:
        self._notice(f"监听 http://{self.host}:{self.port}{RPC_PATH}"
                     f"（卡片 {CARD_PATH}）")
        if self.no_register:
            self._notice(f"未向 registry 注册（--a2a-no-register）；"
                         f"要被人发现可注册：{self.registry}")
        else:
            self._notice(f"注册到 registry {self.registry}")
        for note in self._notes:
            self._notice(note)
        self._notes = []
        if not _is_loopback(self.host):
            self._notice("警告：监听地址不是回环 —— 任何能连到这台机器的人都能让它执行任务。"
                         "没有鉴权（面板的客户端也不发凭据），要鉴权请套反向代理")
        if not self.no_register and not _is_loopback(urlsplit(self.registry).hostname or ""):
            card_host = urlsplit(self.base_url).hostname or ""
            if _is_loopback(card_host):
                self._notice(f"警告：registry 在 {self.registry}，而卡片地址是 {self.base_url} "
                             f"—— 它会去抓**它自己**回环上的那个端口，注册多半会失败。"
                             f"跨机器请用 --a2a-advertise 填一个对方能抓到的地址")
            elif urlsplit(self.base_url).scheme == "http":
                self._notice("警告：strict 模式的 registry 只收 https 卡片地址 —— "
                             "远程注册需要在前置 TLS 反向代理后用 --a2a-advertise https://…")

    # --- JSON-RPC -----------------------------------------------------
    def handle_rpc(self, raw: bytes, *, caller: str = "") -> Tuple[int, Dict[str, Any]]:
        """处理一次 JSON-RPC 调用，返回 `(HTTP 状态码, 响应体)`。

        HTTP 层恒回 200（坏 JSON 也是 200 + error 对象）：客户端两种都处理，
        而「200 + JSON-RPC error」比 400 更符合它们的期望。
        """
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "null")
        except (ValueError, UnicodeDecodeError):
            return 200, _rpc_error(None, -32700, "解析不了这段 JSON")
        if isinstance(payload, list):
            return 200, _rpc_error(None, -32600, "不支持批量请求（一次一条）")
        if not isinstance(payload, dict):
            return 200, _rpc_error(None, -32600, "不是合法的 JSON-RPC 请求")
        rid = payload.get("id")
        if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return 200, _rpc_error(rid, -32600, "不是合法的 JSON-RPC 2.0 请求")
        method = str(payload["method"])
        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return 200, _rpc_error(rid, -32602, "params 必须是对象")
        try:
            if method in _SEND_METHODS:
                result = self._rpc_send(params, caller)
            elif method in _GET_METHODS:
                result = self._rpc_get(params)
            elif method in _CANCEL_METHODS:
                result = self._rpc_cancel(params)
            elif method in _PUSH_SET_METHODS:
                result = self._rpc_push_set(params)
            elif method in _PUSH_GET_METHODS:
                result = self._rpc_push_get(params)
            elif method in _PUSH_DELETE_METHODS:
                result = self._rpc_push_delete(params)
            elif method in _UNSUPPORTED_METHODS:
                return 200, _rpc_error(rid, -32004, f"不支持这个操作：{method}")
            else:
                return 200, _rpc_error(rid, -32601, f"不认识的方法：{method}")
        except _RpcError as exc:
            return 200, _rpc_error(rid, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 一条请求炸了不该带走服务
            self.log.exception("a2a 处理 %s 时出错", method)
            return 200, _rpc_error(rid, -32603, f"服务内部错误：{exc}")
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}

    def _rpc_send(self, params: Dict[str, Any], caller: str) -> Dict[str, Any]:
        message = params.get("message")
        if not isinstance(message, dict):
            raise _RpcError(-32602, "params.message 必须是对象")
        text = message_text(message)
        if not text:
            raise _RpcError(-32602, "消息里没有文本（parts 里找不到 text）")
        config = _as_dict(params.get("configuration"))
        # 内联回调地址在这里就取出来（只解析、只校验；写进 task 是在下面那把锁里）。
        # 与任务一起登记，是这个功能唯一不赛跑的位置：任务跑完可能比回包还早，
        # 那一刻它必须已经知道该往哪儿送。
        push = _push_from_configuration(config)

        task_id = _as_text(message.get("taskId"))
        context_id = _as_text(message.get("contextId"))
        # 每一轮一个 run id，**在复位之前**生成：follow up 可能在上一次还在跑的时候就
        # 到了，而桥是串行的 —— 上一轮的答案稍后还会经同一个 `send_text` 回到同一个
        # task。没有这个标记，它会先到先得被当成这一轮的答案。
        # 它与复位必须在**同一次加锁**里切过去：中间留一道缝，旧 run 就能在缝里通过
        # 所有权检查，把旧答案写进刚复位的 working —— 这一轮的真答案随后被终态挡掉。
        run_id = uuid.uuid4().hex[:12]

        # 「查 + 建/复位」必须在**同一把锁**里。`get()` 在锁外、`new()` 在锁内时，两个
        # 并发请求带着同一个还不存在的 `taskId`（客户端自带 id 重试就是这个形状）会都
        # 看到「没有」，于是各建一条 —— `_TaskStore.new()` 是无条件写，后到的覆盖先到的：
        # 先到那条的答案被 `_finish` 的所有权检查丢掉，而任务其实被跑了两遍。
        with self._tasks.lock:              # RLock：`new()` 内部还会再加一次
            previous = self._tasks.get(task_id) if task_id else None
            if previous is not None:
                # 面板的 follow up：把此前往来压成前缀再跑一次（不引入 per-task 会话，
                # 也不假装有 context 隔离 —— 卡片也没声称 context 支持）。
                context_id = context_id or _as_text(previous.get("contextId"))
                prefix = _follow_up_prefix(previous)
                # **先复位再跑**：上一次的终态与 `_done` 都得清掉，否则 `_set_state`
                # 与 `_finish` 会因「已是终态」直接返回 —— 新答案被丢弃，而 blocking
                # 的调用方立刻拿到上一次的旧答案。
                previous["status"] = {"state": "working", "timestamp": _now()}
                previous["artifacts"] = []
                previous["_done"] = threading.Event()
                previous["_run"] = run_id          # 与复位同一把锁：所有权切换是原子的
                # 等待用的 Event 也在这把锁里取：锁外重读 `task["_done"]` 可能拿到
                # 并发的下一轮换上去的那个 —— 而这一轮的答案按所有权规则不会去 set
                # 它，于是白等满 `block_wait` 才回 working。
                wait_event = previous["_done"]
                # 历史里只存**原文**：前缀是给这次 run 用的。把带前缀的文本写回历史，
                # 下一次的前缀就会把上一次的前缀再包一层 —— 逐轮翻倍，没有上界。
                previous["history"].append(
                    {"role": "user", "parts": [{"kind": "text", "text": text}]})
                # 回调地址跟着 **task** 走，不跟着这一轮走：follow up 常常不带配置
                # （调用方只是在同一个任务里追问），而它的新结论一样要送出去。
                if push is not None:
                    previous["_push"] = push
                text = prefix + text
                task = previous
            else:
                task_id = task_id or ("t-" + uuid.uuid4().hex[:16])
                context_id = context_id or ("c-" + uuid.uuid4().hex[:16])
                task = self._tasks.new(task_id=task_id, context_id=context_id, text=text)
                task["_run"] = run_id
                if push is not None:
                    task["_push"] = push
                wait_event = task["_done"]      # 同上：在锁里取，别在锁外重读

        # 有效回调地址看的是 **task**，不是这次请求：follow up 常常不带 configuration，
        # 但地址仍然有效（配置跟着 task 走）。只看这次请求的话，一个已经登记了回调的
        # 追加消息会被白等满 `block_wait`，而答案马上就会由回调送到 —— 正是「等」要
        # 避开的浪费，只是反了过来。
        blocking = _client_waits(config, push=isinstance(task.get("_push"), dict))
        self._set_state(task, "working")
        msg = InboundMessage(
            text=text,
            channel=self.name,
            channel_type=self.type,
            user_id=caller,
            user_name="外部智能体",
            conversation_id=context_id,
            kind="dm",
            msg_id=task["id"],
            reply_token=task["id"],        # send_text 靠它找回这个 task
            raw={"task": task["id"], "context": context_id,
                 "blocking": blocking, "run": run_id},
        )
        # emit 立刻返回：真正跑 ReAct 的是桥的工作线程。跑完它调 send_text 改状态。
        self.emit(msg)

        # 等到答案再回（`_client_waits` 解释了为什么缺省就等）。事件在**锁里**取的：
        # 这里重读 `task["_done"]` 可能拿到并发的下一轮换上去的那个。
        if blocking and self.block_wait > 0:
            wait_event.wait(self.block_wait)
        return self._view(task)

    def _rpc_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task_id = _as_text(params.get("id") or params.get("taskId"))
        if not task_id:
            raise _RpcError(-32602, "缺少 id")
        task = self._tasks.get(task_id)
        if task is None:
            raise _RpcError(-32001, f"没有这个任务：{task_id}（进程重启后任务表会清空）")
        return self._view(task, params.get("historyLength"))

    def _rpc_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task_id = _as_text(params.get("id") or params.get("taskId"))
        if not task_id:
            raise _RpcError(-32602, "缺少 id")
        task = self._tasks.get(task_id)
        if task is None:
            raise _RpcError(-32001, f"没有这个任务：{task_id}")
        with self._tasks.lock:
            if _state_of(task) in TERMINAL_STATES:
                raise _RpcError(-32002, f"任务已是终态，不能取消：{_state_of(task)}")
        # 诚实语义：只是**不再等**，不是停手 —— ReAct 每一步都在等模型返回，
        # 没有中断点。结果照跑，只是不再写进这个 task。
        self._finish(task, "canceled", "调用方取消了这个任务（模型可能仍会把这一步跑完）")
        return self._view(task)

    # --- 回调登记（push notification config）--------------------------
    def _push_task(self, params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """取 `taskId` 指向的任务；没有就 `-32001`。

        `id` 与 `taskId` 都认：0.3 的 send/get 用 `id`，回调这几个方法用 `taskId`，
        而 1.x 的写法两边混着来。认两个键是十行的事，换来的是方言不再重要。
        """
        task_id = _as_text(params.get("taskId") or params.get("id"))
        if not task_id:
            raise _RpcError(-32602, "缺少 taskId")
        task = self._tasks.get(task_id)
        if task is None:
            raise _RpcError(-32001, f"没有这个任务：{task_id}（进程重启后任务表会清空）")
        return task_id, task

    def _rpc_push_set(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """登记 / 替换这个任务的回调地址。

        替换而不是追加：一个 task 只有一个结论要送，留着一堆地址只会让同一条结论
        被送好几遍。登记**不等到终态**才生效 —— 任务此刻还在跑，正好赶上。
        """
        task_id, task = self._push_task(params)
        config = _push_from_params(params)
        if config is None:
            raise _RpcError(-32602, "缺少 pushNotificationConfig")
        with self._tasks.lock:
            task["_push"] = config
        return {"taskId": task_id, "pushNotificationConfig": _push_public(config)}

    def _rpc_push_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task_id, task = self._push_task(params)
        with self._tasks.lock:
            config = task.get("_push")
        if not isinstance(config, dict):
            # 消息里说清「是**配置**没有，不是任务没有」：`-32001` 在别处等于
            # 任务不存在，只给码不给话会让对面以为自己的 id 写错了。
            raise _RpcError(-32001, f"任务 {task_id} 没有登记回调地址")
        return {"taskId": task_id, "pushNotificationConfig": _push_public(config)}

    def _rpc_push_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """撤掉回调地址。本来就没了也算成功 —— DELETE 是幂等的。"""
        task_id, task = self._push_task(params)
        with self._tasks.lock:
            task.pop("_push", None)
        return {"taskId": task_id}

    # --- 回话 ---------------------------------------------------------
    def send_text(self, msg: InboundMessage, text: str) -> None:
        """把桥跑出来的答案写回 `msg.reply_token` 指向的那个 task。"""
        task = self._tasks.get(msg.reply_token or msg.msg_id)
        if task is None:
            return
        run_id = msg.raw.get("run") if isinstance(msg.raw, dict) else None
        body = "" if text is None else str(text).strip()
        # 「这是桥的通知还是模型的结论」判的是**带外标记**（桥在 `_reply_quietly`
        # 里写的 `msg.raw["notice"]`），不是文本前缀：模型完全可能把
        # `[openthree] ` 当作答案的开头（比如它正在解释这个前缀），只按前缀判会把
        # 一条真答案误记成失败回给对面的智能体。
        is_notice = isinstance(msg.raw, dict) and bool(msg.raw.get("notice"))
        if is_notice:
            # 桥自己塞的话（「正在退出」/「这条消息没跑通」）**不是**结论：
            # 记成 completed 会让对面拿到一条"成功"的空答案。
            self._finish(task, "failed", body or "这条消息没有跑完（桥给出的通知是空的）",
                         run_id=run_id)
        elif not body:
            self._finish(task, "failed", "智能体这次没有给出结论（answer 为空）",
                         run_id=run_id)
        else:
            self._finish(task, "completed", body, run_id=run_id)

    def _finish(self, task: Dict[str, Any], state: str, text: str,
                run_id: Any = None) -> None:
        """置终态 + 写 artifact + 唤醒 blocking 等待者。终态之后一律忽略。

        `run_id` 给了就在**同一把锁**里比所有权：比在锁外、改在锁内的话，中间
        那一瞬正好够一个已被 follow up 取代的旧 run 把答案写进来（先到先得），
        这一轮的真答案随后撞上终态检查被丢掉 —— 调用方看到「问 A 答 B」。
        """
        with self._tasks.lock:
            if run_id is not None and task.get("_run") != run_id:
                return
            if _state_of(task) in TERMINAL_STATES:
                return
            _set_terminal(task, state, text)
            # 入队回调（RLock 可重入；`_notify_push` 只快照 + 入队，不做 IO）：
            # 与终态在同一把锁里，送出去的快照就不会是「completed + 空 artifacts」。
            self._notify_push(task)
            # 在锁里捕获事件对象：follow up 会在同一把锁里把 `_done` 换成新的 Event，
            # 锁外重读 `task["_done"]` 可能读到**下一轮**那个 —— set() 唤醒的是还在
            # working 的新一轮等待者，它立刻拿着 working 就走了。
            done = task["_done"]
        done.set()

    def _set_state(self, task: Dict[str, Any], state: str) -> None:
        with self._tasks.lock:
            if _state_of(task) in TERMINAL_STATES:
                return
            task["status"] = {"state": state, "timestamp": _now()}

    def _view(self, task: Dict[str, Any], history_length: Any = None) -> Dict[str, Any]:
        """持锁快照。

        `_set_terminal` / `_set_state` / follow up 的复位都是**分几步**改
        `status` / `artifacts` / `history` 的；不在同一把锁里读，就会看到
        「completed + 空 artifacts」这种撕裂态 —— 面板上是一片空白，而调用方
        以为拿到了结论。深拷贝救不了它：它拷贝的是一串不同瞬间的引用。
        """
        with self._tasks.lock:
            return _public(task, history_length)

    # --- 回调投递（push notifications）--------------------------------
    def _notify_push(self, task: Dict[str, Any]) -> None:
        """把**终态**快照入队，交给投递线程；不做任何 IO。

        只在终态发，是规范允许的一次收窄（规范说「状态或产物变化时」）：我们能
        观察到的迁移只有 `submitted → working → 终态`，而 `working` 那一刻调用方
        手上已经有了 —— 它就写在 `message/send` 的响应里，再推一条只是多一次往返。

        配置在这里**快照**：这一条代表「那个终态已经发生了」。之后 `delete` 只拦住
        后续事件，收不回已经在路上的结论。
        """
        with self._tasks.lock:
            config = task.get("_push")
            if not isinstance(config, dict) or not _as_text(config.get("url")):
                return
            payload = _public(task)          # 与 `tasks/get` 完全同一个视图
            task_id = _as_text(task.get("id"))
        self._ensure_push_worker()
        self._push_q.put((task_id, payload, dict(config)))

    def _ensure_push_worker(self) -> None:
        """按需起投递线程（第一次有回调要发时）。`stop()` 之后不再起。"""
        with self._push_lock:
            if self._push_stopped:
                return
            if self._pusher is not None and self._pusher.is_alive():
                return
            self._pusher = threading.Thread(
                target=self._push_loop, name=f"a2a-push-{self.name}", daemon=True)
            self._pusher.start()

    def _push_loop(self) -> None:
        """单线程 FIFO：同一个 task 的先后两条结论**按顺序**送达。

        一条队列而不是「每条回调一个线程」：多线程会让 follow up 的新结论有机会
        越过上一轮的旧结论，对面看到的就是「先答 A 再答 B」。
        """
        while True:
            item = self._push_q.get()
            try:
                if item is None:
                    return
                task_id, payload, config = item
                try:
                    self._deliver(task_id, payload, config)
                except Exception:  # noqa: BLE001 - 一条回调炸了不该让投递线程退出
                    self.log.exception("a2a 回调 %s 投递时出错", task_id)
            finally:
                self._push_q.task_done()

    def _deliver(self, task_id: str, payload: Dict[str, Any],
                 config: Dict[str, Any]) -> bool:
        """POST 整个 Task；失败按有界退避重试。返回是否送达。

        重试判据：连不上 / 超时 / 5xx / 429 值得再试；其余 4xx 是**对方明确拒收**
        （token 不认识、路径不对），重试只会把同一个拒绝重放几遍。
        """
        url = _as_text(config.get("url"))
        headers: Dict[str, str] = {}
        token = _as_text(config.get("token"))
        if token:
            headers["X-A2A-Notification-Token"] = token
        authorization = _as_text(config.get("authorization"))
        if authorization:
            headers["Authorization"] = authorization
        where = _redact(url)                  # 日志里不出现 path：token 通常就在里面
        last = ""
        for attempt in range(1, PUSH_TRIES + 1):
            try:
                status, _ = http_call(self._push_opener(), "POST", url, payload,
                                      headers=headers, timeout=PUSH_TIMEOUT,
                                      read_limit=PUSH_READ_LIMIT)
            except _TransportError as exc:
                last = str(exc)
            else:
                if 200 <= status < 300:
                    self.log.info("a2a 回调 %s → %s 已送达（HTTP %d，第 %d 次）",
                                  task_id, where, status, attempt)
                    return True
                last = f"HTTP {status}"
                if status < 500 and status != 429:
                    self._notice(f"回调 {where} 拒收任务 {task_id} 的结论（{last}）——"
                                 f"结论仍在 tasks/get 里")
                    return False
            if attempt < PUSH_TRIES:
                time.sleep(PUSH_BACKOFF[min(attempt - 1, len(PUSH_BACKOFF) - 1)])
        self._notice(f"回调 {where} 连不上，任务 {task_id} 的结论没送出去（{last}）——"
                     f"结论仍在 tasks/get 里")
        return False

    def _push_opener(self) -> urllib.request.OpenerDirector:
        """回调专用 opener：绕开环境代理，且**不跟随重定向**。

        跟随重定向等于把「POST 到 A」变成「POST 到 A 指定的任何地方」：地址本来
        就是调用方给的，再让它链式改一次，我们就成了替它打内网的跳板。3xx 在这里
        算一次投递失败（记日志），不是一次跳转。
        """
        if self._push_opener_obj is None:
            self._push_opener_obj = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect())
        return self._push_opener_obj

    def _push_stop(self) -> None:
        """叫停投递线程：先让它把队列里排着的送完（有上限），再退出。"""
        with self._push_lock:
            pusher, self._pusher = self._pusher, None
            self._push_stopped = True
            if pusher is not None:
                self._push_q.put(None)
        if pusher is not None:
            pusher.join(timeout=PUSH_STOP_GRACE)
            if pusher.is_alive():
                self.log.warning("a2a 回调线程没在 %s 秒内排空，剩下的不再重试",
                                 PUSH_STOP_GRACE)

    # --- 注册 ---------------------------------------------------------
    def _registrar_loop(self) -> None:
        backoff = 0
        while not self.stopping:
            try:
                self._registration_tick()
            except (_TransportError, A2aError, ValueError) as exc:
                wait = REGISTER_BACKOFF[min(backoff, len(REGISTER_BACKOFF) - 1)]
                backoff += 1
                self._notice(f"注册没成功（{exc}），{wait:.0f} 秒后重试")
            except Exception as exc:  # noqa: BLE001 - 单次异常不该让心跳线程静默退出
                # 这条兜底不是「宽容」而是「诚实」：心跳线程一死，registry 里那条记录
                # 就永远停在最后一次成功上 —— 一路 stale 到被人当尸体清掉，而日志里
                # 一个字都没有。宁可退避重试 + 记一条 log。
                wait = REGISTER_BACKOFF[min(backoff, len(REGISTER_BACKOFF) - 1)]
                backoff += 1
                self.log.exception("a2a 注册心跳出错：%s", exc)
            else:
                backoff = 0
                wait = REGISTER_INTERVAL
            self._stop.wait(wait)

    def _registration_tick(self) -> None:
        """一次「确认自己还在 registry 里」的检查；不在了就重新注册。

        启动**先 GET 再 POST**：ADP 对注册按来源 IP 限流 20 次/小时，一个秒退秒起
        的开发循环用「每次都 POST」能在半小时内把自己限死。
        """
        stored_id = _as_text(self._state.get("agent_id"))
        stored_url = _as_text(self._state.get("card_endpoint"))
        if stored_id and stored_url == self.card_endpoint:
            # 地址必须一起比：`id` 是**旧地址**派生的，改了端口 / advertise
            # 之后旧 id 一样 GET 回 200，于是我们高高兴兴跳过注册，而 registry 里
            # 那条记录指向一个已经不存在的地址 —— 探测一路失败到 down，新地址却
            # 从未注册。这个症状很难从面板上认出来（「它就在那儿，但是 down」）。
            status, body = self._call("GET", f"/v1/agents/{stored_id}")
            if status == 200:
                return
            if status != 404:
                raise _TransportError(f"GET /v1/agents/{stored_id} 回了 {status}：{body[:200]}")
        self._register()

    def _register(self) -> Dict[str, Any]:
        old_id = _as_text(self._state.get("agent_id"))
        old_url = _as_text(self._state.get("card_endpoint"))
        # 交的是**卡片地址**，不是卡片内容，也不是 RPC 端点：registry 会 GET 它来抓卡片。
        # 交 `/a2a` 会稳定地拿到 422 CARD_UNREACHABLE —— 那个路径只收 POST。
        status, body = self._call("POST", "/v1/agents", {"url": self.card_endpoint})
        if status not in (200, 201):
            raise _TransportError(f"POST /v1/agents 回了 {status}：{body[:200]}")
        doc = _as_dict(_loads(body))
        new_id = _as_text(doc.get("id"))
        if not new_id:
            raise _TransportError(f"POST /v1/agents 没给 id：{body[:200]}")
        token = _as_text(doc.get("manageToken"))
        # 真漂了就收尸：本进程换了地址，旧条目留着也只是一路 down。
        # 顺序是先拿到新 id 再删旧的 —— 反过来会丢掉「新注册失败时的退路」。
        if old_id and old_url and old_url != self.card_endpoint and self.mode == "auto":
            self._bury(old_id, old_url)
        updates: Dict[str, Any] = {"agent_id": new_id, "card_endpoint": self.card_endpoint}
        if token:
            updates["manage_token"] = token
        elif new_id != old_id:
            # 200 + manageToken: null 表示这个 URL 已经有人注册过，token 不轮换
            # （"先到先得"）。旧的 token 属于旧 id，留着只会误导下一次 DELETE。
            updates["manage_token"] = ""
        self._save_state(**updates)
        if new_id != old_id:
            self._notice(f"已在 registry 登记：{new_id}（{self.card_endpoint}）")
        return doc

    def _bury(self, old_id: str, old_url: str) -> None:
        """删掉上次留下的旧条目（只在自动模式发生：显式端口不漂）。"""
        if self._probe_alive(old_url):
            self._notice(f"旧条目 {old_id}（{old_url}）上还有服务在响应，保留不删")
            return
        token = _as_text(self._state.get("manage_token"))
        headers = {"X-ADP-Token": token} if token else {}
        try:
            status, body = self._call("DELETE", f"/v1/agents/{old_id}", headers=headers)
        except _TransportError as exc:
            self._notice(f"旧条目 {old_id} 未清理（registry 连不上：{exc}）")
            return
        if status in (200, 204, 404):
            self._notice(f"旧条目 {old_id} 已清理")
        elif status == 401:
            self._notice(f"旧条目 {old_id} 我们无权删除（token 不是首注册者的），"
                         f"可以在面板里手动删")
        else:
            self._notice(f"旧条目 {old_id} 未清理（DELETE 回了 {status}：{body[:120]}）")

    def _probe_alive(self, card_endpoint: str) -> bool:
        """旧卡片地址上还有活的东西吗？**拿到任何 HTTP 响应就不删** —— 那很可能是另一个
        openthree 实例（它注册的正是同一个 URL），删掉会毁掉一个活实例的注册记录，
        比留一条僵尸条目糟得多。"""
        try:
            http_call(self._http(), "GET", card_endpoint, timeout=5.0)
            return True
        except _TransportError:
            return False

    # --- registry 调用 -------------------------------------------------
    def _http(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            self._opener = _opener()
        return self._opener

    def _call(self, method: str, path: str,
              payload: Optional[Dict[str, Any]] = None, *,
              headers: Optional[Dict[str, str]] = None,
              timeout: float = 15.0) -> Tuple[int, str]:
        url = f"{self.registry}{path}"
        try:
            return http_call(self._http(), method, url, payload,
                             headers=headers, timeout=timeout)
        except _TransportError as exc:
            raise _TransportError(str(exc)) from exc

    # --- 小工具 -------------------------------------------------------
    def _notice(self, text: str) -> None:
        self.log.info("a2a: %s", text)
        try:
            print(f"[A2A:{self.name}] {text}", flush=True)
        except (BrokenPipeError, OSError, ValueError):
            # stdout 被掐断（`openthree -A2A | head -1`、daemon 没有终端）**不该**
            # 影响服务：这一句只是给人看的，服务照收任务。不在这里吞掉的话，异常会
            # 从 run() 里逃出去 —— 那时端口已经 bind 住了（有 finally 兜着，但频道会
            # 莫名其妙地「起不来」）。
            pass


def _loads(text: str) -> Any:
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


def _rpc_error(rid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": int(code), "message": str(message)}}


def _follow_up_prefix(task: Dict[str, Any]) -> str:
    """把同一 task 里此前的问答压成一段前缀（follow up 的输入）。"""
    lines: List[str] = []
    for item in list(task.get("history") or []):
        entry = _as_dict(item)
        text = message_text(entry)
        if not text:
            continue
        role = str(entry.get("role") or "").lower()
        who = "用户" if "user" in role else "你"
        lines.append(f"{who}：{text}")
    if not lines:
        return ""
    return "（这是同一个任务里的追加消息。此前往来：\n" + "\n".join(lines) + "\n）\n\n"


__all__ = [
    "A2aChannel", "A2aError", "build_card", "message_text", "http_call",
    "CARD_PATH", "RPC_PATH", "PROTOCOL_VERSION", "DEFAULT_REGISTRY", "DEFAULT_HOST",
    "PORT_RANGE", "MAX_BODY_BYTES", "ENV_REGISTRY",
]
