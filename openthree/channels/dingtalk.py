"""钉钉频道：机器人 Stream 模式收消息 + sessionWebhook 回复。

为什么用 Stream 模式（而不是 HTTP 回调）：它**不需要公网地址**，客户端主动连出去。
协议（对齐钉钉官方 `dingtalk-stream` SDK 与 qwenpaw 的 dingtalk 频道）：

  1. `POST https://api.dingtalk.com/v1.0/gateway/connections/open`
     body: `{clientId, clientSecret, subscriptions:[{type:"CALLBACK", topic:"/v1.0/im/bot/messages/get"}], ua, localIp}`
     回：`{endpoint, ticket}`
  2. 连 `wss://<endpoint>?ticket=<ticket>`；服务端推 JSON 帧
     `{type:"SYSTEM"|"CALLBACK", headers:{topic, messageId, ...}, data:"<json 字符串>"}`
  3. **必须在 3 秒内 ack**（否则钉钉重投），然后慢慢处理 → 用 sessionWebhook 回消息

于是「慢」这件事由智能体那边承担，钉钉侧只看到 ack。这和 qwenpaw 的处理方式一致。

**但「由智能体承担」不等于「对方什么都不该看见」**：一次任务可能跑十分钟，而聊天
窗口里那时一个像素都不会变 —— 发消息的人分不清「在排队」「在思考」和「进程死了」。
所以这个频道把状态显形成**一枚挂在用户那条消息上的「文字表情」**，随状态变化 ——
👀已接收 / ⏳排队中 / 🤔处理中·<正在做什么> <已耗时> / ✅完成 / ☹️出错 / 🚫已取消。
桥只有一条工作线程，`x` 这种串行是设计，不是故障 —— 所以「前面还有几条在排」也写在
那枚表情里（`⏳排队中·前面还有 2 条`）。

**状态只走表情，没有文字回执**：每条消息多一条与答复无关的聊天记录只是噪声，而
「排队第几位」「正在调哪个工具」表情那一格写得下。答复仍是单独一条消息（`send_text`），
与状态互不干扰。

表情走开放平台 `POST /v1.0/robot/emotion/reply|recall`（`emotionType=2` 的文字表情
能带一小段自定义文字，所以「在处理什么」写得下），令牌走 `/v1.0/oauth2/accessToken`。
两者都是**尽力而为**：应用没开机器人权限、令牌拿不到、网络抖动 —— 按消息节流地记
一句警告（同一条消息 `EMOTION_WARN_INTERVAL` 秒内只喊一次），答复照常发出，
一个字节都不受影响。

换表情的顺序是**先挂新的、挂成功了才摘旧的**。反过来（先摘后挂）时「摘成功、挂
失败」会让那条消息上一枚表情都不剩：删是真发生过的，而没挂上的那枚没有任何记录
指向「该补挂」—— 用户看到状态凭空消失，之后就什么都不再出现。先挂后摘的最坏结果
是**残留一枚旧状态**：看得见，而且下一次刷新就会纠正它。
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

from ._http import HttpError, post_json, post_json_status
from ._ws import WebSocket, WebSocketError
from .base import Channel, InboundMessage

OPEN_API = "https://api.dingtalk.com"
OPEN_CONNECTION_API = f"{OPEN_API}/v1.0/gateway/connections/open"
OAUTH_TOKEN_API = f"{OPEN_API}/v1.0/oauth2/accessToken"
EMOTION_REPLY_API = f"{OPEN_API}/v1.0/robot/emotion/reply"
EMOTION_RECALL_API = f"{OPEN_API}/v1.0/robot/emotion/recall"
BOT_TOPIC = "/v1.0/im/bot/messages/get"
TEXT_CHUNK = 4000
_AT_PREFIX = re.compile(r"^\s*@[^\s@]+\s*")

# 文字表情的内置 id 与背景：钉钉规定的一组常量，换掉它表情就渲染不出来。
TEXT_EMOTION_ID = "2659900"
EMOTION_BACKGROUND = "im_bg_1"

# 同一个状态「纯刷新」的最小间隔（秒）：长任务里耗时在走，但没必要每秒刷一次。
STATUS_MIN_INTERVAL = 20.0

# 同一条消息的表情失败最多多久喊一次（秒）。第一次必定喊；之后的失败要等这么久
# 才有下一条 —— 「整个进程只喊一次」会让后到的每一种丢失都静默（这类「表情凭空
# 消失」长期没被当成问题是它最直接的原因），而每次都喊则会用一个坏令牌刷满日志。
EMOTION_WARN_INTERVAL = 300.0

# 上面那张「按消息节流」的表最多记多少条（超过按时间淘汰最旧的）：防无界增长。
EMOTION_WARN_LIMIT = 200

# 状态 → 表情前缀 / 一句话。键集合就是 `Channel.notify()` 允许的全部状态。
STATUS_FACE = {
    "accepted": "👀",
    "queued": "⏳",
    "running": "🤔",
    "done": "✅",
    "error": "☹️",
    "canceled": "🚫",
    "skipped": "🚫",
}
STATUS_WORD = {
    "accepted": "已接收",
    "queued": "排队中",
    "running": "处理中",
    "done": "完成",
    "error": "出错",
    "canceled": "已取消",
    "skipped": "未处理",
}

#: 终态：到了这里这条消息就结束了，之后再来的过程态一律丢掉（见 `_Status.put`）。
_TERMINAL_STATES = ("done", "error", "canceled", "skipped")


def duration(secs: float) -> str:
    """把秒数说成 `45s` / `3m20s` / `1h05m` —— 表情上只有几十个字符的位置。"""
    total = int(max(0.0, float(secs or 0)))
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def local_ip() -> str:
    """本机出口 IP（钉钉只是拿它做统计，取不到就空着）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:  # noqa: BLE001
        return ""


