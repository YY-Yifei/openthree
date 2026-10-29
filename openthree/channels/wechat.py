"""微信频道：iLink Bot HTTP API（扫码登录 → 长轮询收 → HTTP 回）。

协议（对齐 qwenpaw 的 wechat 频道）：
  * 登录：`GET ilink/bot/get_bot_qrcode?bot_type=3` 拿二维码 → 轮询
    `GET ilink/bot/get_qrcode_status?qrcode=…` 直到 `confirmed`，拿 `bot_token` 与 `baseurl`
  * 收消息：`POST ilink/bot/getupdates`（服务端最多挂 35 秒，`get_updates_buf` 当游标）
  * 发消息：`POST ilink/bot/sendmessage`（要带回消息里的 `context_token`）

所以它**不需要公网地址**，只要能出网 —— 这也是选它而不是公众号回调的原因。

登录用的二维码**直接画在控制台上**，手机对着终端就能扫 —— 服务端给的
`qrcode_img_content` 既可能是 base64 的 PNG，也可能只是一段确认页 URL，
两种形态都由 `_qr` 统一成模块矩阵；后者需要本地编码（`_qrencode`）。
PNG 同时落在 `.openthree/channels/<频道名>-qrcode.png`，事后重扫 / 转发都用得上。

纯标准库实现，不依赖任何第三方 SDK；token 与游标落在
`.openthree/channels/<频道名>.json`，重启不重复扫码。
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._http import HttpError, get_json, post_json
from .base import Channel, InboundMessage
from . import _qr

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.0.1"
GETUPDATES_TIMEOUT = 45.0      # 服务端长轮询上限 35s，留点余量
QRCODE_STATUS_TIMEOUT = 60.0   # 扫码状态轮询本身也是长轮询
DEFAULT_TIMEOUT = 15.0
MAX_SEEN = 2000                # 去重集合上限（够覆盖一次重连的重复投递）
TEXT_DEDUP_TTL = 30.0          # 同一用户 + 同一文本 30 秒内只处理一次
MAX_TEXT_CHUNK = 1500          # 单条回复的最大字符数，长回答分多条发

_ITEM_PLACEHOLDER = {
    "image_item": "[图片]",
    "voice_item": "[语音]",
    "video_item": "[视频]",
    "file_item": "[文件]",
    "emoji_item": "[表情]",
}


def _as_int(value: Any, default: int = 0) -> int:
    """把服务端回来的任意值读成 int。

    一条消息不该因为某个字段是 `"abc"` 就让整个频道停摆：服务端字段类型偶尔会变，
    用户也可能手改 token 文件。读不懂就给默认值，让上层决定「跳过这一条」。
    """
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> Dict[str, Any]:
    """非 dict 一律当空 dict —— `item_list` 里的 `text_item` 未必是对象。"""
    return value if isinstance(value, dict) else {}


def make_headers(bot_token: str = "") -> Dict[str, str]:
    """iLink 请求头：`X-WECHAT-UIN` 是一个随机数（防重放），有 token 就带 Bearer。"""
    uin = base64.b64encode(str(secrets.randbelow(0xFFFFFFFF)).encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
    }
    if bot_token:
        headers["Authorization"] = f"Bearer {bot_token}"
    return headers


def extract_text(item_list: List[Dict[str, Any]]) -> str:
    """把 `item_list` 压成一段文本：文本项直取，媒体项给占位符。

    只做「够用」的提取 —— 频道这边不解析图片内容；智能体要图片可以自己拿 URL。
    语音项如果自带 ASR 文本（`voice_item.text_item.text`）就用它，比占位符有用得多。
    """
    parts: List[str] = []
    for item in (item_list if isinstance(item_list, list) else []):
        if not isinstance(item, dict):
            continue
        text_item = _as_dict(item.get("text_item"))
        if text_item and _as_int(item.get("type"), 1) == 1:
            text = str(text_item.get("text") or "").strip()
            if text:
                parts.append(text)
            continue
        voice = _as_dict(item.get("voice_item"))
        if voice:
            asr = str(_as_dict(voice.get("text_item")).get("text") or "").strip()
            parts.append(asr or "[语音]")
            continue
        file_item = _as_dict(item.get("file_item"))
        if file_item:
            name = str(file_item.get("file_name") or "").strip()
            parts.append(f"[文件] {name}".strip())
            continue
        for key, placeholder in _ITEM_PLACEHOLDER.items():
            if item.get(key):
                parts.append(placeholder)
                break
    return "\n".join(p for p in parts if p).strip()


def parse_inbound(msg: Dict[str, Any], *, channel: str = "") -> Optional[InboundMessage]:
    """把一条 iLink 消息解析成 `InboundMessage`；不是「用户发给机器人」的返回 None。"""
    if not isinstance(msg, dict):
        return None
    if _as_int(msg.get("message_type")) != 1:    # 1 = 用户 → 机器人
        return None
    text = extract_text(msg.get("item_list") or [])
    if not text:
        return None
    from_user = str(msg.get("from_user_id") or "")
    return InboundMessage(
        text=text,
        channel=channel,
        channel_type="wechat",
        user_id=from_user,
        user_name=str(msg.get("from_user_nickname") or from_user),
        conversation_id=str(msg.get("group_id") or from_user),
        kind="group" if msg.get("group_id") else "dm",
        msg_id=str(msg.get("msg_id") or msg.get("context_token") or ""),
        reply_token=str(msg.get("context_token") or ""),
        raw=msg,
    )


def split_text(text: str, limit: int = MAX_TEXT_CHUNK) -> List[str]:
    """按行切分长文本（尽量不把一句话劈开），每段不超过 limit。"""
    text = str(text or "")
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:               # 单行就超长：硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c]


class WeChatChannel(Channel):
    """微信 iLink Bot 频道。"""

    type = "wechat"

    def __init__(self, name: str, options: Dict[str, Any], **kwargs: Any) -> None:
        super().__init__(name, options, **kwargs)
        self._state_path: Optional[Path] = None
        self._state: Dict[str, Any] = {}
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._text_seen: "OrderedDict[str, float]" = OrderedDict()
        self._state_lock = threading.Lock()
        self.login_timeout = float(self.option("login_timeout", "300") or 300)

    # --- 配置 / 状态 ---------------------------------------------------
    @property
    def base_url(self) -> str:
        return (self._state.get("base_url") or self.option("base_url", DEFAULT_BASE_URL)
                or DEFAULT_BASE_URL).rstrip("/")

    @property
    def bot_token(self) -> str:
        return str(self._state.get("bot_token") or self.option("bot_token", "") or "")

    def _default_state_path(self) -> Path:
        root = self.workspace_dir or Path(".openthree") / "channels"
        return Path(root) / "channels" / f"{self.name}.json"

    def state_path(self) -> Path:
        custom = self.option("token_file", "")
        return Path(custom) if custom else self._default_state_path()

    def _load_state(self) -> None:
        self._state_path = self.state_path()
        self._state = {}
        if self._state_path.exists():
            try:
                loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 坏档就当没登录过，重新扫码
                self.log.warning("token 文件读不出来，将重新登录：%s", self._state_path)
                return
            # 只认 dict：手改过的文件可能是列表 / 字符串，那样后面所有 `.get()`
            # 都会炸在轮询线程里 —— 而线程一死，这个号就永久离线了。
            self._state = loaded if isinstance(loaded, dict) else {}

    def _context_tokens(self) -> Dict[str, Any]:
        """`context_tokens` 必须是 dict；不是就换一个新的（同样的道理：别炸在轮询线程里）。"""
        tokens = self._state.get("context_tokens")
        if not isinstance(tokens, dict):
            tokens = {}
            self._state["context_tokens"] = tokens
        return tokens

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        with self._state_lock:
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                self._state_path.write_text(
                    json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                self.log.warning("token 文件写入失败：%s", exc)

    # --- 登录 ---------------------------------------------------------
    def _write_qrcode(self, matrix: Optional[List[List[bool]]],
                      raw_png: bytes) -> Optional[Path]:
        """把二维码落到磁盘：内容本身是 PNG 就存原图，否则把矩阵现场编码成 PNG。

        这条路径是「事后重扫 / 转发」的保险，所以落下去的必须是一张**真图** ——
        不能是把 URL 当 base64 解出来的那几十字节垃圾。
        """
        data = raw_png or (_qr.matrix_to_png(matrix) if matrix else b"")
        if not data or self._state_path is None:
            return None
        path = self._state_path.parent / f"{self.name}-qrcode.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception:  # noqa: BLE001
            return None
        return path

    def _print_qrcode(self, qrcode: str, text: str,
                      matrix: Optional[List[List[bool]]],
                      png: Optional[Path]) -> None:
        """把二维码画到控制台上 —— 扫码的人就在这个终端前，不该让他去开文件。

        终端宽度优先：`full` 档一个模块占两列，宽终端（Windows Terminal / 现代
        cmd 常见 120 列）没问题，80 列的窄终端就可能折行 —— 折行的码是扫不出来的。
        所以先按 M 级画；装不下就用 L 级重编（少几个模块），再装不下才退化成
        「给图片路径」。画不出来（控制台连方块字符都不认）同样退化。
        """
        prefix = f"[频道:{self.name}] "
        width = _qr.console_width()
        art = _qr.render_matrix(matrix, max_width=width, min_quiet=1) if matrix else ""
        if not art and text:
            art = _qr.render_text(text, ec="l", max_width=width, min_quiet=1)
        if art:
            self.log.info("二维码已打印到控制台，请用微信扫码登录")
            print(f"{prefix}请用微信扫码登录：", flush=True)
            try:
                print(art, flush=True)
            except UnicodeEncodeError:          # 控制台编码保守：退成全 ASCII 的那档
                fallback = (_qr.render_matrix(matrix, style="ascii") if matrix
                            else _qr.render_text(text, ec="l", style="ascii"))
                print(fallback, flush=True)
        else:
            self.log.info("二维码没能画成字符画，请打开二维码图片扫码")
            print(f"{prefix}请用微信扫码登录", flush=True)
        if png:
            print(f"{prefix}二维码图片：{png}", flush=True)
        elif not art:
            print(f"{prefix}二维码内容：{qrcode[:200]}", flush=True)

    def login(self) -> None:
        """扫码登录：把二维码打印到控制台 → 轮询状态 → 存 token。"""
        if self._state_path is None:            # 单独调用 login() 也要知道 token 落哪
            self._load_state()
        base = self.base_url
        data = get_json(f"{base}/ilink/bot/get_bot_qrcode", params={"bot_type": 3},
                        headers=make_headers(""), timeout=DEFAULT_TIMEOUT)
        qrcode = str(data.get("qrcode") or "").strip()
        image = str(data.get("qrcode_img_content") or "")
        if not qrcode:
            raise RuntimeError(f"没拿到二维码：{data}")
        matrix, raw_png = _qr.content_to_matrix(image or qrcode)
        png = self._write_qrcode(matrix, raw_png)
        # 服务端给的是图片时矩阵才是唯一依据；给 URL 时窄终端可以降级重编
        text = "" if raw_png else (image or qrcode).strip()
        self._print_qrcode(qrcode, text, matrix, png)
        deadline = time.time() + self.login_timeout
        while time.time() < deadline and not self.stopping:
            try:
                status = get_json(f"{base}/ilink/bot/get_qrcode_status",
                                  params={"qrcode": qrcode},
                                  headers=make_headers(""),
                                  timeout=QRCODE_STATUS_TIMEOUT)
            except HttpError as exc:
                self.log.warning("扫码状态查询失败（重试）：%s", exc)
                self._stop.wait(2)
                continue
            state = str(status.get("status") or "")
            if state == "confirmed":
                token = str(status.get("bot_token") or "")
                server_base = str(status.get("baseurl") or "") or base
                if not token:
                    raise RuntimeError("扫码已确认但没返回 bot_token")
                self._state["bot_token"] = token
                self._state["base_url"] = server_base.rstrip("/")
                self._save_state()
                self._notice("登录成功，token 已保存")
                return
            if state == "expired":
                raise RuntimeError("二维码已过期，请重新运行")
            self._stop.wait(1.5)
        raise RuntimeError("等待扫码超时")

    # --- 收 -----------------------------------------------------------
    def run(self) -> None:
        self._load_state()
        if not self.bot_token:
            self.login()
        cursor = str(self._state.get("get_updates_buf") or "")
        self._notice(f"已连接（{self.base_url}），开始长轮询")
        while not self.stopping:
            try:
                data = self._post("ilink/bot/getupdates", {
                    "get_updates_buf": cursor,
                    "base_info": {"channel_version": CHANNEL_VERSION},
                }, timeout=GETUPDATES_TIMEOUT)
            except HttpError as exc:
                self.log.warning("getupdates 失败，5 秒后重试：%s", exc)
                self._stop.wait(5)
                continue

            msgs = data.get("msgs") or []
            if not isinstance(msgs, list):
                msgs = []
            new_cursor = str(data.get("get_updates_buf") or "")
            for raw_msg in msgs:
                # 逐条兜底：一条读不懂的消息只丢它自己。让异常逸出 while 会杀掉
                # 这条轮询线程 —— 而 `-d` 下没有监督重启，那个号就永久离线了。
                try:
                    inbound = parse_inbound(raw_msg, channel=self.name)
                    if inbound is None:
                        continue
                    if not self.allowed(inbound.user_id):
                        self.log.info("忽略未授权的发送者：%s", inbound.user_id)
                        continue
                    if self._duplicate(inbound):
                        continue
                    self._remember_context_token(inbound)
                except Exception as exc:  # noqa: BLE001 - 坏消息不该停掉频道
                    self.log.warning("跳过一条读不懂的消息：%s", exc)
                    continue
                self.emit(inbound)

            if new_cursor and new_cursor != cursor:
                cursor = new_cursor
                self._state["get_updates_buf"] = cursor
                self._save_state()
            if _as_int(data.get("ret")) != 0 and not msgs:
                self.log.debug("getupdates ret=%s，3 秒后重试", data.get("ret"))
                self._stop.wait(3)

    # --- 去重 / 上下文 -------------------------------------------------
    def _duplicate(self, inbound: InboundMessage) -> bool:
        """两级去重：消息 id（重连重复投递）+ 同人同文本（游标回退时重复）。"""
        now = time.time()
        key = inbound.msg_id or ""
        if key:
            if key in self._seen:
                return True
            self._seen[key] = now
            while len(self._seen) > MAX_SEEN:
                self._seen.popitem(last=False)
        text_key = f"{inbound.user_id}:{hash(inbound.text)}"
        last = self._text_seen.get(text_key)
        self._text_seen[text_key] = now
        while len(self._text_seen) > MAX_SEEN:
            self._text_seen.popitem(last=False)
        return bool(last and now - last < TEXT_DEDUP_TTL)

    def _remember_context_token(self, inbound: InboundMessage) -> None:
        """记住每个用户最近的 `context_token` —— 后续主动发消息要靠它。"""
        if not inbound.reply_token:
            return
        tokens = self._context_tokens()
        if tokens.get(inbound.user_id) != inbound.reply_token:
            tokens[inbound.user_id] = inbound.reply_token
            self._save_state()

    # --- 发 -----------------------------------------------------------
    def send_text(self, msg: InboundMessage, text: str) -> None:
        body_text = str(text or "").strip()
        if not body_text:
            return
        context_token = (msg.reply_token
                         or self._context_tokens().get(msg.user_id, ""))
        if not context_token:
            self.log.warning("没有 context_token，无法回复 %s", msg.user_id)
            return
        for chunk in split_text(body_text):
            try:
                resp = self._post("ilink/bot/sendmessage", {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": msg.user_id,
                        "client_id": str(uuid.uuid4()),
                        "message_type": 2,       # 2 = BOT
                        "message_state": 2,      # 2 = FINISH
                        "context_token": context_token,
                        "item_list": [{"type": 1, "text_item": {"text": chunk}}],
                    },
                    "base_info": {"channel_version": CHANNEL_VERSION},
                }, timeout=DEFAULT_TIMEOUT)
            except HttpError as exc:
                self.log.warning("发送失败：%s", exc)
                return
            if _as_int(resp.get("ret")) != 0:
                self.log.warning("发送返回 ret=%s errmsg=%s",
                                 resp.get("ret"), resp.get("errmsg") or resp.get("err_msg"))
                return

    def stop(self) -> None:
        self._stop.set()

    # --- 内部 ---------------------------------------------------------
    def _api(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _post(self, path: str, payload: Dict[str, Any], *,
              timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        data = post_json(self._api(path), payload,
                         headers=make_headers(self.bot_token), timeout=timeout)
        return data if isinstance(data, dict) else {}

    def _notice(self, text: str) -> None:
        self.log.info("%s", text)
        print(f"[频道:{self.name}] {text}", flush=True)
