"""钉钉渠道**入站图片**（对方发来的图）的测试。

钉钉比微信多一跳：消息里没有能直接用的地址，只有一个 `downloadCode`，要拿它 +
`robotCode` 去开放平台换一次性的 `downloadUrl`，再带 accessToken GET 下来。所以
这里守六件事：

1. **协议形状**：`picture.content.downloadCode`，富文本里可能叫
   `pictureDownloadCode` —— 拼错一个字段名就是「收不到图」。
2. **失败要说真话**：换不到地址 / 下载失败 / 不是图片 / 空内容 / 超限，每一种都
   留一句原因拼进正文；文字那部分照样跑（一张图收不到不该连问题一起丢掉）。
3. **不泄漏**：失败原因里不带 `downloadCode`、不带地址 —— 它含一次下载许可，而
   这句话会进正文 → 模型上下文 → 可能原样回给对方。
4. **闸门只有一份**：条数 / 单张 / `assets/` 预算走 `_inbound.collect_images()`
   （与 A2A、微信同一条），钉钉这边不另写一套。
5. **只 GET 我们自己换回来的地址**：回调里的任何 URL 字段一个请求都不发，
   `file://` 也不许（`urlopen` 对它是照读的 —— 那等于让人读本机文件）。
6. **read loop 不跑慢网络**：`handle_frame` 只 ack / 解析 / 入队，收图在另一条
   线程上按序做。钉钉是推送式，服务端只等 3 秒的 ack。

全程不联网：`post_json()` 与 `get_bytes()` 都被替换掉。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from openthree.channels import _inbound
from openthree.channels import dingtalk as dt

CODE = "THIS-IS-A-DOWNLOAD-CODE"
ENDPOINT = "https://cdn.example.com/file?sign=abc"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(200)


def _png(size: int = 200) -> bytes:
    """一张内容可区分（去重靠内容）的「PNG」。"""
    return b"\x89PNG\r\n\x1a\n" + bytes(size)


def _picture(code: str = CODE, msg_id: str = "m1", **extra) -> dict:
    """一条「用户在单聊里发了一张图」的原始回调（字段取自真实 Stream 回调）。"""
    content = {"downloadCode": code} if code else {}
    content.update(extra)
    return {
        "msgtype": "picture",
        "msgId": msg_id,
        "content": content,
        "conversationType": "1",          # 1 = 单聊
        "senderStaffId": "u1",
        "senderNick": "甲",
        "sessionWebhook": "http://hook/1",
    }


def _rich(*items) -> dict:
    raw = _picture(code="", msg_id="m2")
    raw["msgtype"] = "richText"
    raw["content"] = {"richText": list(items)}
    return raw


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    """accessToken 不联网（换地址要它，但令牌本身不是被测对象）。"""
    monkeypatch.setattr(dt.DingTalkChannel, "access_token", lambda self: "TOK")


@pytest.fixture()
def chan(tmp_path):
    """一个不联网的频道；换地址 / 下载由各用例自己定义。"""
    return dt.DingTalkChannel("ding", {"robot_code": "ding1234"},
                              workspace_dir=tmp_path / ".openthree")


def _endpoint(monkeypatch, url: str = ENDPOINT, *, status: int = 0):
    """钉住「换地址」这一步，返回它收到的请求（用来断言形状）。"""
    seen: dict = {}

    def fake_post(target, payload=None, *, headers=None, timeout=0.0, **_):
        seen["url"] = target
        seen["payload"] = payload or {}
        seen["headers"] = headers or {}
        if status:
            raise dt.HttpError(f"HTTP {status}", status=status)
        return {"downloadUrl": url}

    monkeypatch.setattr(dt, "post_json", fake_post)
    return seen


def _download(monkeypatch, data: bytes, *, record: list | None = None):
    """钉住下载这一步。"""
    def fake_get(url, timeout=0.0, max_bytes=0):
        if record is not None:
            record.append({"url": url, "max_bytes": max_bytes})
        return data

    monkeypatch.setattr(dt, "get_bytes", fake_get)


def _emit(ch, raw):
    """把一条原始回调走完「解析 → 收图」，返回 `InboundMessage`。"""
    inbound = dt.parse_callback_data(raw, channel="ding", require_mention=False)
    assert inbound is not None
    ch._absorb_images(inbound, raw)
    return inbound


# --- 协议形状 ---------------------------------------------------------
def test_extract_images_reads_the_download_code():
    specs = dt.extract_images(_picture())
    assert specs == [{"download_code": CODE}]


def test_rich_text_pictures_are_extracted_under_either_field_name():
    """富文本里的图片项可能用 `pictureDownloadCode`（官方两个版本都发过）。"""
    specs = dt.extract_images(_rich(
        {"text": "看这张"},
        {"type": "picture", "pictureDownloadCode": "DC-RICH"},
        {"pictureDownloadCode": "DC-NO-TYPE"},        # 没声明类型：字段说了算
    ))
    assert [s["download_code"] for s in specs] == ["DC-RICH", "DC-NO-TYPE"]


def test_rich_text_file_items_are_not_treated_as_images():
    """明确声明成文件的项不收 —— 把 PDF 当图下下来再被拒，说明会变成「不是图片」。"""
    specs = dt.extract_images(_rich(
        {"type": "file", "downloadCode": "DC-FILE", "fileName": "a.pdf"},
        {"type": "picture", "downloadCode": "DC-PIC"},
    ))
    assert [s["download_code"] for s in specs] == ["DC-PIC"]


def test_a_declared_file_item_gets_no_picture_placeholder():
    """占位符与 `extract_images()` 是同一份判据：声明成文件的项说「附件」而不是
    「图片」。否则正文悬着一个永远等不到「未接收」说明的 `[图片]` —— 读起来
    就是丢了一张图。"""
    raw = _rich({"type": "file", "downloadCode": "DC-FILE", "fileName": "a.pdf"})
    assert dt.extract_text(raw) == "[附件]"
    assert dt.extract_images(raw) == []
    # 真图片仍旧说「图片」（占位符本身不能一起丢掉）
    assert dt.extract_text(_rich({"type": "picture",
                                  "downloadCode": "DC"})) == "[图片]"


def test_a_word_or_voice_message_yields_no_images():
    """文本 / 语音的 `content` 里也可能有 downloadCode（语音就是 AMR）——
    那个占位符逻辑不动，也不该被当成图收下来。"""
    assert dt.extract_images({"msgtype": "text", "text": {"content": "hi"}}) == []
    assert dt.extract_images({"msgtype": "audio",
                              "content": {"downloadCode": "DC-AMR"}}) == []


def test_a_picture_is_downloaded_and_stored(chan, monkeypatch):
    """换地址 → 下载 → 按 magic bytes 落在 `assets/`（与 CLI `--image` 同一个目录）。"""
    seen = _endpoint(monkeypatch)
    calls: list = []
    _download(monkeypatch, _png(), record=calls)

    inbound = _emit(chan, _picture())

    assert len(inbound.images) == 1
    assert Path(inbound.images[0]).read_bytes() == _png()
    assert Path(inbound.images[0]).suffix == ".png"        # 后缀由内容决定
    assert Path(inbound.images[0]).parent == chan.assets_dir()
    assert "未接收" not in inbound.text
    # 换地址的请求形状：downloadCode + robotCode，令牌走请求头
    assert seen["payload"] == {"downloadCode": CODE, "robotCode": "ding1234"}
    assert seen["headers"]["x-acs-dingtalk-access-token"] == "TOK"
    assert calls[0]["url"] == ENDPOINT


# --- 只 GET 我们自己换回来的地址 --------------------------------------
def test_the_message_content_never_decides_what_gets_downloaded(chan, monkeypatch):
    """回调里的 URL 字段一个请求都不发 —— 只认我们自己换回来的那个地址。

    这条是 SSRF 的反面证据：`pictureDownloadUrl` 之类的字段来自入站消息（不可信），
    而 `urlopen()` 会跟随重定向。一条消息就能让进程去 GET 内网 / 云 metadata。
    """
    _endpoint(monkeypatch, url=ENDPOINT)
    calls: list = []
    _download(monkeypatch, _png(), record=calls)
    raw = _picture(pictureDownloadUrl="http://169.254.169.254/latest/meta-data")

    inbound = _emit(chan, raw)

    assert len(inbound.images) == 1
    assert [c["url"] for c in calls] == [ENDPOINT]


def test_a_non_http_download_url_is_refused(chan, monkeypatch):
    """`file://` 不许下：`get_bytes()` 走 `urlopen()`，它对本地路径是照读的。"""
    _endpoint(monkeypatch, url="file:///C:/Windows/win.ini")

    def boom(url, timeout=0.0, max_bytes=0):       # pragma: no cover - 不该被调用
        raise AssertionError(f"非 http(s) 地址不该被下载：{url}")

    monkeypatch.setattr(dt, "get_bytes", boom)
    inbound = _emit(chan, _picture())
    assert inbound.images == []
    assert "下载地址不可用" in inbound.text


# --- 失败要说真话 -----------------------------------------------------
def test_exchange_failure_is_explained_and_the_text_survives(chan, monkeypatch):
    _endpoint(monkeypatch, status=400)

    def boom(url, timeout=0.0, max_bytes=0):       # pragma: no cover
        raise AssertionError("换不到地址时不该下载")

    monkeypatch.setattr(dt, "get_bytes", boom)
    inbound = _emit(chan, _picture())
    assert inbound.images == []
    assert "[图片]" in inbound.text                # 原来的占位符还在
    assert "换不到下载地址，HTTP 400" in inbound.text


def test_download_failure_is_explained(chan, monkeypatch):
    _endpoint(monkeypatch)

    def boom(url, timeout=0.0, max_bytes=0):
        raise dt.HttpError(f"HTTP 500 {url}", status=500)

    monkeypatch.setattr(dt, "get_bytes", boom)
    inbound = _emit(chan, _picture())
    assert inbound.images == []
    assert "下载失败，HTTP 500" in inbound.text


def test_the_two_hops_share_one_time_budget(chan, monkeypatch):
    """换地址用掉的时间从下载里扣。`collect_images()` 的 deadline 只在两张之间
    检查，两跳各拿一份 `timeout` 的话，最后一张能把上界拖成 deadline + timeout
    —— 而那条上界的存在理由就是「别让排队的消息等」。"""
    spent = 0.3

    def slow_resolve(code, timeout):
        time.sleep(spent)
        return ENDPOINT

    monkeypatch.setattr(chan, "_resolve_download_url", slow_resolve)
    seen: list = []

    def fake_get(url, timeout=0.0, max_bytes=0):
        seen.append(timeout)
        return _png()

    monkeypatch.setattr(dt, "get_bytes", fake_get)
    assert chan._fetch_image({"download_code": CODE}, 1.0) == (_png(), "")
    assert len(seen) == 1
    assert 0 < seen[0] <= 1.0 - spent + 0.05      # 只剩 0.7 左右，不是整份 1.0


def test_a_spent_budget_leaves_nothing_for_the_download(chan, monkeypatch):
    """换地址耗光预算 ⇒ 一个下载请求都不发，如实说「换下载地址超时」。"""
    def slow_resolve(code, timeout):
        time.sleep(0.3)
        return ENDPOINT

    monkeypatch.setattr(chan, "_resolve_download_url", slow_resolve)
    calls: list = []

    def boom(*a, **k):                             # pragma: no cover
        calls.append(1)
        raise AssertionError("预算已耗光时不该下载")

    monkeypatch.setattr(dt, "get_bytes", boom)
    data, why = chan._fetch_image({"download_code": CODE}, 0.2)
    assert data is None
    assert "超时" in why
    assert calls == []


def test_failure_reason_never_leaks_the_download_code(chan, monkeypatch):
    """`downloadCode` 是一次下载许可，不能跟着正文走进模型 / 对方。"""
    _endpoint(monkeypatch, status=400)

    def boom(url, timeout=0.0, max_bytes=0):       # pragma: no cover
        raise AssertionError("不该下载")

    monkeypatch.setattr(dt, "get_bytes", boom)
    inbound = _emit(chan, _picture())
    assert CODE not in inbound.text


def test_missing_credential_is_refused_without_any_request(chan, monkeypatch):
    def boom(*a, **k):                             # pragma: no cover
        raise AssertionError("没有下载凭证时不该发请求")

    monkeypatch.setattr(dt, "post_json", boom)
    monkeypatch.setattr(dt, "get_bytes", boom)
    inbound = _emit(chan, _picture(code=""))
    assert inbound.images == []
    assert "没有下载凭证" in inbound.text


def test_missing_robot_code_is_explained(tmp_path, monkeypatch):
    """`robotCode` 缺失时如实说 —— 不拿 client_id 糊过去（那是表情的回落值）。"""
    ch = dt.DingTalkChannel("ding", {}, workspace_dir=tmp_path / ".openthree")
    assert ch.robot_code == ""

    def boom(*a, **k):                             # pragma: no cover
        raise AssertionError("没有 robot_code 时不该发请求")

    monkeypatch.setattr(dt, "post_json", boom)
    inbound = _emit(ch, _picture())
    assert inbound.images == []
    assert "robot_code" in inbound.text


def test_content_that_is_not_an_image_is_refused(chan, monkeypatch):
    """换回来的东西确实不是图 ⇒ 按「认不出类型」拒掉，一个字节都不落盘。"""
    _endpoint(monkeypatch)
    _download(monkeypatch, b"this is definitely not a picture")
    inbound = _emit(chan, _picture())
    assert inbound.images == []
    assert "认不出图片类型" in inbound.text
    assert list(chan.assets_dir().glob("*")) == []


def test_empty_download_is_refused(chan, monkeypatch):
    """空响应：如实说「空内容」，不写一个 0 字节的「图」进 assets。"""
    _endpoint(monkeypatch)
    _download(monkeypatch, b"")
    inbound = _emit(chan, _picture())
    assert inbound.images == []
    assert "空内容" in inbound.text


def test_text_message_is_untouched(chan, monkeypatch):
    """纯文本消息：换地址与下载一次都不发，一个说明都不加。"""
    def boom(*a, **k):                             # pragma: no cover
        raise AssertionError("没有图片时不该发请求")

    monkeypatch.setattr(dt, "post_json", boom)
    monkeypatch.setattr(dt, "get_bytes", boom)
    inbound = _emit(chan, {"msgtype": "text", "text": {"content": "你好"},
                           "conversationType": "1", "senderStaffId": "u1",
                           "sessionWebhook": "http://hook/1"})
    assert inbound.text == "你好"
    assert inbound.images == []


# --- 闸门 -------------------------------------------------------------
def test_images_are_off_means_no_requests_at_all(tmp_path, monkeypatch):
    ch = dt.DingTalkChannel("ding", {"robot_code": "ding1234", "allow_images": "false"},
                            workspace_dir=tmp_path / ".openthree")

    def boom(*a, **k):                             # pragma: no cover
        raise AssertionError("关掉 allow_images 后不该发请求")

    monkeypatch.setattr(dt, "post_json", boom)
    monkeypatch.setattr(dt, "get_bytes", boom)
    inbound = _emit(ch, _picture())
    assert inbound.images == []
    assert "关掉了 allow_images" in inbound.text


def test_count_limit_is_enforced_before_downloading(chan, monkeypatch):
    """超出的那几张**一个请求都不发**：条数上限在换地址之前判。"""
    _endpoint(monkeypatch)
    calls: list = []
    _download(monkeypatch, _png(), record=calls)
    raw = _rich(*[{"type": "picture", "downloadCode": f"DC{i}"}
                  for i in range(_inbound.MAX_IMAGES_PER_MESSAGE + 3)])

    inbound = _emit(chan, raw)

    assert len(inbound.images) == _inbound.MAX_IMAGES_PER_MESSAGE
    assert len(calls) == _inbound.MAX_IMAGES_PER_MESSAGE
    assert "一条消息最多收" in inbound.text


def test_single_image_size_limit_comes_from_the_shared_setting(chan, monkeypatch):
    """单张上限读 `OPENTHREE_MAX_IMAGE_BYTES`（与 A2A、微信、CLI 同一条）。"""
    _endpoint(monkeypatch)
    calls: list = []
    _download(monkeypatch, _png(), record=calls)
    monkeypatch.setenv("OPENTHREE_MAX_IMAGE_BYTES", "4096")

    _emit(chan, _picture())
    assert calls[0]["max_bytes"] == 4096


def test_assets_budget_refuses_new_images_only(chan, monkeypatch):
    """`assets/` 到顶后只拒新图（与 A2A、微信共用那一条预算）。"""
    _endpoint(monkeypatch)
    _download(monkeypatch, _png(10))
    assert len(_emit(chan, _picture(msg_id="a")).images) == 1

    monkeypatch.setattr(_inbound, "ASSETS_BUDGET_BYTES", 1)   # 已经放不下了
    _download(monkeypatch, _png(20))                          # 内容不同：绕开 sha1
    inbound = _emit(chan, _picture(msg_id="b"))
    assert inbound.images == []
    assert "上限" in inbound.text


# --- read loop 不跑慢网络 ---------------------------------------------
class _WS:
    def __init__(self) -> None:
        self.sent: list = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


def _frame(raw: dict, message_id: str = "f1") -> dict:
    return {"type": "CALLBACK",
            "headers": {"messageId": message_id, "topic": dt.BOT_TOPIC},
            "data": json.dumps(raw)}


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_handle_frame_acks_first_and_downloads_off_the_read_loop(tmp_path, monkeypatch):
    """ack 之后立刻返回：下载在收图线程上做，read loop 不被几秒的网络扣住。

    钉钉是推送式，服务端只等 3 秒的 ack —— 在 read loop 上下图，后续帧的 ack 会
    一起被拖后，钉钉于是重投同一条消息。
    """
    seen: list = []
    ch = dt.DingTalkChannel("ding", {"robot_code": "ding1234"},
                            workspace_dir=tmp_path / ".openthree",
                            on_message=seen.append)
    _endpoint(monkeypatch)
    calls: list = []

    def slow_get(url, timeout=0.0, max_bytes=0):
        time.sleep(0.3)                       # 假装一张图要下 300ms
        calls.append(url)
        return _png()

    monkeypatch.setattr(dt, "get_bytes", slow_get)
    ws = _WS()
    started = time.monotonic()
    ch.handle_frame(ws, _frame(_picture()))
    elapsed = time.monotonic() - started

    assert ws.sent and json.loads(ws.sent[0])["code"] == 200   # 先 ack
    assert elapsed < 0.2                      # 没等下载
    assert calls == []                        # 下载还没开始
    assert _wait_for(lambda: len(seen) == 1)  # 但它确实会送到
    assert len(seen[0].images) == 1


def test_messages_are_delivered_in_order(tmp_path, monkeypatch):
    """连发两条不会因为图大图小而乱序进桥（桥只有一条工作线程）。"""
    seen: list = []
    ch = dt.DingTalkChannel("ding", {"robot_code": "ding1234"},
                            workspace_dir=tmp_path / ".openthree",
                            on_message=seen.append)
    _endpoint(monkeypatch)

    def by_code(url, timeout=0.0, max_bytes=0):
        if "slow" in url:
            time.sleep(0.3)
        return _png()

    monkeypatch.setattr(dt, "get_bytes", by_code)
    monkeypatch.setattr(dt.DingTalkChannel, "_resolve_download_url",
                        lambda self, code, timeout: f"https://cdn/{code}")

    ws = _WS()
    ch.handle_frame(ws, _frame(_picture(code="slow", msg_id="m-slow"), "f1"))
    ch.handle_frame(ws, _frame(_picture(code="fast", msg_id="m-fast"), "f2"))

    assert _wait_for(lambda: len(seen) == 2)
    assert [m.msg_id for m in seen] == ["m-slow", "m-fast"]