def ack_payload(message_id: str, topic: str, *, code: int = 200,
                data: str = "{}") -> Dict[str, Any]:
    """Stream 的 ack 帧：3 秒内回它，否则钉钉会重投同一条消息。"""
    headers = {"contentType": "application/json"}
    if message_id:
        headers["messageId"] = message_id
    if topic:
        headers["topic"] = topic
    return {"code": code, "headers": headers, "message": "OK", "data": data}


def strip_at(text: str) -> str:
    """去掉群聊里 `@机器人` 的前缀（可能连着好几个）。"""
    prev = None
    while prev != text:
        prev = text
        text = _AT_PREFIX.sub("", text)
    return text.strip()


def extract_text(data: Dict[str, Any]) -> str:
    """把钉钉消息压成一段文本（媒体项给占位符，语音优先用它的识别文案）。"""
    msgtype = str(data.get("msgtype") or "").lower()
    if msgtype == "text":
        return str((data.get("text") or {}).get("content") or "").strip()
    if msgtype == "richText" or msgtype == "richtext":
        parts = []
        for item in (data.get("content") or {}).get("richText") or []:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts).strip()
    if msgtype in ("audio", "voice"):
        recognition = str((data.get("content") or {}).get("recognition") or "").strip()
        return recognition or "[语音]"
    if msgtype == "picture":
        return "[图片]"
    if msgtype == "video":
        return "[视频]"
    if msgtype == "file":
        name = str((data.get("content") or {}).get("fileName") or "").strip()
        return f"[文件] {name}".strip()
    return ""


def parse_callback_data(data: Dict[str, Any], *, channel: str = "",
                        require_mention: bool = True) -> Optional[InboundMessage]:
    """把一条钉钉机器人回调解析成 `InboundMessage`；该忽略的返回 None。"""
    if not isinstance(data, dict):
        return None
    kind = "group" if str(data.get("conversationType") or "") == "2" else "dm"
    if kind == "group" and require_mention and not data.get("isInAtList"):
        return None
    text = extract_text(data)
    if not text:
        return None
    if kind == "group":
        text = strip_at(text)
        if not text:
            return None
    sender_id = str(data.get("senderStaffId") or data.get("senderId") or "")
    return InboundMessage(
        text=text,
        channel=channel,
        channel_type="dingtalk",
        user_id=sender_id,
        user_name=str(data.get("senderNick") or sender_id),
        conversation_id=str(data.get("conversationId") or ""),
        kind=kind,
        msg_id=str(data.get("msgId") or ""),
        reply_token=str(data.get("sessionWebhook") or ""),
        raw=data,
    )


