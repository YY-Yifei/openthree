"""微信渠道**入站图片**（对方发来的图）的测试。

守四件事，都是「本地全绿、对面却什么都没看见」的那一类：

1. **协议形状**：凭证在 `media.encrypt_query_param`、密钥在 `image_item.aeskey`
   （hex）或 `media.aes_key`（base64）—— 拼错一个字段名就是 404 / 一张灰图。
2. **失败要说真话**：下载失败 / 解密失败 / 不是图片 / 超限，每一种都要留一句原因
   拼进正文；文字那部分照样跑（一条图收不到不该连问题一起丢掉）。
3. **不泄漏**：失败原因里不带 URL —— 它含 `encrypt_query_param`，而这句话会进正文
   → 模型上下文 → 可能原样回给对方。
4. **闸门只有一份**：条数 / 单张大小 / `assets/` 预算走 `_inbound`（与 A2A 同一条），
   微信这边不另写一套。

全程不联网：`get_bytes()` 被替换掉。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from openthree.channels import _aes, _inbound
from openthree.channels import wechat as wx

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
KEY = bytes(range(16))
CREDENTIAL = "THIS-IS-A-DOWNLOAD-CREDENTIAL"


def _png(size: int = 200) -> bytes:
    """一张内容可区分（去重靠内容）的「PNG」。"""
    return b"\x89PNG\r\n\x1a\n" + bytes(size)


def _raw_image(*, aeskey: str = KEY.hex(), aes_key: str = "", url: str = "",
               credential: str = CREDENTIAL, msg_id: str = "m1") -> dict:
    """一条「用户发了一张图」的原始消息（字段取自真实 iLink 响应的形状）。"""
    media = {"encrypt_type": 1}
    if credential:
        media["encrypt_query_param"] = credential
    if aes_key:
        media["aes_key"] = aes_key
    item = {"type": 2, "image_item": {"media": media}}
    if aeskey:
        item["image_item"]["aeskey"] = aeskey
    if url:
        item["image_item"]["url"] = url
    return {
        "message_type": 1,
        "msg_id": msg_id,
        "from_user_id": "u1",
        "context_token": "ctx-1",
        "item_list": [item],
    }


@pytest.fixture()
def chan(tmp_path):
    """一个不联网的频道；`get_bytes` 由各用例自己定义（返回什么就是什么）。"""
    ws = tmp_path / ".openthree"
    return wx.WeChatChannel("wx", {}, workspace_dir=ws)


def _emit(ch, raw, monkeypatch=None, payload=None):
    """把一条原始消息走完「解析 → 收图」，返回 `InboundMessage`。"""
    inbound = wx.parse_inbound(raw, channel="wx")
    assert inbound is not None
    ch._absorb_images(inbound, raw)
    return inbound


def _aes_art(ch, plain: bytes, monkeypatch):
    monkeypatch.setattr(wx, "get_bytes",
                        lambda url, timeout=0.0, max_bytes=0: _aes.encrypt(plain, KEY))


# --- 协议形状 ---------------------------------------------------------
def test_extract_images_keys_off_the_field_not_the_discriminator():
    """有 `image_item` 就是图 —— 判别符写错了也照样收（字段才是要用的东西）。"""
    raw = _raw_image()
    raw["item_list"][0]["type"] = 1        # 判别符撒谎
    specs = wx.extract_images(raw["item_list"])
    assert len(specs) == 1
    assert specs[0]["encrypt_query_param"] == CREDENTIAL
    assert specs[0]["aeskey_hex"] == KEY.hex()


def test_encrypted_image_is_downloaded_and_stored(chan, monkeypatch):
    """AES 密文进来 → 明文落在 `.openthree/assets/`（与 CLI `--image` 同一个目录）。"""
    plain = _png()
    _aes_art(chan, plain, monkeypatch)
    inbound = _emit(chan, _raw_image())

    assert len(inbound.images) == 1
    assert Path(inbound.images[0]).read_bytes() == plain
    assert Path(inbound.images[0]).suffix == ".png"      # 后缀由 magic bytes 决定
    assert Path(inbound.images[0]).parent == chan.assets_dir()
    assert "未接收" not in inbound.text


def test_plain_bytes_without_a_key_are_accepted(chan, monkeypatch):
    """没有密钥 = 内容没加密，别再拿空密钥去解（会得到一堆垃圾）。"""
    plain = _png()
    monkeypatch.setattr(wx, "get_bytes", lambda url, timeout=0.0, max_bytes=0: plain)
    inbound = _emit(chan, _raw_image(aeskey=""))
    assert len(inbound.images) == 1


def test_base64_raw_key_is_accepted(chan, monkeypatch):
    """`media.aes_key` 可以是 base64(16 字节原始密钥) —— 官方 SDK 的另一种形状。"""
    import base64

    plain = _png()
    _aes_art(chan, plain, monkeypatch)
    inbound = _emit(chan, _raw_image(
        aeskey="", aes_key=base64.b64encode(KEY).decode()))
    assert len(inbound.images) == 1
    assert open(inbound.images[0], "rb").read() == plain


def test_base64_of_hex_key_is_accepted(chan, monkeypatch):
    """`media.aes_key` 也可以是 base64(一串 hex) —— 解错的密钥不报错，只会得到垃圾。"""
    import base64

    plain = _png()
    _aes_art(chan, plain, monkeypatch)
    inbound = _emit(chan, _raw_image(
        aeskey="", aes_key=base64.b64encode(KEY.hex().encode()).decode()))
    assert len(inbound.images) == 1


# --- 失败要说真话 -----------------------------------------------------
def test_download_failure_is_explained_and_the_text_survives(chan, monkeypatch):
    def boom(url, timeout=0.0, max_bytes=0):
        raise wx.HttpError(f"HTTP 500 {url}", status=500)

    monkeypatch.setattr(wx, "get_bytes", boom)
    inbound = _emit(chan, _raw_image())
    assert inbound.images == []
    assert "[图片]" in inbound.text                 # 原来的占位符还在
    assert "下载失败，HTTP 500" in inbound.text


def test_failure_reason_never_leaks_the_credential(chan, monkeypatch):
    """`encrypt_query_param` 是一份下载凭证，不能跟着正文走进模型 / 对方。"""
    def boom(url, timeout=0.0, max_bytes=0):
        raise wx.HttpError(f"HTTP 500 {url}", status=500)

    monkeypatch.setattr(wx, "get_bytes", boom)
    inbound = _emit(chan, _raw_image())
    assert CREDENTIAL not in inbound.text


def test_wrong_key_is_reported_as_a_decryption_failure(chan, monkeypatch):
    """密钥不对 ⇒ 补位不合法 ⇒ 如实说「解密失败」，而不是当成一张坏图存下来。"""
    wrong = _aes.encrypt(_png(), bytes(range(16, 32)))
    monkeypatch.setattr(wx, "get_bytes", lambda url, timeout=0.0, max_bytes=0: wrong)
    inbound = _emit(chan, _raw_image())
    assert inbound.images == []
    assert "解密失败" in inbound.text


def test_content_that_is_not_an_image_is_refused(chan, monkeypatch):
    """没加密、也确实不是图 ⇒ 按「认不出类型」拒掉，一个字节都不落盘。"""
    plain = b"this is definitely not a picture"
    monkeypatch.setattr(wx, "get_bytes", lambda url, timeout=0.0, max_bytes=0: plain)
    inbound = _emit(chan, _raw_image(aeskey=""))
    assert inbound.images == []
    assert "认不出图片类型" in inbound.text
    assert list(chan.assets_dir().glob("*")) == []       # 一个字节都没落盘


def test_missing_credential_is_refused_without_any_request(chan, monkeypatch):
    def boom(url, timeout=0.0, max_bytes=0):       # pragma: no cover - 不该被调用
        raise AssertionError("没有下载凭证时不该发请求")

    monkeypatch.setattr(wx, "get_bytes", boom)
    inbound = _emit(chan, _raw_image(credential="", url=""))
    assert inbound.images == []
    assert "没有下载凭证" in inbound.text


def test_a_url_without_credential_is_never_downloaded(chan, monkeypatch):
    """SSRF 的反面证据：`image_item.url` 来自入站消息，**一个请求都不许发**。

    这条地址是云 metadata 的典型形状 —— 回退到它等于让一条消息指挥本机去 GET 任意
    地址（`urlopen` 还会跟随重定向）。凭证才是协议给的正路。
    """
    def boom(url, timeout=0.0, max_bytes=0):       # pragma: no cover - 不该被调用
        raise AssertionError(f"不该发请求：{url}")

    monkeypatch.setattr(wx, "get_bytes", boom)
    inbound = _emit(chan, _raw_image(credential="",
                                     url="http://169.254.169.254/latest/meta-data"))
    assert inbound.images == []
    assert "没有下载凭证" in inbound.text


def test_text_message_is_untouched(chan, monkeypatch):
    """纯文本消息：一次下载都不发，一个说明都不加。"""
    def boom(url, timeout=0.0, max_bytes=0):       # pragma: no cover
        raise AssertionError("没有图片时不该发请求")

    monkeypatch.setattr(wx, "get_bytes", boom)
    raw = {"message_type": 1, "msg_id": "m2", "from_user_id": "u1",
           "item_list": [{"type": 1, "text_item": {"text": "你好"}}]}
    inbound = _emit(chan, raw)
    assert inbound.text == "你好"
    assert inbound.images == []


# --- 闸门 -------------------------------------------------------------
def test_images_are_off_means_no_requests_at_all(tmp_path, monkeypatch):
    ws = tmp_path / ".openthree"
    ch = wx.WeChatChannel("wx", {"allow_images": "false"}, workspace_dir=ws)

    def boom(url, timeout=0.0, max_bytes=0):       # pragma: no cover
        raise AssertionError("关掉 allow_images 后不该发请求")

    monkeypatch.setattr(wx, "get_bytes", boom)
    inbound = _emit(ch, _raw_image())
    assert inbound.images == []
    assert "关掉了 allow_images" in inbound.text


def test_count_limit_is_enforced_before_downloading(chan, monkeypatch):
    """超出的那几张**一个请求都不发**：条数上限在下载之前判。"""
    calls = []

    def fake_get(url, timeout=0.0, max_bytes=0):
        calls.append(url)
        return _aes.encrypt(_png(), KEY)

    monkeypatch.setattr(wx, "get_bytes", fake_get)
    raw = _raw_image()
    raw["item_list"] = [
        {"type": 2, "image_item": {"aeskey": KEY.hex(),
                                   "media": {"encrypt_query_param": f"C{i}"}}}
        for i in range(_inbound.MAX_IMAGES_PER_MESSAGE + 3)
    ]
    inbound = _emit(chan, raw)
    assert len(inbound.images) == _inbound.MAX_IMAGES_PER_MESSAGE
    assert len(calls) == _inbound.MAX_IMAGES_PER_MESSAGE
    assert "一条消息最多收" in inbound.text


def test_single_image_size_limit_comes_from_the_shared_setting(chan, monkeypatch):
    """单张上限读 `OPENTHREE_MAX_IMAGE_BYTES`（与 A2A、CLI 同一条）。"""
    seen = {}

    def fake_get(url, timeout=0.0, max_bytes=0):
        seen["max_bytes"] = max_bytes
        return _aes.encrypt(_png(), KEY)

    monkeypatch.setenv("OPENTHREE_MAX_IMAGE_BYTES", "4096")
    monkeypatch.setattr(wx, "get_bytes", fake_get)
    _emit(chan, _raw_image())
    assert seen["max_bytes"] == 4096


def test_assets_budget_refuses_new_images_only(chan, monkeypatch):
    """`assets/` 到顶后只拒新图（与 A2A 共用那一条预算）。"""
    _aes_art(chan, _png(10), monkeypatch)
    assert len(_emit(chan, _raw_image(msg_id="a")).images) == 1
    monkeypatch.setattr(_inbound, "ASSETS_BUDGET_BYTES", 1)     # 已经放不下了
    _aes_art(chan, _png(20), monkeypatch)                       # 内容不同：绕开 sha1
    inbound = _emit(chan, _raw_image(msg_id="b"))
    assert inbound.images == []
    assert "上限" in inbound.text


# --- 与频道循环的连接 -------------------------------------------------
def test_run_hands_the_images_to_the_bridge(tmp_path, monkeypatch):
    """`run()` 真的会调收图 —— 否则上面每一条都只是「函数自己会跑」。"""
    ws = tmp_path / ".openthree"
    (ws / "channels").mkdir(parents=True)
    (ws / "channels" / "wx.json").write_text('{"bot_token": "tok"}', encoding="utf-8")

    plain = _png()
    seen = []
    ch = wx.WeChatChannel("wx", {}, workspace_dir=ws, on_message=seen.append)

    def fake_post(self, path, payload, *, timeout=0.0):
        assert path == "ilink/bot/getupdates"
        self._stop.set()                    # 一轮之后收工
        return {"msgs": [_raw_image()], "get_updates_buf": "cursor-2"}

    monkeypatch.setattr(wx.WeChatChannel, "_post", fake_post)
    monkeypatch.setattr(wx, "get_bytes",
                        lambda url, timeout=0.0, max_bytes=0: _aes.encrypt(plain, KEY))
    ch.run()

    assert len(seen) == 1
    assert len(seen[0].images) == 1
    assert open(seen[0].images[0], "rb").read() == plain
