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
"""
from __future__ import annotations

import json
import re
import socket
import threading
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

from ._http import HttpError, post_json, post_json_status
from ._ws import WebSocket, WebSocketError
from .base import Channel, InboundMessage

OPEN_API = "https://api.dingtalk.com"
OPEN_CONNECTION_API = f"{OPEN_API}/v1.0/gateway/connections/open"
BOT_TOPIC = "/v1.0/im/bot/messages/get"
TEXT_CHUNK = 4000
_AT_PREFIX = re.compile(r"^\s*@[^\s@]+\s*")


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

    def __init__(self, name: str, options: Dict[str, Any], **kwargs: Any) -> None:
        super().__init__(name, options, **kwargs)
        self.client_id = self.option("client_id", "")
        self.client_secret = self.option("client_secret", "")
        self.require_mention = self.option_bool("require_mention", True)
        self._ws: Optional[WebSocket] = None
        self._ws_lock = threading.Lock()
        self._ping_stop = threading.Event()

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
    def send_text(self, msg: InboundMessage, text: str) -> None:
        webhook = msg.reply_token
        if not webhook:
            self.log.warning("没有 sessionWebhook，无法回复 %s", msg.user_id)
            return
        body_text = str(text or "").strip()
        if not body_text:
            return
        for start in range(0, len(body_text), TEXT_CHUNK):
            chunk = body_text[start:start + TEXT_CHUNK]
            status, body = post_json_status(
                webhook, {"msgtype": "text", "text": {"content": chunk}})
            if status in (200, 201, 204):
                continue
            if status in (42001, 400):
                self.log.warning("sessionWebhook 已过期，这条回复发不出去：%s", body[:200])
                return
            self.log.warning("发送失败（HTTP %s）：%s", status, body[:200])
            return

    def stop(self) -> None:
        self._stop.set()
        self._ping_stop.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # --- 内部 ---------------------------------------------------------
    def _notice(self, text: str) -> None:
        self.log.info("%s", text)
        print(f"[频道:{self.name}] {text}", flush=True)