class DingTalkChannel(Channel):
    """钉钉机器人（Stream 模式）频道。"""

    type = "dingtalk"
    supports_status = True

    def __init__(self, name: str, options: Dict[str, Any], **kwargs: Any) -> None:
        super().__init__(name, options, **kwargs)
        self.client_id = self.option("client_id", "")
        self.client_secret = self.option("client_secret", "")
        self.require_mention = self.option_bool("require_mention", True)
        # 表情接口要的是「机器人编码」；内建机器人就是 AppKey，所以留空即取它。
        self.robot_code = self.option("robot_code", "") or self.client_id
        self.status_reactions = self.option_bool("status_reactions", True)
        self._ws: Optional[WebSocket] = None
        self._ws_lock = threading.Lock()
        self._ping_stop = threading.Event()
        self._token = ""
        self._token_exp = 0.0
        self._token_lock = threading.Lock()
        self._emotion_warned: Dict[str, float] = {}
        self._emotion_lock = threading.Lock()
        self._status = _Status(self)

    # --- 连接 ---------------------------------------------------------
    def open_connection(self) -> Dict[str, Any]:
        """换一个 Stream 接入点（endpoint + ticket）。"""
        payload = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "subscriptions": [{"type": "CALLBACK", "topic": BOT_TOPIC}],
            "ua": "openthree-channel/1.0",
        }
        ip = local_ip()
        if ip:
            payload["localIp"] = ip
        data = post_json(OPEN_CONNECTION_API, payload, timeout=15.0)
        if not isinstance(data, dict) or not data.get("endpoint") or not data.get("ticket"):
            raise RuntimeError(f"open connection 返回异常：{data}")
        return data

    def run(self) -> None:
        if not (self.client_id and self.client_secret):
            raise RuntimeError("缺少 client_id / client_secret，先跑 `openthree channel add`")
        backoff = 5.0
        while not self.stopping:
            try:
                conn = self.open_connection()
                uri = f"{conn['endpoint']}?ticket={quote_plus(str(conn['ticket']))}"
                ws = WebSocket(uri)
                ws.connect()
            except (HttpError, RuntimeError, WebSocketError, OSError) as exc:
                self.log.warning("连接钉钉失败，%.0f 秒后重试：%s", backoff, exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            backoff = 5.0
            with self._ws_lock:
                self._ws = ws
            self._notice("已连上钉钉 Stream，开始收消息")
            self._start_keepalive(ws)
            try:
                self._read_loop(ws)
            except WebSocketError as exc:
                self.log.warning("连接断开：%s", exc)
            finally:
                self._ping_stop.set()
                with self._ws_lock:
                    self._ws = None
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            if not self.stopping:
                self._stop.wait(3)

    def _read_loop(self, ws: WebSocket) -> None:
        while not self.stopping:
            raw = ws.recv_text()
            if raw is None:
                return
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                self.log.warning("收到非 JSON 帧，已忽略")
                continue
            try:
                self.handle_frame(ws, frame)
            except Exception as exc:  # noqa: BLE001 - 单帧出错不该拖垮连接
                self.log.warning("处理帧失败：%s", exc)

    def _start_keepalive(self, ws: WebSocket) -> None:
        """定期发 ping：让 NAT / 服务端的空闲连接不被掐断。"""
        self._ping_stop = threading.Event()
        stop = self._ping_stop

        def _loop() -> None:
            while not stop.wait(30.0):
                try:
                    ws.ping()
                except Exception:  # noqa: BLE001
                    return

        threading.Thread(target=_loop, name=f"channel-{self.name}-ping",
                         daemon=True).start()

    # --- 帧路由 -------------------------------------------------------
    def handle_frame(self, ws: WebSocket, frame: Dict[str, Any]) -> None:
        """一帧一条：SYSTEM（ping/disconnect）、CALLBACK（机器人消息）。

        **先 ack 再干活**：钉钉只等 3 秒，而智能体可能跑几分钟。
        """
        headers = frame.get("headers") or {}
        message_id = str(headers.get("messageId") or "")
        topic = str(headers.get("topic") or "")
        frame_type = str(frame.get("type") or "")

        if frame_type == "SYSTEM":
            ws.send_text(json.dumps(ack_payload(message_id, topic), ensure_ascii=False))
            if topic == "disconnect":
                raise WebSocketError("服务端要求断开重连")
            return

        if frame_type != "CALLBACK":
            return

        ws.send_text(json.dumps(ack_payload(message_id, topic), ensure_ascii=False))
        if topic != BOT_TOPIC:
            return
        data = frame.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                self.log.warning("回调 data 不是 JSON，已忽略")
                return
        if not isinstance(data, dict):
            return
        inbound = parse_callback_data(data, channel=self.name,
                                      require_mention=self.require_mention)
        if inbound is None:
            return
        if not self.allowed(inbound.user_id):
            self.log.info("忽略未授权的发送者：%s", inbound.user_id)
            return
        self.emit(inbound)

    # --- 发 -----------------------------------------------------------
    def send_text(self, msg: InboundMessage, text: str) -> bool:
        """把一段文本发回会话；**明确没送到时返回 False**（桥据此不标 ✅）。

        「没送到」包括：没有 sessionWebhook、webhook 过期（42001 / 400）、
        非 2xx。空文本不算失败（本来就没有要发的东西）。
        """
        webhook = msg.reply_token
        if not webhook:
            self.log.warning("没有 sessionWebhook，无法回复 %s", msg.user_id)
            return False
        body_text = str(text or "").strip()
        if not body_text:
            return True
        for start in range(0, len(body_text), TEXT_CHUNK):
            chunk = body_text[start:start + TEXT_CHUNK]
            status, body = post_json_status(
                webhook, {"msgtype": "text", "text": {"content": chunk}})
            if status in (200, 201, 204):
                continue
            if status in (42001, 400):
                self.log.warning("sessionWebhook 已过期，这条回复发不出去：%s", body[:200])
                return False
            self.log.warning("发送失败（HTTP %s）：%s", status, body[:200])
            return False
        return True

    def stop(self) -> None:
        self._stop.set()
        self._ping_stop.set()
        # **不停**状态工作线程：收工顺序是「先停频道、再停桥」，桥会把手头那条跑完
        # 并把终态发出来；在这儿停掉，那条消息就会永远停在 🤔，而答案其实送到了。
        # 它是守护线程，进程退出时自然结束。
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # --- 状态显形 -----------------------------------------------------
    def notify(self, msg: InboundMessage, state: str, detail: str = "") -> None:
        """桥告诉我们「这条消息现在到哪一步了」—— 只入队，立刻返回。

        真正的请求（那一枚表情）由 `_Status` 的后台线程发：调用这里的是收消息线程和
        ReAct 线程，谁都不该为「亮个灯」等一次网络往返。
        """
        if state not in STATUS_FACE:
            return
        try:
            self._status.put(msg, state, detail)
        except Exception:  # noqa: BLE001 - 亮灯出问题绝不能影响收消息
            pass

    def progress_for(self, msg: InboundMessage) -> Optional[Any]:
        """订阅 ReAct 的步级回声（第几步、在调什么工具、等了多久）。"""
        if not self.status_reactions:
            return None
        return _StepProgress(self._status, msg)

    # --- 开放平台：令牌与表情 -----------------------------------------
    def access_token(self) -> str:
        """应用级 accessToken（缓存到过期前一分钟）；拿不到就抛。"""
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_exp:
                return self._token
            data = post_json(OAUTH_TOKEN_API,
                             {"appKey": self.client_id, "appSecret": self.client_secret},
                             timeout=10.0)
            token = str((data or {}).get("accessToken") or "")
            if not token:
                raise RuntimeError(f"accessToken 返回异常：{data}")
            ttl = float((data or {}).get("expireIn") or 7200)
            self._token = token
            self._token_exp = now + max(60.0, ttl - 60.0)
            return token

    def send_emotion(self, msg: InboundMessage, name: str, *, recall: bool = False) -> bool:
        """给 `msg` 那条消息挂 / 摘一枚文字表情；成功 True，失败 False（从不抛）。

        「不抛」是这里的关键：表情只是旁证，应用没开机器人权限、令牌过期、
        网络抖动都不该让一条消息跑不通。失败**要说真话**，但同一条消息
        `EMOTION_WARN_INTERVAL` 秒内只喊一次（`_warn_emotion`）—— 一个坏令牌不该
        刷满日志，可「整个进程只喊一次」又会让后来的每一种丢失都静默。
        """
        if not self.status_reactions:
            return False
        msg_id = str(msg.msg_id or "")
        conv_id = str(msg.conversation_id or "")
        if not (msg_id and conv_id and self.robot_code):
            return False
        body = {
            "robotCode": self.robot_code,
            "openMsgId": msg_id,
            "openConversationId": conv_id,
            "emotionType": 2,
            "emotionName": name,
            "textEmotion": {
                "emotionId": TEXT_EMOTION_ID,
                "emotionName": name,
                "text": name,
                "backgroundId": EMOTION_BACKGROUND,
            },
        }
        try:
            post_json(EMOTION_RECALL_API if recall else EMOTION_REPLY_API, body,
                      headers={"x-acs-dingtalk-access-token": self.access_token()},
                      timeout=8.0)
            return True
        except Exception as exc:  # noqa: BLE001 - 表情失败只是看不见状态
            self._warn_emotion(msg_id, exc, recall=recall)
            return False

    def can_react(self, msg: InboundMessage) -> bool:
        """这条消息**在原理上**能不能挂表情（开关打开、机器人码与两个 id 齐）。

        与 `send_emotion` 的返回值别混：那里的 False 既可能是「试了但失败」，也可能是
        「压根没试」（关掉了、缺 id）。「没挂上」这句真话只在真试过时才有意义 ——
        一个关掉的功能不会「丢」状态，把「没开」报成「丢了」只会教人忽略这条警告。
        """
        return bool(self.status_reactions and self.robot_code
                    and str(msg.msg_id or "") and str(msg.conversation_id or ""))

    def _warn_emotion(self, msg_id: str, exc: BaseException, *, recall: bool = False) -> None:
        """记一条**带消息 id** 的真话：同一条消息 `EMOTION_WARN_INTERVAL` 秒内一次。

        为什么要带上 `msgId`：一句不带现场的通告无法回答「哪条消息丢状态了、
        丢的是挂还是摘」。为什么要按消息节流：令牌坏掉时每条消息都会失败，
        全局不节流会把日志刷满；而「整个进程只喊一次」则让第一次之后的每一种
        丢失都静默 —— 那正是「表情凭空消失」长期没被当成问题的原因。
        """
        now = time.time()
        key = msg_id or "-"
        with self._emotion_lock:
            last = self._emotion_warned.get(key)
            if last is not None and now - last < EMOTION_WARN_INTERVAL:
                return
            self._emotion_warned[key] = now
            excess = len(self._emotion_warned) - EMOTION_WARN_LIMIT
            if excess > 0:
                for old, _ in sorted(self._emotion_warned.items(),
                                     key=lambda kv: kv[1])[:excess]:
                    self._emotion_warned.pop(old, None)
        self.log.warning("表情%s没发出去（消息 %s，同一条消息 %.0f 秒内不再重复）：%s",
                         "摘不下" if recall else "挂不上", key, EMOTION_WARN_INTERVAL, exc)

    # --- 内部 ---------------------------------------------------------
    def _notice(self, text: str) -> None:
        self.log.info("%s", text)
        print(f"[频道:{self.name}] {text}", flush=True)


#: 表情文字的宽度上限：钉钉挂得下的位置就这么多，超了宁可截断也不能整条丢掉。
FACE_LIMIT = 24


def _clip(text: str, limit: int = FACE_LIMIT) -> str:
    """把一段文字裁到 `limit` 以内（含省略号）。"""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


class _Status:
    """把「状态」变成钉钉里看得见的变化：一枚挂在消息上的当前状态表情。

    为什么要有后台线程：`notify()` 是从收消息线程与 ReAct 线程里调到的，一次 HTTP
    往返（~200ms）会直接拖慢 ack 与下一步推理。所以这里只入队，真请求交给一条守护
    线程发；同一个 msgId 的多次刷新合并成最新那一次，并按 `STATUS_MIN_INTERVAL`
    节流 —— 长任务里唯一能证明「还活着」的是耗时在往前走，但也没必要每秒刷一次。

    一个 msgId 到达终态后留一个**墓碑**（`_finished`）：那一轮的心跳线程可能还没睡醒，
    它晚到的 `idle` 不该把同一条消息重新当成「刚开始处理」—— 那会把已经 ✅ 的消息
    又点亮成 🤔。墓碑按时间淘汰，所以它不会无界增长。

    这个线程**不随 `channel.stop()` 停**：收工顺序是「先停频道、再停桥」，而桥会把
    手头那条跑完并把终态发出来 —— 在这里提前停掉，那条消息就永远停在 🤔，而答案
    其实已经送到了。它是守护线程，进程退出时自然结束。
    """

    #: 墓碑最多记多少条（超了按时间淘汰最旧的）：一条消息一个键，几百条够用。
    FINISHED_LIMIT = 500

    #: 每条消息最多记「还没确认摘掉」的几枚表情。正常情况下一两枚就够；
    #: 这个上限防的是「摘一直失败而挂一直成功」时列表无界增长。
    FACE_MEMORY = 3

    #: 一枚表情没挂上之后，最多**立即**补挂几次（不看节流）。超过就回到
    #: `STATUS_MIN_INTERVAL` 的正常节奏 —— 令牌 10s + 表情 8s 的超时会把这条唯一
    #: 的后台线程堵住，而所有消息的亮灯都排在它后面。
    RETRY_LIMIT = 1

    def __init__(self, channel: "DingTalkChannel") -> None:
        self.channel = channel
        self._cv = threading.Condition()
        self._pending: Dict[str, Any] = {}
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._finished: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None

    # --- 入口 ---------------------------------------------------------
    def put(self, msg: InboundMessage, state: str, detail: str = "") -> None:
        """记下最新状态并唤醒工作线程；同一个 msgId 只保留最新那条。

        已经收尾的消息，只接受**终态**（重复的 `done` 无副作用），过程态一律丢：
        晚到的心跳不该把一条已完成的消息重新点亮。
        """
        key = _msg_key(msg)
        terminal = state in _TERMINAL_STATES
        with self._cv:
            if not terminal and key in self._finished:
                return
            queued = self._pending.get(key)
            if not terminal and queued is not None and queued[1] in _TERMINAL_STATES:
                return          # 队里已经躺着一个终态：别用过程态把它顶掉
            self._pending[key] = (msg, state, detail)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._loop, name=f"channel-{self.channel.name}-status",
                    daemon=True)
                self._thread.start()
            self._cv.notify()

    # --- 工作线程 -----------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait(0.5)
                items = list(self._pending.items())
                self._pending.clear()
            for key, (msg, state, detail) in items:
                try:
                    self._apply(key, msg, state, detail)
                except Exception:  # noqa: BLE001 - 一个状态画不出来，不该拖死这条线程
                    pass

    def _apply(self, key: str, msg: InboundMessage, state: str, detail: str) -> None:
        now = time.time()
        # 出队时**再查一次**墓碑：入队那一刻墓碑可能还没落（终态正在发两枚表情，
        # 各自可能等 8 秒），于是晚到的心跳挤了进来 —— 这里把它挡掉。
        if state not in _TERMINAL_STATES and key in self._finished:
            return
        info = self._seen.get(key)
        if info is None:
            info = {"at": now, "faces": [], "state": "", "detail": "", "sent": 0.0,
                    "lost": False, "retry": 0}
            self._seen[key] = info
        face = _face(state, detail, now - info["at"]) if self.channel.can_react(msg) else ""
        if face:
            # **只有「同一件事的纯刷新」（耗时在走）才节流**；换了细节就立刻发 ——
            # 否则第一步的「调用 run_shell」会被「处理中 0s」压住二十秒，
            # 而那正是这次要治的症状。上一枚没挂上时也放行：一条消息上一枚表情都
            # 没有是**看不见**的错，趁早补挂；放行次数由 `RETRY_LIMIT` 封顶。
            same_event = (state, detail) == (info["state"], info["detail"])
            retry = bool(info["lost"]) and info["retry"] <= self.RETRY_LIMIT
            if not same_event or retry or now - info["sent"] >= STATUS_MIN_INTERVAL:
                self._draw(msg, info, face, state, detail, now)
        if state in _TERMINAL_STATES:
            # 两件事都值得说真话，而且**此刻不说就再没机会**：墓碑会挡住之后所有
            # 过程态 `put`，`_seen` 这条记录下面就要 pop，没有任何后续刷新能纠正。
            #   * 没挂上 —— 用户看到的最后一枚不是终态（甚至一枚都没有）；
            #   * 没摘掉 —— 旧表情与 ✅ 并排，看起来像「完成了却还在跑」。
            missing = bool(face) and face not in info["faces"]
            # 只算**旧**表情：刚挂上的那一枚终态表情（✅ / ☹️）本来就该留在消息上，
            # 把它也算成「没摘掉」，这条警告就会在每一次正常收工时报一次反话。
            stale = [f for f in info["faces"] if f != face]
            if missing or stale:
                bits = []
                if missing:
                    bits.append(f"终态表情「{face}」没挂上")
                    if not info["faces"]:
                        bits.append("消息上现在一枚状态表情都没有")
                if stale:
                    bits.append("旧表情没摘掉、会留在消息上：" + "、".join(stale))
                self.channel.log.warning(
                    "状态没能在消息 %s 上收尾：%s（这条消息之后不再刷新）",
                    key, "；".join(bits))
            self._seen.pop(key, None)
            self._finished[key] = now            # 墓碑：挡住晚到的心跳
            self._prune_finished()

    def _draw(self, msg: InboundMessage, info: Dict[str, Any], face: str,
              state: str, detail: str, now: float) -> None:
        """把 `face` 画到消息上：**先挂新的，挂上了才摘旧的**。

        这个顺序是这里唯一不能反的东西。反过来（先摘后挂）时，「摘成功、挂失败」
        会让消息上一枚表情都不剩：摘是真发生过的（`info["faces"]` 也跟着删了），
        而没挂上的那枚没有任何记账指向「该补挂」—— 用户看到状态凭空消失，
        之后连终态也不出现，日志里也只有第一条失败（`_warn_emotion` 按消息节流）。
        先挂后摘的最坏结果是残留一枚旧表情：看得见，下一次刷新就会纠正。
        """
        attached = face in info["faces"]
        if not attached:
            attached = self.channel.send_emotion(msg, face)
            if attached:
                info["faces"].append(face)
                info["lost"] = False
                info["retry"] = 0
            else:
                # 没挂上：表情列表原样留着（它们确实还在消息上），并记下「该补挂」。
                info["lost"] = True
                info["retry"] += 1
        if attached:
            for old in [f for f in info["faces"] if f != face]:
                # **摘失败的那枚留在列表里**：下一次刷新会再摘一次 —— 否则它就一直
                # 留在消息上，谁也不记得去碰它。
                if self.channel.send_emotion(msg, old, recall=True):
                    info["faces"].remove(old)
            # 列表只留最近几枚（`FACE_MEMORY`）：万一「摘」一直失败而「挂」一直成功，
            # 不至于每刷新一次就多攒一枚、每次都去试一遍 8 秒。
            # **裁剪必须在摘除循环之后**：先裁会把本轮本来能摘掉的旧表情提前丢掉，
            # 从此再没有一次刷新去碰它，终态的 `stale` 也数不到它 —— 它就永久留在
            # 消息上，日志里一个字都没有。
            del info["faces"][:-self.FACE_MEMORY]
        # 记账**与这次成败无关**：失败也推进 `state` / `detail` / `sent`。
        # 否则 `state` 永远停在 ""，节流闸门永远不生效 —— 此后每个进度事件都会
        # 再打一轮令牌 + 表情请求。补挂由 `lost` / `retry` 单独负责，且有上限。
        info.update(state=state, detail=detail, sent=now)

    def _prune_finished(self) -> None:
        """墓碑只用来挡「晚到的心跳」，留最近的若干条就够（按时间淘汰最旧的）。"""
        excess = len(self._finished) - self.FINISHED_LIMIT
        if excess <= 0:
            return
        for key, _ in sorted(self._finished.items(), key=lambda kv: kv[1])[:excess]:
            self._finished.pop(key, None)


