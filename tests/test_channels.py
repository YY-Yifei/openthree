"""频道层测试：配置、协议解析、WebSocket 帧、桥、管理器、CLI。

这里不碰真网络：微信 / 钉钉的「联网部分」用协议层的纯函数（解析、ack、帧编解码）
来守；真连的部分靠 `channel test --online`，那不该进单元测试。
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time
import types
import zlib
from pathlib import Path

import pytest

from openthree.channels import _qr
from openthree.channels import config as ch_config
from openthree.channels._ws import (OPCODE_PING, OPCODE_TEXT, WebSocket, WebSocketError,
                                    decode_frame, _encode_frame)
from openthree.channels.base import Channel, InboundMessage
from openthree.channels.bridge import SHUTDOWN_NOTICE, AgentBridge, compose_input
from openthree.channels.config import ChannelConfig, ChannelsConfig, config_path
from openthree.channels.dingtalk import (ack_payload, extract_text as dt_extract,
                                         parse_callback_data, strip_at)
from openthree.channels.manager import ChannelManager, ChannelManagerError, instantiate
from openthree.channels.wechat import (WeChatChannel, extract_text as wx_extract,
                                       make_headers, parse_inbound, split_text)


# --- 配置文件 ---------------------------------------------------------
def test_normalize_type_accepts_colloquial_names():
    assert ch_config.normalize_type("dingding") == "dingtalk"
    assert ch_config.normalize_type("钉钉") == "dingtalk"
    assert ch_config.normalize_type("微信") == "wechat"
    assert ch_config.normalize_type("WX") == "wechat"
    assert ch_config.normalize_type("nope") == "nope"


def test_channel_config_reports_missing_required_fields():
    ding = ChannelConfig(name="d", type="dingtalk", options={"client_id": "x"})
    assert ding.missing() == ["client_secret"]
    assert not ding.is_ready()
    ding.options["client_secret"] = "y"
    assert ding.missing() == []
    assert ding.is_ready()


def test_wechat_has_no_required_fields_and_defaults_apply():
    wx = ChannelConfig(name="w", type="wechat")
    assert wx.is_ready()
    assert wx.option("base_url") == "https://ilinkai.weixin.qq.com"


def test_secrets_are_redacted():
    ding = ChannelConfig("d", "dingtalk",
                         options={"client_id": "abcdef", "client_secret": "supersecretvalue"})
    red = ding.redacted()
    assert red["client_id"] == "abcdef"
    assert red["client_secret"] != "supersecretvalue"
    assert "…" in red["client_secret"] or red["client_secret"] == "***"


def test_config_roundtrip_multiple_channels(tmp_path):
    cfg = ChannelsConfig(path=tmp_path / "channels.yaml")
    cfg.add(ChannelConfig("wx", "wechat", options={"bot_token": "t"}))
    cfg.add(ChannelConfig("ding", "dingtalk", enabled=False,
                          options={"client_id": "a", "client_secret": "b"}))
    cfg.save()

    back = ChannelsConfig.load(cfg.path)
    assert back.names() == ["wx", "ding"]
    assert back.get("ding").enabled is False
    assert back.get("ding").options["client_secret"] == "b"
    assert back.warnings == []


def test_config_accepts_flat_options_and_tolerates_bad_entries(tmp_path):
    path = tmp_path / "channels.yaml"
    path.write_text(
        "schema: 1\n"
        "channels:\n"
        "  - name: wx\n"
        "    type: 微信\n"
        "    base_url: https://example.com\n"
        "  - just-a-string\n"
        "  - {name: wx, type: wechat}\n"
        "  - {name: weird, type: nope}\n",
        encoding="utf-8")
    cfg = ChannelsConfig.load(path)
    assert cfg.get("wx").type == "wechat"
    assert cfg.get("wx").options["base_url"] == "https://example.com"
    assert any("已忽略" in w for w in cfg.warnings)      # 不是映射的那条
    assert any("重复" in w for w in cfg.warnings)         # 重名
    assert any("不受支持" in w for w in cfg.warnings)      # 未知类型
    assert cfg.get("weird") is not None                    # 未知类型仍保留，只是不可用


def test_unique_name_and_remove(tmp_path):
    cfg = ChannelsConfig(path=tmp_path / "c.yaml")
    cfg.add(ChannelConfig("wechat", "wechat"))
    assert cfg.unique_name("wechat") == "wechat-2"
    assert cfg.remove("wechat") is True
    assert cfg.remove("wechat") is False


def test_config_path_lives_in_workspace():
    assert config_path(Path("/tmp/proj/.openthree")).name == "channels.yaml"


# --- 微信 -------------------------------------------------------------
def test_wechat_extract_text_handles_text_and_media():
    items = [{"type": 1, "text_item": {"text": "你好"}},
             {"type": 3, "image_item": {"url": "x"}},
             {"type": 4, "voice_item": {"text_item": {"text": "语音转写"}}}]
    assert wx_extract(items) == "你好\n[图片]\n语音转写"


def test_wechat_parse_inbound_only_user_messages():
    raw = {"message_type": 1, "from_user_id": "u1", "context_token": "ctx",
           "msg_id": "m1", "item_list": [{"type": 1, "text_item": {"text": "在吗"}}]}
    msg = parse_inbound(raw, channel="wx")
    assert msg is not None
    assert msg.text == "在吗"
    assert msg.reply_token == "ctx"
    assert msg.kind == "dm"
    assert "微信" in msg.label()

    assert parse_inbound({**raw, "message_type": 2}, channel="wx") is None
    assert parse_inbound({"message_type": 1, "from_user_id": "u"}, channel="wx") is None


def test_wechat_split_text_keeps_chunks_under_limit():
    text = "\n".join(f"第{i}行" * 10 for i in range(50))
    chunks = split_text(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text
    assert split_text("", limit=10) == []


def test_wechat_headers_carry_token_only_when_present():
    assert "Authorization" not in make_headers("")
    assert make_headers("tok")["Authorization"] == "Bearer tok"
    assert make_headers("")["AuthorizationType"] == "ilink_bot_token"


def test_wechat_run_keeps_polling_past_bad_messages(tmp_path, monkeypatch):
    """一条读不懂的消息只丢它自己 —— 不能把轮询线程带走。

    线程一死，`-d` 下没有监督重启，那个号就永久离线了，除非重启进程。
    """
    from openthree.channels import wechat as wx

    (tmp_path / "channels").mkdir()
    (tmp_path / "channels" / "wx.json").write_text(
        json.dumps({"bot_token": "tok"}), encoding="utf-8")

    good = {"message_type": 1, "from_user_id": "u1", "msg_id": "m-good",
            "context_token": "ctx",
            "item_list": [{"type": 1, "text_item": {"text": "在吗"}}]}
    batch = [
        {"message_type": "abc", "from_user_id": "u2", "item_list": []},   # int() 会炸
        {"message_type": 1, "from_user_id": "u3",                          # text_item 不是对象
         "item_list": [{"type": 1, "text_item": "裸字符串"}]},
        "根本不是 dict",
        {"message_type": 1, "from_user_id": "u4", "msg_id": "m-boom",
         "item_list": []},
        good,
    ]

    real_parse = wx.parse_inbound

    def flaky_parse(msg, *, channel=""):
        if isinstance(msg, dict) and msg.get("msg_id") == "m-boom":
            raise RuntimeError("解析炸了")
        return real_parse(msg, channel=channel)

    monkeypatch.setattr(wx, "parse_inbound", flaky_parse)

    channel = WeChatChannel("wx", {}, workspace_dir=tmp_path)
    seen: list[str] = []
    done = threading.Event()
    polls: list[str] = []

    def fake_emit(inbound):
        seen.append(inbound.text)
        done.set()

    def fake_post(path, payload, *, timeout=0.0):
        polls.append(path)
        if len(polls) > 1:
            time.sleep(0.01)                    # 让循环转得慢一点，别空转
            return {}
        return {"msgs": list(batch), "get_updates_buf": "c1"}

    monkeypatch.setattr(channel, "emit", fake_emit)
    monkeypatch.setattr(channel, "_post", fake_post)
    monkeypatch.setattr(channel, "_notice", lambda text: None)

    thread = threading.Thread(target=channel.run, daemon=True)
    thread.start()
    assert done.wait(5.0)                      # 坏消息之后的正常消息照样跑到了
    deadline = time.time() + 5.0
    while time.time() < deadline and len(polls) < 2:
        time.sleep(0.01)                        # 再转一圈：证明循环还活着
    channel.stop()
    thread.join(5.0)

    assert not thread.is_alive()                # 是被 stop() 收走的，不是自己死掉的
    assert seen == ["在吗"]
    assert len(polls) >= 2


# --- 钉钉 -------------------------------------------------------------
def test_dingtalk_parse_dm_text():
    data = {"conversationId": "cid", "conversationType": "1", "senderNick": "张三",
            "senderStaffId": "s1", "msgtype": "text", "msgId": "m1",
            "text": {"content": "帮我看看日志"}, "sessionWebhook": "https://hook"}
    msg = parse_callback_data(data, channel="ding")
    assert msg.text == "帮我看看日志"
    assert msg.kind == "dm"
    assert msg.reply_token == "https://hook"
    assert msg.user_name == "张三"
    assert "钉钉" in msg.label()


def test_dingtalk_group_requires_mention_and_strips_at():
    data = {"conversationId": "cid", "conversationType": "2", "senderStaffId": "s1",
            "msgtype": "text", "text": {"content": "@机器人 早"}, "isInAtList": True}
    msg = parse_callback_data(data, channel="d")
    assert msg is not None and msg.kind == "group"
    assert msg.text == "早"
    # 没被 @ 就忽略
    assert parse_callback_data({**data, "isInAtList": False}, channel="d") is None
    # 关掉 require_mention 之后，同样的消息要能进来
    assert parse_callback_data({**data, "isInAtList": False},
                               channel="d", require_mention=False) is not None


def test_dingtalk_extract_text_variants():
    assert dt_extract({"msgtype": "text", "text": {"content": "hi"}}) == "hi"
    assert dt_extract({"msgtype": "audio", "content": {"recognition": "说了一句话"}}) == "说了一句话"
    assert dt_extract({"msgtype": "audio", "content": {}}) == "[语音]"
    assert dt_extract({"msgtype": "picture", "content": {}}) == "[图片]"
    assert dt_extract({"msgtype": "file", "content": {"fileName": "a.pdf"}}) == "[文件] a.pdf"
    assert dt_extract({"msgtype": "richtext",
                       "content": {"richText": [{"text": "甲"}, {"text": "乙"}]}}) == "甲乙"
    assert dt_extract({"msgtype": "unknown"}) == ""


def test_dingtalk_ack_payload_matches_stream_contract():
    ack = ack_payload("m-1", "/v1.0/im/bot/messages/get")
    assert ack["code"] == 200
    assert ack["headers"]["messageId"] == "m-1"
    assert ack["headers"]["contentType"] == "application/json"
    assert ack["message"] == "OK"
    assert isinstance(ack["data"], str)


def test_strip_at_handles_multiple_mentions():
    assert strip_at("@甲方 @乙方 你好") == "你好"
    assert strip_at("没有提到谁") == "没有提到谁"


# --- WebSocket 帧 ------------------------------------------------------
def test_frame_roundtrip_short_payload():
    frame = _encode_frame(OPCODE_TEXT, b"abc", mask=True)
    fin, opcode, payload, used = decode_frame(frame)
    assert fin and opcode == OPCODE_TEXT and payload == b"abc"
    assert used == len(frame)


def test_frame_roundtrip_medium_and_long_payload():
    for size in (125, 126, 300, 70000):
        payload = bytes(i % 256 for i in range(size))
        frame = _encode_frame(OPCODE_TEXT, payload, mask=True)
        fin, opcode, back, used = decode_frame(frame)
        assert back == payload and used == len(frame)
        # 服务端方向的帧不掩码
        unmasked = _encode_frame(OPCODE_TEXT, payload, mask=False)
        assert decode_frame(unmasked)[2] == payload


def test_websocket_handshake_and_receive_over_loopback():
    """用一个真 TCP 服务端验证握手 + 收帧（含 ping 自动回 pong）。"""
    received: list[bytes] = []
    # 对端读走那一帧之后才关连接：先 close 会发 RST，把还躺在内核缓冲里的字节丢掉，
    # 于是这个断言会随机器负载偶发失败。
    peer_read = threading.Event()

    def server(conn: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        key = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip().decode()
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        conn.sendall(_encode_frame(OPCODE_PING, b"", mask=False))
        conn.sendall(_encode_frame(OPCODE_TEXT, "你好频道".encode(), mask=False))
        data = conn.recv(4096)
        received.append(data)
        peer_read.set()
        conn.close()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept() -> None:
        conn, _ = srv.accept()
        try:
            server(conn)
        finally:
            srv.close()

    threading.Thread(target=accept, daemon=True).start()

    ws = WebSocket(f"ws://127.0.0.1:{port}/ding", connect_timeout=5.0, read_timeout=5.0)
    ws.connect()
    assert ws.recv_text() == "你好频道"          # 读的过程中会为 ping 自动回一个 pong
    assert peer_read.wait(5.0)                    # 等对端收走再关，避免 RST 吃掉这一帧
    ws.close()
    # 客户端发出的帧必须是掩码的（b2 的最高位是掩码位）
    assert received and (received[0][1] & 0x80)


def test_websocket_connect_rejects_non_websocket_scheme():
    with pytest.raises(WebSocketError):
        WebSocket("http://127.0.0.1:1/").connect()


# --- 桥 ---------------------------------------------------------------
class _FakeRuntime:
    def __init__(self, answer: str = "好的") -> None:
        self.answer = answer
        self.inputs: list[str] = []

    def run(self, text, **kwargs):
        self.inputs.append(text)
        return {"answer": self.answer}


def test_compose_input_carries_source():
    msg = InboundMessage(text="帮我查一下", channel="wx", channel_type="wechat",
                         user_id="u1", user_name="张三")
    composed = compose_input(msg)
    assert "微信" in composed and "张三" in composed
    assert composed.rstrip().endswith("帮我查一下")


def test_bridge_runs_queued_message_and_replies():
    rt = _FakeRuntime("答案在这")
    bridge = AgentBridge(rt, echo=False)
    replies: list[tuple[str, str]] = []
    done = threading.Event()

    def reply(msg, text):
        replies.append((msg.text, text))
        done.set()

    bridge.start()
    bridge.submit(InboundMessage(text="你好", channel="wx", channel_type="wechat",
                                 user_id="u"), reply)
    assert done.wait(5.0)
    bridge.stop()

    assert replies == [("你好", "答案在这")]
    assert rt.inputs and "你好" in rt.inputs[0]


def test_bridge_ask_returns_answer_and_under_lock_runs_callable():
    rt = _FakeRuntime("ok")
    bridge = AgentBridge(rt, echo=False)
    assert bridge.ask("hi") == "ok"
    assert bridge.under_lock(lambda: 42) == 42


def test_bridge_reports_failure_back_to_channel():
    class _Boom(_FakeRuntime):
        def run(self, text, **kwargs):
            raise RuntimeError("模型挂了")

    bridge = AgentBridge(_Boom(), echo=False)
    replies: list[str] = []
    done = threading.Event()

    def reply(msg, text):
        replies.append(text)
        done.set()

    bridge.start()
    bridge.submit(InboundMessage(text="hi", channel="wx"), reply)
    assert done.wait(5.0)
    bridge.stop()
    assert "模型挂了" in replies[0]


def test_bridge_stop_tells_queued_messages_they_are_dropped():
    """收工时还排着的一条都不跑 —— 但必须回一句，别让人盯着对话框干等。"""
    started = threading.Event()
    release = threading.Event()

    class _Slow(_FakeRuntime):
        def run(self, text, **kwargs):
            started.set()
            release.wait(5.0)
            return {"answer": "慢答案"}

    bridge = AgentBridge(_Slow(), echo=False)
    replies: dict[str, str] = {}
    lock = threading.Lock()

    def reply(msg, text):
        with lock:
            replies[msg.text] = text

    bridge.start()
    bridge.submit(InboundMessage(text="A", channel="wx"), reply)
    assert started.wait(5.0)                    # A 已被工作线程拿走，正在跑
    bridge.submit(InboundMessage(text="B", channel="wx"), reply)
    bridge.submit(InboundMessage(text="C", channel="wx"), reply)

    assert bridge.stop(timeout=0.2) is False    # 手头那条还没跑完 → 别急着收会话
    assert replies == {"B": SHUTDOWN_NOTICE, "C": SHUTDOWN_NOTICE}

    release.set()                               # A 跑完才真算收工
    deadline = time.time() + 5.0
    while time.time() < deadline and "A" not in replies:
        time.sleep(0.01)

    assert replies["A"] == "慢答案"              # 手头那条照样跑完、照样回
    assert bridge.stats()["queued"] == 0
    assert not bridge.busy                      # 计数没被漏减成「永远忙」


# --- 管理器 -----------------------------------------------------------
class _FakeChannel(Channel):
    """测试用频道：收到什么就返回什么，跑在一条线程里直到 stop()。"""

    type = "fake"
    started = 0

    def run(self) -> None:
        type(self).started += 1
        while not self.stopping:
            self._stop.wait(0.02)

    def stop(self) -> None:
        self._stop.set()

    def send_text(self, msg, text) -> None:
        self.sent = text


@pytest.fixture()
def fake_spec(monkeypatch):
    module = types.ModuleType("openthree_fake_channel_module")
    module.FakeChannel = _FakeChannel
    monkeypatch.setitem(sys.modules, "openthree_fake_channel_module", module)
    monkeypatch.setitem(ch_config._SPECS, "fake", ch_config.ChannelSpec(
        type="fake", label="假频道", summary="测试用",
        target="openthree_fake_channel_module:FakeChannel", fields=()))
    yield
    _FakeChannel.started = 0


def test_manager_builds_skips_and_runs(tmp_path, fake_spec):
    cfg = ChannelsConfig(path=tmp_path / "channels.yaml")
    cfg.add(ChannelConfig("f1", "fake"))
    cfg.add(ChannelConfig("off", "fake", enabled=False))
    cfg.add(ChannelConfig("bad", "dingtalk", options={"client_id": "x"}))

    bridge = AgentBridge(_FakeRuntime(), echo=False)
    mgr = ChannelManager(cfg, bridge=bridge, workspace_dir=tmp_path).build()
    assert [s.config.name for s in mgr.statuses] == ["f1"]
    assert len(mgr.skipped) == 2
    assert any("已停用" in s for s in mgr.skipped)
    assert any("client_secret" in s for s in mgr.skipped)

    assert mgr.start() == 1
    mgr.stop()
    assert _FakeChannel.started == 1


def test_manager_raises_when_nothing_runnable(tmp_path):
    cfg = ChannelsConfig(path=tmp_path / "channels.yaml")
    cfg.add(ChannelConfig("w", "wechat", enabled=False))
    mgr = ChannelManager(cfg, bridge=AgentBridge(_FakeRuntime(), echo=False),
                         workspace_dir=tmp_path).build()
    with pytest.raises(ChannelManagerError):
        mgr.start()


def test_manager_only_filter(tmp_path, fake_spec):
    cfg = ChannelsConfig(path=tmp_path / "channels.yaml")
    cfg.add(ChannelConfig("a", "fake"))
    cfg.add(ChannelConfig("b", "fake"))
    mgr = ChannelManager(cfg, bridge=AgentBridge(_FakeRuntime(), echo=False),
                         workspace_dir=tmp_path, only=["b"]).build()
    assert [s.config.name for s in mgr.statuses] == ["b"]


def test_instantiate_rejects_unknown_type(tmp_path):
    with pytest.raises(ChannelManagerError):
        instantiate(ChannelConfig("x", "nope"), on_message=None, workspace_dir=tmp_path)


# --- CLI --------------------------------------------------------------
def test_channel_cli_add_list_show_toggle_remove(tmp_path, capsys):
    from openthree.channels.cli import main as channel_main

    assert channel_main(["-C", str(tmp_path), "add", "--type", "dingding",
                         "--name", "ding", "--set", "client_id=a",
                         "--set", "client_secret=b"]) == 0
    cfg_file = config_path(tmp_path / ".openthree")
    assert cfg_file.exists()

    assert channel_main(["-C", str(tmp_path), "list"]) == 0
    out = capsys.readouterr().out
    assert "ding" in out and "钉钉" in out

    assert channel_main(["-C", str(tmp_path), "show", "ding"]) == 0
    out = capsys.readouterr().out
    assert "client_id = a" in out
    assert "client_secret = b" not in out        # 打码

    assert channel_main(["-C", str(tmp_path), "disable", "ding"]) == 0
    doc = json.loads(json.dumps(__import__("yaml").safe_load(cfg_file.read_text("utf-8"))))
    assert doc["channels"][0]["enabled"] is False

    assert channel_main(["-C", str(tmp_path), "enable", "ding"]) == 0
    assert channel_main(["-C", str(tmp_path), "test", "ding"]) == 0
    assert channel_main(["-C", str(tmp_path), "remove", "ding", "-y"]) == 0
    assert channel_main(["-C", str(tmp_path), "list"]) == 0


def test_channel_cli_rejects_missing_required_on_test(tmp_path, capsys):
    from openthree.channels.cli import main as channel_main

    assert channel_main(["-C", str(tmp_path), "add", "--type", "dingtalk",
                         "--name", "ding", "--set", "client_id=a"]) == 0
    assert channel_main(["-C", str(tmp_path), "test", "ding"]) == 2
    assert "client_secret" in capsys.readouterr().err


def test_openthree_cli_dispatches_channel_subcommand(tmp_path):
    from openthree.cli import main as openthree_main

    assert openthree_main(["channel", "-C", str(tmp_path), "list"]) == 0


def test_openthree_cli_daemon_without_channels_fails(tmp_path, capsys):
    from openthree.cli import main as openthree_main

    code = openthree_main(["-d", "-C", str(tmp_path)])
    assert code == 2
    assert "没有可启动的频道" in capsys.readouterr().err


def test_openthree_cli_daemon_rejects_task(tmp_path):
    from openthree.cli import main as openthree_main

    assert openthree_main(["-d", "-C", str(tmp_path), "顺便干点活"]) == 2


def test_openthree_cli_daemon_rejects_no_channels(tmp_path, capsys):
    """`-d` 就是「只跑频道」：再说 --no-channels 是自相矛盾，不能默默忽略。"""
    from openthree.cli import main as openthree_main

    assert openthree_main(["-d", "--no-channels", "-C", str(tmp_path)]) == 2
    assert "--no-channels" in capsys.readouterr().err


# --- 微信扫码：二维码直接打在控制台上 ------------------------------------
def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + ctype + body
            + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))


def _finder() -> list[list[bool]]:
    return [[r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)
             for c in range(7)] for r in range(7)]


def _qr_matrix(modules: int = 33) -> list[list[bool]]:
    """造一张带三个真定位图形的矩阵（形状不对时 `png_to_matrix` 会拒收）。"""
    grid = [[(r * 7 + c * 3) % 5 == 0 for c in range(modules)] for r in range(modules)]
    finder = _finder()
    for row0, col0 in ((0, 0), (0, modules - 7), (modules - 7, 0)):
        for r in range(7):
            for c in range(7):
                grid[row0 + r][col0 + c] = finder[r][c]
    return grid


def _paeth(left: int, up: int, upleft: int) -> int:
    p = left + up - upleft
    pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
    if pa <= pb and pa <= pc:
        return left
    return up if pb <= pc else upleft


def _filter_line(ftype: int, line: bytes, prev: bytes, bpp: int) -> bytes:
    """测试侧独立实现的行过滤 —— 用来反证解码，而不是复用被检代码。"""
    out = bytearray()
    for i, value in enumerate(line):
        left = line[i - bpp] if i >= bpp else 0
        upleft = prev[i - bpp] if i >= bpp else 0
        pred = {0: 0, 1: left, 2: prev[i], 3: (left + prev[i]) >> 1}.get(
            ftype, _paeth(left, prev[i], upleft))
        out.append((value - pred) & 0xFF)
    return bytes(out)


_PIXEL = {
    0: lambda dark: b"\x00" if dark else b"\xff",                       # 8 位灰度
    2: lambda dark: b"\x00\x00\x00" if dark else b"\xff\xff\xff",       # RGB
    3: lambda dark: b"\x00" if dark else b"\x01",                       # 调色板下标
    4: lambda dark: b"\x00\xff" if dark else b"\xff\xff",               # 灰度 + alpha
    6: lambda dark: b"\x00\x00\x00\xff" if dark else b"\xff\xff\xff\x00",   # RGBA：透明当白底
}


def _png_from_matrix(matrix, *, scale: int = 4, quiet: int = 4, ftype: int = 0,
                     depth: int = 8, color_type: int = 0) -> bytes:
    """把矩阵按给定 PNG 参数写成真 PNG —— 只为证明解码器认得。"""
    modules = len(matrix)
    size = (modules + 2 * quiet) * scale
    dark = [[0 <= x // scale - quiet < modules and 0 <= y // scale - quiet < modules
             and matrix[y // scale - quiet][x // scale - quiet]
             for x in range(size)] for y in range(size)]
    channels, bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type], 1
    if depth == 1:                                   # 1 位灰度：0 = 黑，1 = 白，MSB 在前
        rows = []
        for row in dark:
            raw = bytearray()
            for start in range(0, size, 8):
                byte = 0
                for bit in range(8):
                    value = row[start + bit] if start + bit < size else False
                    byte = (byte << 1) | (0 if value else 1)
                raw.append(byte)
            rows.append(bytes(raw))
    else:
        rows = [b"".join(_PIXEL[color_type](value) for value in row) for row in dark]
        bpp = channels
    payload = bytearray()
    prev = bytes(len(rows[0])) if rows else b""
    for row in rows:
        payload.append(ftype)
        payload.extend(_filter_line(ftype, row, prev, bpp))
        prev = row
    ihdr = struct.pack(">IIBBBBB", size, size, depth, color_type, 0, 0, 0)
    palette = (_png_chunk(b"PLTE", b"\x00\x00\x00\xff\xff\xff")
               if color_type == 3 else b"")
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + palette
            + _png_chunk(b"IDAT", zlib.compress(bytes(payload)))
            + _png_chunk(b"IEND", b""))


@pytest.mark.parametrize("modules", [21, 25, 33, 45, 61])
def test_qr_matrix_roundtrip_across_versions(modules):
    grid = _qr_matrix(modules)
    assert _qr.png_to_matrix(_png_from_matrix(grid)) == grid


@pytest.mark.parametrize("ftype", [0, 1, 2, 3, 4])
def test_qr_decode_handles_every_png_filter(ftype):
    grid = _qr_matrix(25)
    assert _qr.png_to_matrix(_png_from_matrix(grid, ftype=ftype)) == grid


@pytest.mark.parametrize("color_type,depth", [(0, 1), (0, 8), (2, 8), (3, 8), (4, 8), (6, 8)])
def test_qr_decode_handles_color_types(color_type, depth):
    grid = _qr_matrix(29)
    png = _png_from_matrix(grid, depth=depth, color_type=color_type)
    assert _qr.png_to_matrix(png) == grid


def test_qr_render_styles_keep_the_square_aspect():
    grid = _qr_matrix(33)
    half = _qr.render_matrix(grid, quiet=2, style="half").splitlines()
    assert len(half) == (33 + 2 * 2 + 1) // 2      # 一行两个模块行
    assert len(half[0]) == 33 + 2 * 2              # 一个模块一个字符宽
    for style, cell in (("full", "██"), ("ascii", "##")):
        lines = _qr.render_matrix(grid, quiet=2, style=style).splitlines()
        assert len(lines) == 33 + 2 * 2            # 一行一个模块行
        assert len(lines[0]) == 2 * (33 + 2 * 2)   # 一个模块两个字符宽
        assert set("".join(lines)) <= set(cell + " ")


def test_qr_render_refuses_what_it_cannot_read():
    assert _qr.render_png(b"") == ""
    assert _qr.render_png(b"not a png at all") == ""
    assert _qr.render_qr_base64("!!!not base64!!!") == ""
    blank = _png_from_matrix([[False] * 21 for _ in range(21)])
    assert _qr.render_png(blank) == ""             # 纯白图：没有定位图形，宁可不画


def test_qr_console_style_degrades_with_encoding():
    class _Stream:
        def __init__(self, encoding):
            self.encoding = encoding

    assert _qr.console_style(_Stream("utf-8")) == "half"
    assert _qr.console_style(_Stream("gbk")) == "full"       # GBK 有 █，没有 ▀
    assert _qr.console_style(_Stream("ascii")) == "ascii"
    assert _qr.console_style(_Stream(None)) == "half"        # 管道 / 无编码：按 utf-8


def test_wechat_login_prints_qrcode_on_console(tmp_path, monkeypatch, capsys):
    from openthree.channels import wechat as wx

    grid = _qr_matrix(33)
    png = _png_from_matrix(grid)
    image = base64.b64encode(png).decode()

    def fake_get_json(url, params=None, headers=None, timeout=None):
        if url.endswith("get_bot_qrcode"):
            return {"qrcode": "qr-token-123", "qrcode_img_content": image}
        return {"status": "confirmed", "bot_token": "tok-abc",
                "baseurl": "https://example.test"}

    monkeypatch.setattr(wx, "get_json", fake_get_json)
    channel = WeChatChannel("wx", {}, workspace_dir=tmp_path)
    channel.login()

    out = capsys.readouterr().out
    assert "请用微信扫码登录" in out
    assert "█" in out or "#" in out                # 真画了二维码，而不只是给路径
    assert len(out.splitlines()) > 10
    assert json.loads(channel.state_path().read_text("utf-8"))["bot_token"] == "tok-abc"
    saved = tmp_path / "channels" / "wx-qrcode.png"
    assert saved.read_bytes() == png               # 图片照样落盘，留着事后重扫


# --- 真实响应形态：qrcode_img_content 是 URL，不是图片 ------------------
REAL_LOGIN_URL = ("https://liteapp.weixin.qq.com/q/7GiQu1?qrcode="
                  "bef43f7a5f3d47867b41e7b67d5898a5&bot_type=3")


def _fake_qr_api(monkeypatch, content: str, status: str = "expired") -> None:
    """把 get_json 换成「登录接口返回给定内容、状态轮询返回给定状态」。"""
    from openthree.channels import wechat as wx

    def fake_get_json(url, params=None, headers=None, timeout=None):
        if url.endswith("get_bot_qrcode"):
            return {"qrcode": "qr-token-123", "qrcode_img_content": content}
        return {"status": status}

    monkeypatch.setattr(wx, "get_json", fake_get_json)


def test_wechat_login_handles_url_payload(tmp_path, monkeypatch, capsys):
    """真实服务端给的是确认页 URL —— 照样要画到控制台，落盘要是**真 PNG**。

    这条是核心回归：旧实现把 URL 当 base64 解成几十字节垃圾落成假 PNG，
    控制台上什么也画不出来，用户只看到一个打不开的图片路径。
    """
    _fake_qr_api(monkeypatch, REAL_LOGIN_URL)
    channel = WeChatChannel("wx", {}, workspace_dir=tmp_path)
    with pytest.raises(RuntimeError, match="过期"):
        channel.login()

    out = capsys.readouterr().out
    assert "请用微信扫码登录" in out
    assert "█" in out or "##" in out                # 真画了码，不是只给路径
    assert len(out.splitlines()) > 10

    saved = tmp_path / "channels" / "wx-qrcode.png"
    data = saved.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")     # 是真 PNG，不是几十字节垃圾
    from openthree.channels import _qrencode

    assert _qr.png_to_matrix(data) == _qrencode.encode_matrix(REAL_LOGIN_URL)


def test_wechat_login_never_saves_unreadable_bytes_as_png(tmp_path, monkeypatch, capsys):
    """内容既不是图片也不是干净文本时，落盘的仍必须是一张能解的 PNG。"""
    junk = base64.b64encode(b"not-a-png-at-all").decode()
    _fake_qr_api(monkeypatch, junk)
    channel = WeChatChannel("wx", {}, workspace_dir=tmp_path)
    with pytest.raises(RuntimeError, match="过期"):
        channel.login()
    capsys.readouterr()

    data = (tmp_path / "channels" / "wx-qrcode.png").read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert _qr.png_to_matrix(data) is not None


def test_qrcode_falls_back_to_lower_error_level_on_narrow_console(
        tmp_path, monkeypatch, capsys):
    """80 列 GBK 终端：M 级（41 模块 × 2 列）装不下，必须降成 L 级再画。"""
    from openthree.channels import _qrencode
    from openthree.channels import wechat as wx

    monkeypatch.setattr(_qr, "console_style", lambda stream=None: "full")
    monkeypatch.setattr(_qr, "console_width", lambda default=80: 80)
    _fake_qr_api(monkeypatch, REAL_LOGIN_URL)
    channel = WeChatChannel("wx", {}, workspace_dir=tmp_path)
    with pytest.raises(RuntimeError, match="过期"):
        channel.login()

    out = capsys.readouterr().out
    art = [line for line in out.splitlines() if "█" in line]
    assert art, "80 列终端上也必须画出二维码"
    assert all(len(line) <= 80 for line in art)
    assert len(art[0]) == 2 * (len(_qrencode.encode_matrix(REAL_LOGIN_URL, ec="l")) + 2)


# --- 编码器：容量表 / 结构不变量 ---------------------------------------
def test_qrencode_capacity_table_is_consistent():
    """每个版本的数据码字 + 纠错码字 = 规范给定的总码字数。"""
    from openthree.channels import _qrencode

    totals = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172, 7: 196, 8: 242,
              9: 292, 10: 346, 11: 404, 12: 466, 13: 532, 14: 581, 15: 655}
    for version, levels in _qrencode._DATA.items():
        for level, (ec, blocks, data) in levels.items():
            assert data + ec * blocks == totals[version], f"v{version}-{level}"


def test_qrencode_structure_invariants():
    from openthree.channels import _qrencode

    matrix = _qrencode.encode_matrix(REAL_LOGIN_URL)
    assert matrix is not None
    n = len(matrix)
    assert n == 17 + 4 * _qrencode.version_of(matrix)

    for row0, col0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for dr in range(7):
            for dc in range(7):
                ring = dr in (0, 6) or dc in (0, 6)
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                assert matrix[row0 + dr][col0 + dc] == (ring or core)

    for index in range(8, n - 8):                   # 时序图形（第 6 行 / 第 6 列）
        assert matrix[6][index] == (index % 2 == 0)
        assert matrix[index][6] == (index % 2 == 0)

    assert matrix[n - 8][8]                          # 固定深色模块


@pytest.mark.parametrize("version,level", [(1, "l"), (1, "h"), (5, "m"),
                                           (10, "q"), (15, "h")])
def test_qrencode_fits_exactly_at_capacity(version, level):
    from openthree.channels import _qrencode

    cap = _qrencode.capacity(version, level)
    assert _qrencode.encode_matrix("a" * cap, ec=level, version=version) is not None
    assert _qrencode.encode_matrix("a" * (cap + 1), ec=level, version=version) is None


def test_qrencode_adapts_version_to_content():
    from openthree.channels import _qrencode

    assert _qrencode.version_of(_qrencode.encode_matrix("hi")) == 1
    assert _qrencode.version_of(_qrencode.encode_matrix(REAL_LOGIN_URL)) <= 6
    assert _qrencode.encode_matrix("x" * 500) is None      # 装不下返回 None，不抛异常


def test_qr_matrix_png_roundtrip():
    from openthree.channels import _qrencode

    matrix = _qrencode.encode_matrix(REAL_LOGIN_URL)
    png = _qr.matrix_to_png(matrix)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert _qr.png_to_matrix(png) == matrix


def test_qr_render_respects_terminal_width():
    from openthree.channels import _qrencode

    matrix = _qrencode.encode_matrix(REAL_LOGIN_URL)
    wide = _qr.render_matrix(matrix, style="full", max_width=120)
    assert wide and len(wide.splitlines()[0]) <= 120
    narrow = _qr.render_matrix(matrix, style="full", max_width=86, min_quiet=1)
    assert narrow and len(narrow.splitlines()[0]) == 2 * (41 + 2)
    # 静默区收到底还装不下：宁可不画（折行的码扫不出来），让调用方给路径
    assert _qr.render_matrix(matrix, style="full", max_width=40, min_quiet=1) == ""


