"""微信渠道**出站媒体**（图片 / 文件）的测试。

守三件事，都是「看起来正常但其实是错的」那一类：

1. **闸门**：根外 / 密钥 / 超体积 / 超个数一律不发，而且**原因要写进正文** ——
   对方不知道少了什么，会以为功能没生效。
2. **协议形状**：`image_item`(type 2) / `file_item`(type 4) 的字段名与取值
   （`mid_size` 是数字、`len` 是字符串、`aes_key` 是 `base64(hex)`）—— 这些错一个，
   对面看到的就是一张灰图或一条空气消息，而本地日志全绿。
3. **失败隔离**：上传失败不能连累正文；没有附件时行为**与从前一字不差**。

全程不联网：`_post()` 与模块级 `post_bytes()` 都被替换掉。
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from openthree.channels import _outbound
from openthree.channels import wechat as wx
from openthree.channels.base import InboundMessage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    """一个不联网的频道 + 记账本。"""
    ws = tmp_path / ".openthree"
    calls = {"upload": [], "send": [], "bytes": []}

    def _post(self, path, payload, timeout=0):
        if path == "ilink/bot/getuploadurl":
            calls["upload"].append(payload)
            return {"upload_full_url": "https://cdn.example/upload?x=1"}
        if path == "ilink/bot/sendmessage":
            calls["send"].append(payload)
            return {"ret": 0}
        raise AssertionError(f"意外的接口：{path}")

    def _post_bytes(url, data, headers=None, timeout=0):
        calls["bytes"].append({"url": url, "size": len(data),
                               "headers": dict(headers or {})})
        return 200, {"x-encrypted-param": "ENC123"}, b""

    monkeypatch.setattr(wx.WeChatChannel, "_post", _post)
    monkeypatch.setattr(wx, "post_bytes", _post_bytes)
    ch = wx.WeChatChannel("wx", {}, workspace_dir=ws)
    ch._state = {"bot_token": "tok"}
    ch._state_path = ws / "channels" / "wx.json"
    return ch, ws, tmp_path, calls


def _msg(attachments=None, text="hi"):
    m = InboundMessage(text=text, channel="wx", channel_type="wechat",
                       user_id="u1", reply_token="ctx")
    m.attachments = list(attachments or [])
    return m


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(PNG)
    return p


def _text_file(tmp_path, name="note.txt"):
    p = tmp_path / name
    p.write_text("这是一份文本附件\n", encoding="utf-8")
    return p


# --- 能力声明 ---------------------------------------------------------
def test_supports_files_follows_outbound_roots(tmp_path):
    ws = tmp_path / ".openthree"
    assert wx.WeChatChannel("a", {}, workspace_dir=ws).supports_files is True
    off = wx.WeChatChannel("b", {"outbound_roots": "[]"}, workspace_dir=ws)
    assert off.supports_files is False


def test_outbound_roots_accepts_both_config_spellings(tmp_path):
    ws = tmp_path / ".openthree"
    ch = wx.WeChatChannel("a", {"outbound_roots": "['assets', 'tmp']"}, workspace_dir=ws)
    names = sorted(p.name for p in ch.outbound_roots)
    assert names == ["assets", "tmp"]
    # 空列表（配置层会写成字符串 "[]"）是唯一关法
    assert wx.WeChatChannel("b", {"outbound_roots": "[]"}, workspace_dir=ws).outbound_roots == []


# --- 闸门 -------------------------------------------------------------
def test_gate_rejects_outside_blacklist_and_oversize(fake, tmp_path):
    ch, ws, base, _ = fake
    outside = Path(tmp_path.parent) / "outside.png"
    outside.write_bytes(PNG)
    secret = ws / "config.yaml"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("llm:\n  api_key: sk-xxx\n", encoding="utf-8")
    big = base / "big.bin"
    big.write_bytes(b"\x00" * (_outbound.MAX_FILE_BYTES + 1))

    media, notes = ch._outbound_media(_msg([
        {"path": str(_png(base))},
        {"path": str(outside)},
        {"path": str(secret)},
        {"path": str(big)},
    ]))
    assert len(media) == 1
    joined = " / ".join(notes)
    assert "不在允许发送的目录内" in joined
    assert "密钥黑名单" in joined
    assert "超过单文件上限" in joined


def test_gate_rejects_more_than_the_count_limit(fake, tmp_path):
    ch, ws, base, _ = fake
    files = [_png(base, f"p{i}.png") for i in range(wx.MAX_OUTBOUND_FILES + 2)]
    media, notes = ch._outbound_media(_msg([{"path": str(p)} for p in files]))
    assert len(media) == wx.MAX_OUTBOUND_FILES
    assert any("最多发" in n for n in notes)


def test_gate_rejects_files_that_are_not_there(fake, tmp_path):
    ch, ws, base, _ = fake
    media, notes = ch._outbound_media(_msg([{"path": str(base / "nope.png")}]))
    assert media == []
    assert "读不到这个路径" in notes[0]


def test_no_attachments_means_no_notes(fake):
    ch, ws, base, _ = fake
    assert ch._outbound_media(_msg([])) == ([], [])


def test_gate_says_why_when_outbound_files_are_off(tmp_path, monkeypatch):
    """关掉出站文件时，附件要变成一句说明 —— 不能悄悄消失。"""
    ws = tmp_path / ".openthree"
    ch = wx.WeChatChannel("wx", {"outbound_roots": "[]"}, workspace_dir=ws)
    media, notes = ch._outbound_media(_msg([{"path": str(_png(tmp_path))}]))
    assert media == []
    assert "关掉了出站文件" in notes[0]


def test_name_is_sanitized(fake, tmp_path):
    ch, ws, base, _ = fake
    media, _ = ch._outbound_media(_msg([
        {"path": str(_text_file(base)), "name": "../../evil.txt"}]))
    assert media[0]["name"] == "evil.txt"


# --- 协议形状 ---------------------------------------------------------
def test_image_is_sent_as_image_item_with_the_right_encoding(fake, tmp_path):
    ch, ws, base, calls = fake
    ch.send_text(_msg([{"path": str(_png(base))}]), "正文")

    assert len(calls["upload"]) == 1
    up = calls["upload"][0]
    assert up["media_type"] == wx.MEDIA_IMAGE
    assert up["rawsize"] == len(PNG)
    assert up["filesize"] > len(PNG)          # 加密 + 补位之后更大
    assert len(up["aeskey"]) == 32 and all(c in "0123456789abcdef" for c in up["aeskey"])
    # 上传不带 bot_token：授权已在加密参数里
    assert "Authorization" not in calls["bytes"][0]["headers"]

    item = calls["send"][1]["msg"]["item_list"][0]
    assert item["type"] == 2
    media = item["image_item"]["media"]
    assert media["encrypt_query_param"] == "ENC123"
    assert media["encrypt_type"] == 1
    # aes_key 是 base64(hex)：解开必须正好是 API 用的那串 hex
    assert base64.b64decode(media["aes_key"]).decode() == up["aeskey"]
    assert item["image_item"]["mid_size"] == up["filesize"]


def test_non_image_goes_as_file_item(fake, tmp_path):
    ch, ws, base, calls = fake
    ch.send_text(_msg([{"path": str(_text_file(base))}]), "正文")

    assert calls["upload"][0]["media_type"] == wx.MEDIA_FILE
    item = calls["send"][1]["msg"]["item_list"][0]
    assert item["type"] == 4
    assert item["file_item"]["file_name"] == "note.txt"
    assert isinstance(item["file_item"]["len"], str)   # 协议要字符串
    assert item["file_item"]["media"]["aes_key"]


def test_text_comes_before_media_and_carries_context_token(fake, tmp_path):
    ch, ws, base, calls = fake
    ch.send_text(_msg([{"path": str(_png(base))}]), "先看正文")

    assert len(calls["send"]) == 2
    assert calls["send"][0]["msg"]["item_list"][0]["type"] == 1
    assert calls["send"][1]["msg"]["item_list"][0]["type"] == 2
    assert all(c["msg"]["context_token"] == "ctx" for c in calls["send"])


def test_media_only_reply_still_sends(fake, tmp_path):
    """只有图片、没有正文时也要发出去 —— 旧实现会在空正文处直接 return。"""
    ch, ws, base, calls = fake
    ch.send_text(_msg([{"path": str(_png(base))}]), "")
    assert len(calls["send"]) == 1
    assert calls["send"][0]["msg"]["item_list"][0]["type"] == 2


def test_rejected_files_are_explained_in_the_text(fake, tmp_path):
    ch, ws, base, calls = fake
    outside = Path(tmp_path.parent) / "outside.png"
    outside.write_bytes(PNG)
    ch.send_text(_msg([{"path": str(outside)}]), "说明")
    text = calls["send"][0]["msg"]["item_list"][0]["text_item"]["text"]
    assert "不在允许发送的目录内" in text


# --- 失败隔离 ---------------------------------------------------------
def test_upload_failure_does_not_lose_the_text(fake, tmp_path, monkeypatch):
    ch, ws, base, calls = fake

    def boom(url, data, headers=None, timeout=0):
        raise wx.HttpError("CDN 挂了")

    monkeypatch.setattr(wx, "post_bytes", boom)
    ch.send_text(_msg([{"path": str(_png(base))}]), "正文照常")

    assert len(calls["send"]) == 1
    assert calls["send"][0]["msg"]["item_list"][0]["type"] == 1


def test_missing_context_token_reports_not_delivered(fake, tmp_path):
    """没有 `context_token` 就一条也发不出去 —— 必须回 False（否则桥记 ✅）。"""
    ch, ws, base, calls = fake
    msg = _msg([{"path": str(_png(base))}])
    msg.reply_token = ""
    assert ch.send_text(msg, "正文") is False
    assert calls["send"] == []


def test_no_attachments_behaves_exactly_as_before(fake, tmp_path):
    ch, ws, base, calls = fake
    ch.send_text(_msg([]), "只有文字")
    assert len(calls["send"]) == 1
    assert calls["upload"] == []
    assert calls["bytes"] == []


def test_upload_url_can_be_built_from_upload_param(fake, tmp_path, monkeypatch):
    """CDN 只给 `upload_param` 时，要自己拼出上传地址（并带上 filekey）。"""
    ch, ws, base, calls = fake

    def _post(self, path, payload, timeout=0):
        if path == "ilink/bot/getuploadurl":
            calls["upload"].append(payload)
            return {"upload_param": "PARAM-1"}
        return {"ret": 0}

    monkeypatch.setattr(wx.WeChatChannel, "_post", _post)
    ch.send_text(_msg([{"path": str(_png(base))}]), "正文")
    assert calls["bytes"][0]["url"].startswith(wx.CDN_BASE + "/upload?encrypted_query_param=")
    assert "filekey=" in calls["bytes"][0]["url"]


def test_media_only_reply_that_fails_reports_not_delivered(fake, tmp_path, monkeypatch):
    """只有媒体、上传全失败 = 这次答复**什么都没送到** → 必须回 False。

    回 None 会让桥把终态记成 ✅，而对方那边一片安静 —— 「报告说成功、用户什么都没收到」
    是这套可观测性最不该出现的组合。
    """
    ch, ws, base, calls = fake

    def boom(url, data, headers=None, timeout=0):
        raise wx.HttpError("CDN 挂了")

    monkeypatch.setattr(wx, "post_bytes", boom)
    assert ch.send_text(_msg([{"path": str(_png(base))}]), "") is False


def test_text_delivered_with_failed_media_is_not_a_failure(fake, tmp_path, monkeypatch):
    """正文发到了、只是图没跟上：正文就是这次答复，不能记成失败。"""
    ch, ws, base, calls = fake

    def boom(url, data, headers=None, timeout=0):
        raise wx.HttpError("CDN 挂了")

    monkeypatch.setattr(wx, "post_bytes", boom)
    assert ch.send_text(_msg([{"path": str(_png(base))}]), "正文") is None


def test_wechat_cap_is_stricter_than_the_shared_one(fake, tmp_path):
    """微信侧单文件上限（10 MiB）比共享闸门（20 MiB）小 —— 因为要同步做纯 Python 加密。"""
    ch, ws, base, _ = fake
    assert wx.MAX_MEDIA_BYTES < _outbound.MAX_FILE_BYTES
    big = base / "big.bin"
    big.write_bytes(b"\x00" * (wx.MAX_MEDIA_BYTES + 1))
    media, notes = ch._outbound_media(_msg([{"path": str(big)}]))
    assert media == []
    assert "超过单文件上限" in notes[0]


def test_missing_encrypted_param_is_treated_as_failure(fake, tmp_path, monkeypatch):
    """CDN 没回 `x-encrypted-param` = 文件会显示成空白，宁可报失败也不发。"""
    ch, ws, base, calls = fake

    def no_header(url, data, headers=None, timeout=0):
        return 200, {}, b""

    monkeypatch.setattr(wx, "post_bytes", no_header)
    ch.send_text(_msg([{"path": str(_png(base))}]), "正文")
    assert len(calls["send"]) == 1          # 只有正文
    assert calls["send"][0]["msg"]["item_list"][0]["type"] == 1