def _msg_key(msg: InboundMessage) -> str:
    """一条消息一个键。

    有 `msgId` 就用它（表情接口要的正是它）。没有时用**对象身份**兜底 ——
    同一对话里两条消息若共用一格，前一条的墓碑会把后一条的表情状态一起挡掉。
    """
    return str(msg.msg_id or "") or f"{msg.conversation_id}|{msg.user_id}|{id(msg)}"


def _face(state: str, detail: str, elapsed: float) -> str:
    """状态 + 细节 → 一枚表情的文字（例如 `🤔调用 run_shell 1m20s`）。"""
    face = STATUS_FACE.get(state, "")
    word = STATUS_WORD.get(state, state)
    detail = str(detail or "").strip()
    if state == "running":
        # 「在处理什么」比「处理中」有用得多：看得见 run_shell，就知道不是卡死；
        # 耗时一直在走，就是「还活着」。**耗时必须活过截断**，所以先按剩余宽度
        # 裁细节、再拼耗时 —— 反过来的话，长工具名会把「还活着」那一半裁掉。
        tail = f" {duration(elapsed)}"
        return _clip(f"{face}{detail or word}", max(1, FACE_LIMIT - len(tail))) + tail
    if detail:
        return _clip(f"{face}{word}·{detail}")
    return f"{face}{word}"


