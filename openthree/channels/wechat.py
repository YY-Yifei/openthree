"""微信频道：iLink Bot HTTP API（扫码登录 → 长轮询收 → HTTP 回）。

协议（对齐 qwenpaw 的 wechat 频道）：
  * 登录：`GET ilink/bot/get_bot_qrcode?bot_type=3` 拿二维码 → 轮询
    `GET ilink/bot/get_qrcode_status?qrcode=…` 直到 `confirmed`，拿 `bot_token` 与 `baseurl`
  * 收消息：`POST ilink/bot/getupdates`（服务端最多挂 35 秒，`get_updates_buf` 当游标）
  * 发消息：`POST ilink/bot/sendmessage`（要带回消息里的 `context_token`）
  * 发图片 / 文件：`POST ilink/bot/getuploadurl` 登记 → 把 **AES-128-ECB 加密后**的
    字节 POST 到 CDN → 从响应头 `x-encrypted-param` 取回落点 → 再用 `sendmessage`
    发一条 `image_item` / `file_item`（同一个 `context_token`）

所以它**不需要公网地址**，只要能出网 —— 这也是选它而不是公众号回调的原因。

登录用的二维码**直接画在控制台上**，手机对着终端就能扫 —— 服务端给的
`qrcode_img_content` 既可能是 base64 的 PNG，也可能只是一段确认页 URL，
两种形态都由 `_qr` 统一成模块矩阵；后者需要本地编码（`_qrencode`）。
PNG 同时落在 `.openthree/channels/<频道名>-qrcode.png`，事后重扫 / 转发都用得上。

附件走 `attach_file`（与 A2A 同一个工具、同一套闸门）：智能体把文件挂到本轮那条
`InboundMessage.attachments` 上，这里在**发完正文之后**逐张上传并发出。图片按
magic bytes 认，认出来才用 `image_item` —— 微信按 item type 渲染，把非图片字节
塞进去，对方看到的就是一张破图；其余一律当文件发（视频也在其中）。

收到的图片走**反方向**的同一条链：`image_item` 里没有可直接用的 URL，只有
`media.encrypt_query_param`（下载凭证）与 `aeskey`（hex），内容是 **AES-128-ECB
加密**的。于是「收图」= 下载 → 解密 → 按 magic bytes 认类型 → 落
`.openthree/assets/`（与 CLI `--image` 同一个目录、同一条命名规则）→ 把路径挂到
`InboundMessage.images`，由桥交给 `Runtime.run(images=…)` 进模型的多模态 content。
任何一步失败都只影响那一张图：文字那部分照跑，原因以一句话拼进正文 —— 对发消息的
人来说「图没收到」和「你没看懂」是同一件事，不说清他只会以为机器人坏了。

纯标准库实现，不依赖任何第三方 SDK（AES-128-ECB 在 `_aes.py`，出站闸门与 A2A
共用在 `_outbound.py`）；token 与游标落在 `.openthree/channels/<频道名>.json`，
重启不重复扫码。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ..attach import MAX_OUTBOUND_FILES
from ..multimodal import max_image_bytes, sniff_mime
from ._http import HttpError, get_bytes, get_json, post_bytes, post_json
from .base import Channel, InboundMessage
from . import _aes, _inbound, _outbound, _qr

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.0.1"
GETUPDATES_TIMEOUT = 45.0      # 服务端长轮询上限 35s，留点余量
QRCODE_STATUS_TIMEOUT = 60.0   # 扫码状态轮询本身也是长轮询
DEFAULT_TIMEOUT = 15.0
MAX_SEEN = 2000                # 去重集合上限（够覆盖一次重连的重复投递）
TEXT_DEDUP_TTL = 30.0          # 同一用户 + 同一文本 30 秒内只处理一次
MAX_TEXT_CHUNK = 1500          # 单条回复的最大字符数，长回答分多条发

# --- 出站媒体（图片 / 文件）----------------------------------------------
# 媒体不走 iLink 的 JSON 接口本身：先在 `getuploadurl` 登记，再把**加密后**的字节
# POST 到 CDN，最后用 CDN 回的落点信息发一条 `image_item` / `file_item` 消息。
# CDN 是另一个域名（不是 `base_url`），所以单独列出来。
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
# 传一个 20 MiB 的文件要时间：加密后体积还会涨（PKCS#7 最多补一块），百兆上行
# 也要十几秒。15 秒的默认超时会让大图**必然**失败，然后被当成「发不出去」。
UPLOAD_TIMEOUT = 120.0
MEDIA_IMAGE = 1                # `getuploadurl` 的 media_type
MEDIA_FILE = 3
# 单文件上限**比 A2A 那个（20 MiB）更小**，理由是这里多一道同步开销：加密是纯 Python
# 的（AES 走 T 表，实测 ≈0.5–1.5 秒/MiB），而它跑在桥那**唯一一条**工作线程上 ——
# 20 MiB 会让所有人的答复一起卡住半分钟。10 MiB 实测 4–6 秒，是「发一张图」该有的
# 量级。这不是协议上限，是同步发送的工程上限。
MAX_MEDIA_BYTES = 10 << 20
# 微信按 **item type** 渲染：图片走 `image_item`(2)，其余走 `file_item`(4)。
# 视频 `video_item`(5) 刻意不发 —— 它要额外的封面/时长字段，猜错就是一张黑框；
# 视频当文件发，对方照样能看，没有第二种坏法。

# --- 入站图片 -------------------------------------------------------------
# 图片在 CDN 上是**加密**的：`image_item.media.encrypt_query_param` 是下载凭证
# （不是 URL，要拼进 `/c2c/download?encrypted_query_param=`），`image_item.aeskey`
# 是 16 字节密钥的 hex 形式。所以「收一张图」必然是一次网络往返 + 一次解密。
#
# 这两条超时都跑在**长轮询那条线程**上（收图是同步做的），所以它们同时是「这张图
# 什么时候放弃」和「下一条消息最多晚多久被发现」：单张 20 秒是「一张手机原图」的
# 量级，一条消息合计 45 秒是这条线程愿意为图片付出的上限 —— 再长的等待会让对方的
# 下一条消息排在服务端队列里，而那个代价比少一张图大得多。
IMAGE_TIMEOUT = 20.0
IMAGE_DEADLINE = 45.0

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

    只做「够用」的提取：图片 / 视频 / 文件在这里只留一个占位符，**图片的真实内容**
    由 `_absorb_images()` 下载后挂到 `InboundMessage.images`（占位符仍留着 —— 读文本
    的人才知道这条消息带过一张图）。
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


def extract_images(item_list: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """挑出 `item_list` 里的图片项，给出「下载 + 解密」要用的那几个值。

    按**字段存在性**判（有 `image_item` 就是图），不看 `type` 判别符 —— 与 A2A 收
    part 同一条判据：判别符是对方的一句话，字段才是我真要用的东西；两者不一致时
    以字段为准，判别符写错了图也还能收下来。

    只返回**引用**不下载：`parse_inbound()` 保持纯函数（可单独测、不联网），下载那一步
    在频道里做（`WeChatChannel._inbound_images()`）。

    两个密钥字段都带着，怎么选在 `_decrypt_key()`：
      * `image_item.aeskey`：16 字节密钥的 **hex**
      * `media.aes_key`：**base64**，里面可能是 16 字节原始密钥，也可能是一串 hex
    """
    out: List[Dict[str, str]] = []
    for item in (item_list if isinstance(item_list, list) else []):
        if not isinstance(item, dict):
            continue
        image = _as_dict(item.get("image_item"))
        if not image:
            continue
        media = _as_dict(image.get("media"))
        out.append({
            "encrypt_query_param": str(media.get("encrypt_query_param") or ""),
            "aeskey_hex": str(image.get("aeskey") or ""),
            "aes_key": str(media.get("aes_key") or ""),
        })
    return out


def _append_notes(text: str, notes: List[str]) -> str:
    """把几句说明拼到消息正文后面（空的那几段直接不占位，不留空行）。"""
    parts = [str(text or "").strip()] + [str(n or "").strip() for n in notes]
    return "\n\n".join(p for p in parts if p).strip()


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
                    # 收图放在去重**之后**：重连重复投递的同一条消息不该再下一次 CDN。
                    self._absorb_images(inbound, raw_msg)
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

    # --- 入站图片 -------------------------------------------------------
    @property
    def allow_images(self) -> bool:
        """收不收对方发来的图片（`allow_images`，默认收）。关掉就一个请求也不发。"""
        return self.option_bool("allow_images", True)

    def assets_dir(self) -> Path:
        """收来的图落在哪儿 —— 与 A2A、CLI `--image` 同一个目录（`.openthree/assets/`）。"""
        return _inbound.assets_dir(self.workspace_dir)

    @staticmethod
    def _download_url(spec: Dict[str, str]) -> str:
        """把一条图片引用变成能 GET 的地址（空串 = 这条没带下载凭证）。

        **只认 `media.encrypt_query_param`**，绝不回退到 `image_item.url`：后者来自
        入站消息（不可信输入），而 `urlopen()` 会跟随重定向 —— 一条消息就能让进程
        去 GET 内网 / 云 metadata 地址。A2A 那边是同一条判据（「URI 不下载，要图请
        用 base64」）；微信这边凭证是协议给的唯一正路。

        参数名里那个 d 不能少（`encrypted_query_param`）：它是 CDN 的固定拼法，
        少一个字母就是 404，而 404 在日志里看起来和「图片过期了」一模一样。
        """
        enc = spec.get("encrypt_query_param") or ""
        if not enc:
            return ""
        return f"{CDN_BASE}/download?encrypted_query_param={quote(enc, safe='')}"

    @staticmethod
    def _decrypt_key(spec: Dict[str, str]) -> bytes:
        """这张图的 AES-128 密钥（空 = 内容没加密，别去解）。

        `image_item.aeskey` 是 hex；`media.aes_key` 是 base64，而 base64 解出来可能是
        16 字节原始密钥，也可能是一串 32 字符的 hex（官方 SDK 两种都发过）。

        认不出就返回空：**宁可按「认不出类型」拒掉，也不拿错密钥去解** —— 解错的
        AES 不会报错，只会得到一团垃圾，然后它以「不是图片」的名义被拒，真正的原因
        （密钥不对）从此查不出来。
        """
        hex_key = (spec.get("aeskey_hex") or "").strip()
        if hex_key:
            try:
                key = bytes.fromhex(hex_key)
            except ValueError:
                key = b""
            if len(key) == 16:
                return key
        raw_b64 = (spec.get("aes_key") or "").strip()
        if not raw_b64:
            return b""
        try:
            raw = base64.b64decode(raw_b64, validate=True)
        except (ValueError, TypeError):
            return b""
        if len(raw) == 16:
            return raw
        if len(raw) == 32:
            try:
                return bytes.fromhex(raw.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return b""
        return b""

    def _fetch_image(self, spec: Dict[str, str], timeout: float) -> Tuple[Optional[bytes], str]:
        """下载并按需解密一张图，返回 `(字节, 未收到的原因)`；成功时原因为空串。

        大小闸在下载时而不是下载后：`get_bytes(max_bytes=…)` 读到超限就停，不为一张
        被拒的图先读进几 MB 内存。

        失败原因里**不带 URL** —— 它含 `encrypt_query_param`（一份下载凭证），而这句
        话会进正文 → 模型上下文 → 可能原样回给对方。详情留给日志。
        """
        url = self._download_url(spec)
        if not url:
            return None, "1 张图片未接收（消息里没有下载凭证）"
        limit = max_image_bytes()
        try:
            data = get_bytes(url, timeout=timeout,
                             max_bytes=limit if limit > 0 else MAX_MEDIA_BYTES)
        except Exception as exc:       # noqa: BLE001 - 收图失败不该连文字一起丢
            # 只捕 `HttpError` 不够：`get_bytes()` 已经在里面把读正文的断连包成了
            # `HttpError`，但调用栈上还有别的可能（内存、平台相关的 SSL 库抛法）。
            # 这里放出去一个异常，`run()` 的兜底会 `continue` —— 用户连问题本身都
            # 收不到。承诺是「一切失败都变成一句说明」，就按承诺捕。
            self.log.warning("微信收图失败：%s", exc)     # 详情（含凭证）只进日志
            status = getattr(exc, "status", 0)
            code = f"，HTTP {status}" if status else ""
            return None, f"1 张图片未接收（下载失败{code}）"
        key = self._decrypt_key(spec)
        if len(key) == 16:
            try:
                data = _aes.decrypt(data, key)
            except ValueError as exc:      # 密文不是整块 / 补位不合法
                return None, f"1 张图片未接收（解密失败：{exc}）"
        return data, ""

    def _inbound_images(self, specs: List[Dict[str, str]]) -> Tuple[List[str], List[str]]:
        """下载 / 解密 / 落盘，返回 `(本地路径表, 说明)`。

        闸门按**代价从便宜到贵**排：关掉收图 → 条数 → 单张大小 → 目录预算。被拒的
        每一张各留一句原因，那些原因会拼进正文发回去 —— 对方不知道少了什么，只会
        以为机器人没看懂。

        时间也有一条闸（`IMAGE_DEADLINE`）：这段代码跑在**长轮询那条线程**上，8 张图
        × 20 秒能把下一条消息的发现推迟将近三分钟；超了就把剩下的如实记为没收到。
        """
        accepted = specs[:_inbound.MAX_IMAGES_PER_MESSAGE]
        paths: List[str] = []
        notes: List[str] = []
        deadline = time.monotonic() + IMAGE_DEADLINE
        for index, spec in enumerate(accepted):
            left = deadline - time.monotonic()
            if left <= 0:
                notes.append(f"（另有 {len(accepted) - index} 张图片未接收：下载超时）")
                break
            data, why = self._fetch_image(spec, min(IMAGE_TIMEOUT, left))
            if data is None:
                notes.append(why)      # why 本身就是一句完整的话（不再套一层括号）
                continue
            path, why = _inbound.save_within_budget(self.assets_dir(), data)
            if path:
                paths.append(path)
            else:
                self.log.warning("微信收图落盘失败：%s", why)
                notes.append(why)
        extra = len(specs) - len(accepted)
        if extra > 0:
            notes.append(f"（另有 {extra} 张图片未接收：一条消息最多收 "
                         f"{_inbound.MAX_IMAGES_PER_MESSAGE} 张）")
        return paths, notes

    def _absorb_images(self, inbound: InboundMessage, raw: Dict[str, Any]) -> None:
        """把这条消息里的图片收下来挂到 `inbound.images`，失败写进正文。

        **不抛异常**：收图是轮询线程上的额外工作，一张图失败不该把文字一起丢掉 ——
        而「为什么没收到这张图」必须说出来，静默少一张图与丢消息是同一种感受。
        """
        specs = extract_images(_as_dict(raw).get("item_list") or [])
        if not specs:
            return
        if not self.allow_images:
            inbound.text = _append_notes(inbound.text, [
                f"（{len(specs)} 张图片未处理：本频道关掉了 allow_images）"])
            return
        paths, notes = self._inbound_images(specs)
        if paths:
            inbound.images = paths
            self.log.info("收到 %d 张图片（共 %d 张）", len(paths), len(specs))
        if notes:
            inbound.text = _append_notes(inbound.text, notes)

    # --- 出站媒体（图片 / 文件） ---------------------------------------
    def workspace_root(self) -> Path:
        """项目根（`.openthree` 的父目录）—— `outbound_roots` 与黑名单的基准。"""
        return _outbound.project_root(self.workspace_dir)

    @property
    def outbound_roots(self) -> List[Path]:
        """能发给对方的文件根目录（`outbound_roots`，默认 `"."` = 工作区）。

        `[]` 是**唯一**的关法：那时 `supports_files` 为假，桥不会把这个回合发布
        给 `attach_file`，工具会老实说「这次答复发不出文件」—— 而不是记下来再丢掉。
        """
        raw = self.option("outbound_roots", _outbound.DEFAULT_ROOTS)
        return _outbound.parse_roots(raw, self.workspace_root())

    @property
    def supports_files(self) -> bool:
        """能不能把文件送回去 —— 与 `outbound_roots` 同步（A2A 用的是同一条判据）。"""
        return bool(self.outbound_roots)

    def _outbound_media(self, msg: InboundMessage) -> Tuple[List[Dict[str, Any]], List[str]]:
        """把本轮附件过一遍闸门，返回 `(要发的媒体, 说明)`。

        **不抛异常**：被拒的每一个各留一句原因，拼进正文一起发回去 —— 对收件人来说
        「少了一张图」和「发失败」是同一件事，不说清他只会以为二维码没生成。

        闸门在 `_outbound.py`（与 A2A 共用一份：黑名单只该有一处）。这里只补两件
        微信特有的事：**类型按 magic bytes 定**（见模块说明），以及计数与合计。
        """
        pending = [a for a in (getattr(msg, "attachments", None) or ())
                   if isinstance(a, dict)]
        roots = self.outbound_roots
        if not roots:
            return [], ([f"（{len(pending)} 个附件没有发送：本频道关掉了出站文件）"]
                        if pending else [])
        root = self.workspace_root()
        media: List[Dict[str, Any]] = []
        notes: List[str] = []
        total = 0
        for item in pending:
            raw = str(item.get("path") or "").strip()
            shown = _outbound.clean_name(str(item.get("name") or ""),
                                         Path(raw).name or "附件")
            path, size, reason = _outbound.screen(raw, roots, root,
                                                  max_bytes=MAX_MEDIA_BYTES)
            if path is None:
                notes.append(f"（{shown} 没有发送：{reason}）")
                continue
            if len(media) >= MAX_OUTBOUND_FILES:
                notes.append(f"（{shown} 没有发送：一条答复最多发 "
                             f"{MAX_OUTBOUND_FILES} 个文件）")
                continue
            if total + size > _outbound.MAX_TOTAL_BYTES:
                notes.append(f"（{shown} 没有发送：附件合计超过 "
                             f"{_outbound.MAX_TOTAL_BYTES} 字节）")
                continue
            data, why = _outbound.read_exact(path, size)
            if data is None:
                notes.append(f"（{shown} 没有发送：{why}）")
                continue
            # `sniff_mime()` 对认不出的内容返回 None（不是空串）：先兜成空串再判，
            # 否则一个普通文本附件就会让整条答复在发送前抛异常。
            kind = "image" if (sniff_mime(data) or "").startswith("image/") else "file"
            media.append({"kind": kind, "name": shown, "data": data})
            total += size
        return media, notes

    def _upload_media(self, data: bytes, media_type: int, user_id: str) -> Dict[str, Any]:
        """把一份文件加密后传到微信 CDN，返回拼 `image_item` / `file_item` 要用的值。

        协议（与 qwenpaw 的 wechat client 一致）：先 `getuploadurl` 登记
        filekey / md5 / 原始大小 / 加密后大小 / AES 密钥，再把 **AES-128-ECB 加密后**
        的字节 POST 到 CDN。「文件落在哪」由 CDN 用响应头 `x-encrypted-param` 告知，
        正文可能是空的 —— 所以这条路要用 `post_bytes()` 而不是 `post_json()`。

        **aes_key 有三种编码，三种都得写对**：调 API 用 hex（32 字符）；拼进消息用
        `base64(hex)`；而真正拿去加密的是那 16 个原始字节。少写对一个，对面解不开，
        而本地一切看起来都正常（长度对、不抛异常、图片是一块灰色）。
        """
        if not data:
            raise RuntimeError("空文件不上传")
        key_raw = secrets.token_bytes(16)
        key_hex = key_raw.hex()
        filekey = secrets.token_hex(16)
        encrypted = _aes.encrypt(data, key_raw)
        resp = self._post("ilink/bot/getuploadurl", {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": user_id,
            "rawsize": len(data),
            "rawfilemd5": hashlib.md5(data).hexdigest(),
            "filesize": len(encrypted),
            "aeskey": key_hex,
            "no_need_thumb": True,
            "base_info": {"channel_version": CHANNEL_VERSION},
        })
        url = str(resp.get("upload_full_url") or "").strip()
        if not url:
            param = str(resp.get("upload_param") or "").strip()
            if not param:
                raise RuntimeError(f"getuploadurl 没给上传地址（ret={_as_int(resp.get('ret'))}）")
            url = (f"{CDN_BASE}/upload?encrypted_query_param={quote(param, safe='')}"
                   f"&filekey={filekey}")
        # 上传**不带** bot_token：授权信息已经在那串加密参数里，多带一个头会被 CDN 拒。
        _status, headers, _body = post_bytes(
            url, encrypted, headers={"Content-Type": "application/octet-stream"},
            timeout=UPLOAD_TIMEOUT)
        enc_param = str(headers.get("x-encrypted-param") or "").strip()
        if not enc_param:
            raise RuntimeError("CDN 没回 x-encrypted-param（那样发出去对方看到的是一片空白）")
        return {
            "encrypt_query_param": enc_param,
            "aes_key": base64.b64encode(key_hex.encode()).decode(),
            "size": len(encrypted),
        }

    def _send_media(self, user_id: str, context_token: str, item: Dict[str, Any]) -> bool:
        """上传并发出一个附件；失败只记日志并返回 False（正文已经送到了）。"""
        media_type = MEDIA_IMAGE if item["kind"] == "image" else MEDIA_FILE
        try:
            up = self._upload_media(item["data"], media_type, user_id)
        except (HttpError, RuntimeError, OSError) as exc:
            self.log.warning("附件上传失败（%s）：%s", item["name"], exc)
            return False
        media = {"encrypt_query_param": up["encrypt_query_param"],
                 "aes_key": up["aes_key"],
                 "encrypt_type": 1}
        if item["kind"] == "image":
            payload = {"type": 2, "image_item": {"media": media, "mid_size": up["size"]}}
        else:
            payload = {"type": 4, "file_item": {"media": media,
                                                "file_name": item["name"],
                                                "len": str(up["size"])}}
        try:
            resp = self._post("ilink/bot/sendmessage", {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": user_id,
                    "client_id": str(uuid.uuid4()),
                    "message_type": 2,       # 2 = BOT
                    "message_state": 2,      # 2 = FINISH
                    "context_token": context_token,
                    "item_list": [payload],
                },
                "base_info": {"channel_version": CHANNEL_VERSION},
            })
        except HttpError as exc:
            self.log.warning("附件发送失败（%s）：%s", item["name"], exc)
            return False
        if _as_int(resp.get("ret")) != 0:
            self.log.warning("附件发送返回 ret=%s errmsg=%s",
                             resp.get("ret"), resp.get("errmsg") or resp.get("err_msg"))
            return False
        return True

    # --- 发 -----------------------------------------------------------
    def send_text(self, msg: InboundMessage, text: str) -> Optional[bool]:
        context_token = (msg.reply_token
                         or self._context_tokens().get(msg.user_id, ""))
        if not context_token:
            self.log.warning("没有 context_token，无法回复 %s", msg.user_id)
            # 没有它就一条消息都发不出去（连附件也不行，媒体循环在下面）。
            # 回 None 会让桥记成 ✅，而对方那边一片安静。
            return False
        # 附件先过闸门：被拒的原因要拼进正文一起发出去，否则对方永远不知道少了什么。
        # 顺序也是刻意的 —— **正文在前、图片在后**：一条只有图片的消息没有上下文，
        # 而 explanation 往往就写在正文里。
        media, notes = self._outbound_media(msg)
        body_text = "\n".join(x for x in [str(text or "").strip(), *notes] if x).strip()
        if body_text:
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
                    # 一个字都没送到 = 明确没送到。回 None 会让桥记成 ✅，而对方
                    # 那边一片安静 —— 这正是「报告说成功、用户什么都没收到」。
                    return False
                if _as_int(resp.get("ret")) != 0:
                    self.log.warning("发送返回 ret=%s errmsg=%s",
                                     resp.get("ret"), resp.get("errmsg") or resp.get("err_msg"))
                    return False
        sent = 0
        for item in media:
            if self._send_media(msg.user_id, context_token, item):
                sent += 1
        # 只有媒体、且一个都没发出去 = 这次答复**什么都没送到**。必须回 `False`：
        # `Channel.send_text` 的契约里 False 就是「明确没送到」，桥据此把终态记成
        # 「答复没发出去」而不是 ✅ —— 否则对方那边一片安静，报告里却写着成功。
        if media and not body_text and not sent:
            return False
        return None

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
