"""频道基类：一条进来的消息长什么样、一个频道要实现哪几个方法。

判据只有一句话：**频道只负责「收」与「发」，不负责思考**。
收到的每条消息变成 `InboundMessage` 交给回调，回调（`AgentBridge`）再去跑
`Runtime.run()` —— 与在 `-i` 里敲一行字走的是同一条路，所以内核不必知道
「频道」这回事。回复同样以纯文本回给 `send_text()`。

于是加一个新渠道 = 写一个 `Channel` 子类 + 在 `config._SPECS` 里登记它的配置项，
不用碰 agent / runtime / llm 任何一行。
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class InboundMessage:
    """一条从渠道来的消息 —— 渠道层与智能体层之间的唯一契约。"""

    text: str
    channel: str = ""            # 配置里的频道名（多频道时用来区分是谁收的）
    channel_type: str = ""       # wechat / dingtalk
    user_id: str = ""
    user_name: str = ""
    conversation_id: str = ""
    kind: str = "dm"             # dm（单聊）/ group（群聊）
    msg_id: str = ""
    reply_token: str = ""        # wechat: context_token；dingtalk: sessionWebhook
    # 随消息一起来的图片：本地绝对路径或 http(s) 链接，原样交给
    # `Runtime.run(images=…)`（与 CLI `--image` 同一条路）。
    # 有默认值 ⇒ 微信 / 钉钉 / 既有构造点零改动。
    images: List[str] = field(default_factory=list)
    # 本轮答复要带回去的文件：`{"path": 本机路径, "name": 对方看到的文件名}`。
    # 由 `attach_file` 往里追加，频道在回程取走并做**全部**安全校验（路径白名单
    # 只在频道层有一份 —— 用户态工具绕不过它）。与 `images` 同构：有默认值 ⇒
    # 微信 / 钉钉 / 既有构造点零改动。
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    # 本次运行的取消令牌（`openthree.cancel.CancelToken`）。频道把它建在「一次
    # 运行」上，桥原样交给 `Runtime.run(cancel=…)` —— 频道不认识 ReAct，只是
    # 置了自己那一个事件。默认 None ⇒ 微信 / 钉钉 / 既有构造点零改动。
    cancel: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        """给人看的一行来源标注（会被拼进喂给智能体的那段文本）。"""
        who = self.user_name or self.user_id or "未知用户"
        where = {"wechat": "微信", "dingtalk": "钉钉", "a2a": "A2A"}.get(
            self.channel_type, self.channel_type or "频道")
        tag = self.channel or where
        return f"{where}[{tag}] {who}" if self.channel else f"{where} {who}"


# 频道把消息交给谁：由 manager 注入（一般就是 AgentBridge.submit）
OnMessage = Callable[[InboundMessage], None]


class Channel(ABC):
    """一个频道：`run()` 阻塞地把消息抽进来，`send_text()` 把回答送出去。

    子类必须实现 `run()` / `send_text()` / `stop()`；`run()` 里的循环要盯着
    `self._stop`，这样 `stop()` 能在几秒内让它退出。异常一律由 manager 兜住，
    所以 `run()` 内部不必自己重连（但**应该**自己重连 —— 网络掉线不该让频道彻底死掉）。
    """

    type: str = ""
    # 这个频道能不能把文件送回对方。默认不能：桥只在它为真时才把「当前回合」
    # 发布给 `attach_file`，于是微信 / 钉钉回合里调到那个工具时，它会老实回
    # 「这次答复发不出文件」—— 而不是假装附上了。
    supports_files: bool = False
    # 这个频道能不能把「收到了 / 在排队 / 正处理 / 已完成」显形给对方。默认不能：
    # 桥于是既不调 `notify()`，也不给它造进度观察者 —— 微信等频道行为一个字节不变。
    supports_status: bool = False

    def __init__(self, name: str, options: Dict[str, Any], *,
                 on_message: Optional[OnMessage] = None,
                 logger: Optional[logging.Logger] = None,
                 workspace_dir: Optional[Any] = None) -> None:
        self.name = name
        self.options = dict(options or {})
        self._on_message = on_message
        self.log = logger or logging.getLogger(f"openthree.channel.{name}")
        self._stop = threading.Event()
        # 频道自己的落盘目录（token / 二维码 / 游标）：`.openthree/channels/`
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None

    # --- 生命周期 -----------------------------------------------------
    @abstractmethod
    def run(self) -> None:
        """阻塞运行：收消息 → `self._emit(...)`，直到 `stop()` 或不可恢复的错误。"""

    @abstractmethod
    def stop(self) -> None:
        """请求退出（尽量几秒内生效；重复调用必须安全）。"""

    @abstractmethod
    def send_text(self, msg: InboundMessage, text: str) -> Any:
        """把一段纯文本回复发回 `msg` 的来源会话。

        返回值可选但有用：**显式 `False` 表示「明确没送到」**（webhook 过期、
        非 2xx），桥据此把终态记成「答复没发出去」而不是 ✅。`None` / `True`
        一律当作「送到了，或者这个渠道没法判断」—— 于是既有实现不改也不受影响。
        """

    # --- 工具 ---------------------------------------------------------
    def emit(self, msg: InboundMessage) -> None:
        msg.channel = msg.channel or self.name
        msg.channel_type = msg.channel_type or self.type
        if self._on_message is not None:
            self._on_message(msg)

    # --- 状态显形（可选）---------------------------------------------
    def notify(self, msg: InboundMessage, state: str, detail: str = "") -> None:
        """把一条消息的生命周期状态告诉频道 —— 默认什么都不做。

        `state` 只有这几个值，桥是唯一来源：

            accepted  已收到（还没开跑）
            queued    排在别的消息后面（`detail` 说前面还有几条）
            running   开始处理
            done      处理完了（答案已经送出）
            error     这条没跑通
            canceled  调用方取消了 / 收工时还排在队里
            skipped   压根没开跑（正在退出）

        **必须立刻返回**：调用它的是收消息线程与 ReAct 线程，谁都不该为「亮个灯」
        等一次网络往返。要发请求就自己排队交给后台线程。
        """

    def progress_for(self, msg: InboundMessage) -> Optional[Any]:
        """返回一个订阅 `Progress` 事件的观察者；默认 None = 不订阅。

        观察者协议见 `openthree/progress.py`：方法名与 `Progress` 同名同参
        （`step` / `llm_start` / `llm_done` / `thought` / `tool_start` /
        `tool_done` / `note` / `idle`），只实现关心的那几个即可。
        它拿到的是**步级**回声（第几步、正在调哪个工具、等了多久），
        与 `notify()` 的生命周期状态互补 —— **终态只听 `notify()`**：一个
        `Progress` 是一轮的（多轮任务里会有 N 个），它的结束不是任务的结束。
        """
        return None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def option(self, key: str, default: str = "") -> str:
        value = self.options.get(key, "")
        return default if value is None or str(value).strip() == "" else str(value)

    def option_bool(self, key: str, default: bool = False) -> bool:
        raw = str(self.options.get(key, "")).strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "y", "on", "是", "开")

    def allow_from(self) -> list[str]:
        raw = self.option("allow_from", "")
        return [x.strip() for x in str(raw).replace("，", ",").split(",") if x.strip()]

    def allowed(self, user_id: str) -> bool:
        """白名单：留空 = 全放行（单机自用最常见）。"""
        allow = self.allow_from()
        return not allow or user_id in allow

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} {self.name}>"