class _StepProgress:
    """订阅 ReAct 的进度回声，把「正在做什么」写到那枚表情上。

    只实现关心的方法（协议见 `openthree/progress.py`）：`idle` 是长等待里的心跳，
    它让表情上的耗时一直往前走 —— 于是一个跑十分钟的任务在对方眼里是「还在动」，
    而不是「没反应」。
    """

    def __init__(self, status: _Status, msg: InboundMessage) -> None:
        self._status = status
        self._msg = msg
        self._activity = ""

    def step(self, iteration: Any = None, max_iterations: Any = None) -> None:
        self._activity = f"第{iteration}步"
        self._status.put(self._msg, "running", self._activity)

    def llm_start(self, model: Any = "") -> None:
        self._activity = "思考中"
        self._status.put(self._msg, "running", self._activity)

    def tool_start(self, name: Any = "", args: Any = None) -> None:
        self._activity = f"调用 {name}"
        self._status.put(self._msg, "running", self._activity)

    def idle(self, phase: Any = "", waited: Any = 0.0) -> None:
        """心跳只让耗时往前走，**活动描述仍用最后一次真事件**。

        直接写 `phase` 会把界面倒退：`Progress` 只等心跳线程 0.3 秒，一枚卡在
        `_say()` 里的心跳醒来时带的是**旧阶段名**（例如「等待模型返回」），照着写
        就把 `tool_start` 刚写下的「调用 run_shell」覆盖掉 —— 看起来正好像卡在
        上一步。A2A 的 `_Echo.idle` 用的是同一条纪律。
        """
        self._status.put(self._msg, "running",
                         self._activity or str(phase or "处理中"))
