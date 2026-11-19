"""A2A 频道的测试：不联网、不依赖真实模型，只用标准库起真 socket。

被保护的不变量（动这个文件/这个频道之前先读一遍）：

1. **端口的两种模式互斥**：人给了端口就只听那一个，被占**报错**；没给才按
   `last_port` → 9000–9999 依次 bind。写成「候选序列 + 降级」等于把
   「我指定了 9000」变成一句谎话，所以这条有一个决定性的用例
   （`test_explicit_port_never_falls_back_to_last_port`）。
2. **`allow_reuse_address` 按平台分叉**：Windows 上它允许抢占正在被监听的端口，
   自动探测会招招"成功"；POSIX 上关掉它会让紧接着的重启撞 `TIME_WAIT`。
3. **答案进 `artifacts[].parts[].text`**：面板只从那里取文本，只有 `status.message`
   的 task 在界面上是一片空白。
4. **`card_url` 与 `agent_id` 一起存、一起比**：`id` 由 URL 派生，只比 id 会跳过
   注册、留下一条永远 `down` 的旧记录。
5. **bridge 自己塞的话不是结论**：命中 `NOTICE_PREFIX` 的回复记 `failed`，
   否则对面拿到一条"成功"的空答案。
6. **`message/send` 缺省就把答案等回来**（与 A2A 规范里 `blocking` 缺省 false 相反）：
   有的客户端收到 `working` 之后**再也不问**（OACP 面板的「刷新」只重读它自己的
   账本），答案只有放进这个响应才看得见。显式 `blocking: false` 才是「立刻 ack，
   我自己轮询」。`_channel()` 把窗口钉成 0（等价于不要等），所以断言 ack 路径的
   用例都不慢；断言等待路径的三个用例自己设 `block_wait`。
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from openthree.channels import a2a as a2a_mod
from openthree.channels.a2a import (CARD_PATH, DEFAULT_CARD_DESCRIPTION, MAX_BODY_BYTES,
                                    RPC_PATH, A2aChannel, A2aError, build_card,
                                    message_parts, message_text)
from openthree.channels.base import InboundMessage
from openthree.channels.bridge import (NOTICE_PREFIX, SHUTDOWN_NOTICE, AgentBridge,
                                       compose_input)
from openthree.channels.config import ChannelConfig, ChannelsConfig
from openthree.channels.manager import ChannelManager, apply_extra
from openthree.progress import Progress

# --- 小工具 ---------------------------------------------------------------
def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _occupy(port: int):
    """占住一个端口（listen 着，就像另一个进程正在跑）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", int(port)))
    sock.listen(1)
    try:
        yield sock
    finally:
        sock.close()


def _three_free_ports() -> int:
    """找一个 p，使 p / p+1 / p+2 都空闲（自动模式的小范围测试要用）。"""
    for _ in range(200):
        base = _free_port()
        socks = []
        try:
            for i in range(3):
                s = socket.socket()
                s.bind(("127.0.0.1", base + i))
                socks.append(s)
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
        return base
    raise RuntimeError("找不到三个连续空闲端口")


def _channel(tmp_path: Path, **options: str) -> A2aChannel:
    opts = {"no_register": "true", "port": "0", "block_wait": "0"}
    opts.update({k: v for k, v in options.items()})
    return A2aChannel("a2a", opts, workspace_dir=tmp_path / ".openthree")


def _start(channel: A2aChannel, timeout: float = 5.0) -> threading.Thread:
    thread = threading.Thread(target=channel.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if channel.port:
            return thread
        if not thread.is_alive():
            break
        time.sleep(0.01)
    raise AssertionError("A2A 频道没有在 5 秒内开始监听")


def _http(url: str, payload=None, method: str = "") -> tuple[int, str]:
    import urllib.error
    import urllib.request

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method or ("POST" if payload else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def _rpc(method: str, params: dict | None = None, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def _call(channel: A2aChannel, method: str, params: dict | None = None,
          caller: str = "127.0.0.1") -> tuple[int, dict]:
    raw = json.dumps(_rpc(method, params)).encode("utf-8")
    return channel.handle_rpc(raw, caller=caller)


def _user_msg(text: str, **extra) -> dict:
    message = {"role": "user", "parts": [{"kind": "text", "text": text}]}
    message.update(extra)
    return message


def _text_of(task: dict) -> str:
    return task["artifacts"][0]["parts"][0]["text"]


# --- 卡片 ---------------------------------------------------------------
def test_card_carries_what_registry_and_panel_need():
    card = build_card(name="小明", url="http://127.0.0.1:9000/a2a")
    assert card["name"] == "小明"
    assert card["protocolVersion"] == "0.3.0"
    # ADP 严格校验要 supportedInterfaces；老客户端只认 url —— 两个都要、且同一地址
    assert card["url"] == "http://127.0.0.1:9000/a2a"
    assert card["supportedInterfaces"] == [
        {"url": card["url"], "protocolBinding": "JSONRPC", "protocolVersion": "0.3.0"}]
    assert isinstance(card["capabilities"], dict)
    assert card["capabilities"]["streaming"] is False
    assert card["defaultInputModes"] and card["defaultOutputModes"]
    assert card["skills"] and card["skills"][0]["id"] and card["skills"][0]["name"]
    # 人类裁决：不做卡片签名（不是「以后再说」），所以卡片里不该出现这个字段
    assert "signatures" not in card


def test_card_name_follows_identity_json(tmp_path):
    ws_dir = tmp_path / ".openthree"
    ws_dir.mkdir(parents=True)
    (ws_dir / "identity.json").write_text(json.dumps({"name": "会重命名的它"}),
                                          encoding="utf-8")
    ch = _channel(tmp_path)
    ch._load_options()
    ch.card_url = "http://127.0.0.1:9000/a2a"
    assert ch.card()["name"] == "会重命名的它"
    # 每次 GET 现读：改名之后下一次探测就看到新的名字
    (ws_dir / "identity.json").write_text(json.dumps({"name": "新名字"}), encoding="utf-8")
    assert ch.card()["name"] == "新名字"


def test_card_description_follows_motto(tmp_path):
    """description 是智能体对自己的定义：写了格言就用格言，没写才说内核能干什么。

    与名字同一条读取路径，所以「改格言」与「改名」一样，下一次 GET 就生效，
    不需要重启频道；格言被清掉时回退固定说明，卡片上永远不出现空 description。
    """
    ws_dir = tmp_path / ".openthree"
    ws_dir.mkdir(parents=True)
    ch = _channel(tmp_path)
    ch._load_options()
    ch.card_url = "http://127.0.0.1:9000/a2a"
    assert ch.card()["description"] == DEFAULT_CARD_DESCRIPTION
    (ws_dir / "identity.json").write_text(
        json.dumps({"name": "O3", "motto": "openthree 开发者"}), encoding="utf-8")
    assert ch.card()["description"] == "openthree 开发者"
    (ws_dir / "identity.json").write_text(
        json.dumps({"name": "O3", "motto": "第二句格言"}), encoding="utf-8")
    assert ch.card()["description"] == "第二句格言"
    # `set_identity("O3")` 不带 motto 会把格言清成 null —— 这时卡片要说回兜底文案
    (ws_dir / "identity.json").write_text(
        json.dumps({"name": "O3", "motto": None}), encoding="utf-8")
    assert ch.card()["description"] == DEFAULT_CARD_DESCRIPTION


def test_inbound_label_says_a2a():
    msg = InboundMessage(text="x", channel="a2a", channel_type="a2a", user_name="外部智能体")
    assert "A2A" in msg.label()


# --- HTTP 层 --------------------------------------------------------------
def test_serves_card_and_404(tmp_path):
    ch = _channel(tmp_path)
    thread = _start(ch)
    try:
        base = f"http://127.0.0.1:{ch.port}"
        status, body = _http(base + CARD_PATH)
        assert status == 200
        assert json.loads(body)["url"] == ch.card_url
        assert _http(base + "/nope")[0] == 404
    finally:
        ch.stop()
        thread.join(5)
        assert not thread.is_alive()


def test_oversized_body_is_rejected_without_reading_it(tmp_path):
    ch = _channel(tmp_path)
    thread = _start(ch)
    try:
        sock = socket.create_connection(("127.0.0.1", ch.port), timeout=5)
        try:
            # 只发头、不发正文：服务端必须在读正文**之前**就拒掉
            sock.sendall(b"POST /a2a HTTP/1.1\r\nHost: x\r\nContent-Length: "
                         + str(MAX_BODY_BYTES + 1).encode() + b"\r\n\r\n")
            first = sock.recv(200)
        finally:
            sock.close()
        assert b"413" in first.split(b"\r\n")[0]
    finally:
        ch.stop()
        thread.join(5)


# --- 任务流 ---------------------------------------------------------------
def test_chunked_body_is_refused_instead_of_desyncing(tmp_path):
    """分块请求没有 `Content-Length`：body 不读走就交给 keep-alive 去解析 = 请求走私。

    411 让客户端别试了，并且（因为 411 ≥ 400）顺手关连接 —— 残留的 chunked 字节
    不会被当成下一个请求的请求行。
    """
    ch = _channel(tmp_path)
    thread = _start(ch)
    try:
        sock = socket.create_connection(("127.0.0.1", ch.port), timeout=5)
        try:
            sock.sendall(b"POST /a2a HTTP/1.1\r\nHost: x\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n"
                         b"5\r\nhello\r\n0\r\n\r\n")
            data = b""
            try:
                while True:                 # 读到 EOF：服务端必须主动关连接
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pytest.fail("服务端没关连接 —— 残留的 chunked 字节会被当成下一个请求行")
        finally:
            sock.close()
        assert b"411" in data.split(b"\r\n")[0]
    finally:
        ch.stop()
        thread.join(5)


def test_send_then_answer_lands_in_artifacts(tmp_path):
    ch = _channel(tmp_path)
    thread = _start(ch)
    try:
        base = f"http://127.0.0.1:{ch.port}"
        status, body = _http(base + RPC_PATH,
                             _rpc("message/send", {"message": _user_msg("你好")}))
        assert status == 200
        task = json.loads(body)["result"]
        # 这条测的是**异步**路径（窗口被 `_channel()` 钉成 0，等价于调用方显式
        # `blocking:false`）：立刻回 working，跑完让客户端自己 tasks/get。
        # 「缺省就把答案等回来」由下面的 blocking 一节守着。
        assert task["status"]["state"] == "working"
        assert task["artifacts"] == []
        assert task["history"][0]["parts"][0]["text"] == "你好"

        ch.send_text(InboundMessage(text="", channel="a2a", reply_token=task["id"]), "答案在这里")
        status, body = _http(base + RPC_PATH, _rpc("tasks/get", {"id": task["id"]}, rid=2))
        done = json.loads(body)["result"]
        assert done["status"]["state"] == "completed"
        # 面板只从 artifact 取文本 —— 这条断言就是「面板里不会是一片空白」
        assert _text_of(done) == "答案在这里"
        assert done["history"][-1]["role"] == "agent"
    finally:
        ch.stop()
        thread.join(5)


def test_notice_from_bridge_is_failed_not_completed(tmp_path):
    """桥的通知走**带外标记**（`raw["notice"]`），记 failed 而不是一次"成功"。"""
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "message/send", {"message": _user_msg("你好")})
    task_id = body["result"]["id"]
    notice = NOTICE_PREFIX + "正在退出，这条消息没有处理，请稍后重发。"
    ch.send_text(InboundMessage(text="", channel="a2a", reply_token=task_id,
                                raw={"notice": notice}), notice)
    _, body = _call(ch, "tasks/get", {"id": task_id})
    assert body["result"]["status"]["state"] == "failed"
    assert "退出" in _text_of(body["result"])


def test_model_answer_that_starts_with_the_notice_prefix_is_still_an_answer(tmp_path):
    """反过来的一条：模型完全可能把 `[openthree] ` 当作答案开头（它正在解释这个前缀）。
    只按文本前缀判会把一条真答案误记成失败，所以判的是标记。"""
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "message/send", {"message": _user_msg("你用什么前缀？")})
    task_id = body["result"]["id"]
    answer = NOTICE_PREFIX + "就是我自己提示里的那个前缀。"
    ch.send_text(InboundMessage(text="", channel="a2a", reply_token=task_id), answer)
    _, body = _call(ch, "tasks/get", {"id": task_id})
    assert body["result"]["status"]["state"] == "completed"
    assert _text_of(body["result"]) == answer


def test_bridge_marks_its_notices_out_of_band():
    """标记是桥写的，不是频道猜的 —— 这条守着两边的那份契约。"""
    got = []
    bridge = AgentBridge(rt=None, echo=False)
    msg = InboundMessage(text="hi", raw={})
    bridge.submit(msg, lambda m, text: got.append((m, text)))
    bridge.stop()                       # 关掉 → 队列里那条收到 SHUTDOWN_NOTICE
    assert got and got[0][1] == SHUTDOWN_NOTICE
    assert got[0][0].raw.get("notice") == SHUTDOWN_NOTICE


def test_empty_answer_is_failed(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "message/send", {"message": _user_msg("你好")})
    task_id = body["result"]["id"]
    ch.send_text(InboundMessage(text="", channel="a2a", reply_token=task_id), "   ")
    _, body = _call(ch, "tasks/get", {"id": task_id})
    assert body["result"]["status"]["state"] == "failed"


def test_late_answer_after_terminal_is_ignored(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "message/send", {"message": _user_msg("你好")})
    task_id = body["result"]["id"]
    msg = InboundMessage(text="", channel="a2a", reply_token=task_id)
    ch.send_text(msg, "第一个答案")
    ch.send_text(msg, "迟到的第二个答案")
    _, body = _call(ch, "tasks/get", {"id": task_id})
    assert _text_of(body["result"]) == "第一个答案"


def test_follow_up_reuses_task_and_prefixes_only_the_outgoing_text(tmp_path):
    """历史里只存**原文**：前缀写给这次 run，不写回历史（否则逐轮翻倍）。"""
    ch = _channel(tmp_path)
    ch._load_options()
    seen: list[str] = []
    ch._on_message = lambda msg: seen.append(msg.text)
    _, first = _call(ch, "message/send", {"message": _user_msg("第一条")})
    task_id = first["result"]["id"]
    _, second = _call(ch, "message/send",
                      {"message": _user_msg("第二条", taskId=task_id)})
    _, third = _call(ch, "message/send",
                     {"message": _user_msg("第三条", taskId=task_id)})
    assert second["result"]["id"] == task_id          # 同一个 task
    history = third["result"]["history"]
    # 原文一字不改地留在历史里
    assert [h["parts"][0]["text"] for h in history] == ["第一条", "第二条", "第三条"]
    # 前缀只出现在**这次的输入**里，并且只包一层
    assert "第一条" in seen[1] and "第二条" in seen[1]
    assert seen[2].count("（这是同一个任务里的追加消息") == 1
    assert "第一条" in seen[2] and "第二条" in seen[2]


def test_follow_up_after_terminal_state_runs_again_and_keeps_the_new_answer(tmp_path):
    """终态 task 上的 follow up 必须复位再跑。

    不复位时：`_set_state` / `_finish` 都因「已是终态」直接返回，新答案被丢弃，
    而 blocking 的调用方立刻拿到上一次的**旧**答案。
    """
    ch = _channel(tmp_path)
    ch._load_options()
    _, first = _call(ch, "message/send", {"message": _user_msg("第一轮")})
    task_id = first["result"]["id"]
    ch.send_text(InboundMessage(text="", channel="a2a", reply_token=task_id), "第一个答案")
    _, done = _call(ch, "tasks/get", {"id": task_id})
    assert done["result"]["status"]["state"] == "completed"

    ch.block_wait = 5.0
    ch._on_message = lambda msg: ch.send_text(msg, "第二个答案")
    _, again = _call(ch, "message/send",
                     {"message": _user_msg("再来一轮", taskId=task_id),
                      "configuration": {"blocking": True}})
    assert again["result"]["status"]["state"] == "completed"
    assert _text_of(again["result"]) == "第二个答案"


def test_follow_up_while_previous_run_is_running_ignores_the_old_answer(tmp_path):
    """桥是串行的：上一轮的答案可能在 follow up **之后**才回到同一个 task。

    没有 run id 时它先到先得 —— 问题「问 A 答 B」，而这一轮的真答案被丢掉。
    """
    ch = _channel(tmp_path)
    ch._load_options()
    captured: list[InboundMessage] = []
    ch._on_message = lambda msg: captured.append(msg)
    _, first = _call(ch, "message/send", {"message": _user_msg("第一轮")})
    task_id = first["result"]["id"]
    _, second = _call(ch, "message/send",
                      {"message": _user_msg("第二轮", taskId=task_id)})
    assert second["result"]["status"]["state"] == "working"

    ch.send_text(captured[0], "第一轮的答案")          # 上一轮的答案晚到了
    _, body = _call(ch, "tasks/get", {"id": task_id})
    assert body["result"]["status"]["state"] == "working"     # 不能被它提前收工

    ch.send_text(captured[1], "第二轮的答案")
    _, body = _call(ch, "tasks/get", {"id": task_id})
    assert body["result"]["status"]["state"] == "completed"
    assert _text_of(body["result"]) == "第二轮的答案"


# --- 协议边角 -------------------------------------------------------------
def test_bad_json_is_parse_error(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    status, body = ch.handle_rpc(b"{ not json", caller="x")
    assert status == 200                                # HTTP 200 + JSON-RPC error
    assert body["error"]["code"] == -32700


def test_batch_array_is_rejected(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    raw = json.dumps([_rpc("tasks/get", {"id": "x"})]).encode()
    _, body = ch.handle_rpc(raw)
    assert body["error"]["code"] == -32600


def test_missing_message_is_invalid_params(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "message/send", {})
    assert body["error"]["code"] == -32602
    _, body = _call(ch, "message/send", {"message": {"role": "user", "parts": []}})
    assert body["error"]["code"] == -32602


def test_unknown_method_is_minus_32601(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    for method in ("message/stream", "tasks/pushNotification", "whatever"):
        _, body = _call(ch, method, {})
        # 客户端靠 -32601 换方言重试，所以这个方法表要窄而准
        assert body["error"]["code"] == -32601, method


def test_known_but_unsupported_operation_is_minus_32004(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "tasks/resubscribe", {"id": "x"})
    assert body["error"]["code"] == -32004


def test_unknown_task_is_minus_32001(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "tasks/get", {"id": "nope"})
    assert body["error"]["code"] == -32001


def test_v1_dialect_aliases_work(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    message = {"role": "ROLE_USER", "parts": [{"type": "TextPart", "text": "驼峰方言"}]}
    _, body = _call(ch, "SendMessage", {"message": message})
    assert "result" in body, body
    assert body["result"]["history"][0]["parts"][0]["text"] == "驼峰方言"
    _, body = _call(ch, "GetTask", {"id": body["result"]["id"]})
    assert body["result"]["status"]["state"] == "working"


def test_message_text_reads_all_three_shapes():
    assert message_text({"parts": [{"kind": "text", "text": "a"}]}) == "a"
    assert message_text({"parts": [{"type": "TextPart", "text": "b"}]}) == "b"
    assert message_text({"text": "c"}) == "c"
    assert message_text({"parts": [{"kind": "file", "bytes": "zz"}]}) == ""


# --- 取消 ---------------------------------------------------------------
def test_cancel_is_terminal_and_not_repeatable(tmp_path):
    """取消即终态；重复取消是 -32002。

    「取消之后还有没有可能继续烧 token」由 `tests/test_cancel.py` 守 —— 这里只管
    A2A 层的状态机：终态不可回退、不能重复取消。
    """
    ch = _channel(tmp_path, cancel_grace="0")
    ch._load_options()
    ch.emit = lambda msg: None
    _, body = _call(ch, "message/send", {"message": _user_msg("跑很久的任务")})
    task_id = body["result"]["id"]
    _, body = _call(ch, "tasks/cancel", {"id": task_id})
    assert body["result"]["status"]["state"] == "canceled"
    # 取消**是信号**：task 上那一个令牌真的被置位了（不是只改了个状态）
    assert ch._tasks.get(task_id)["_cancel"].requested is True
    # 终态不能重复取消
    _, body = _call(ch, "tasks/cancel", {"id": task_id})
    assert body["error"]["code"] == -32002


def test_stop_fails_unfinished_tasks(tmp_path):
    ch = _channel(tmp_path)
    thread = _start(ch)
    try:
        base = f"http://127.0.0.1:{ch.port}"
        _, body = _http(base + RPC_PATH, _rpc("message/send", {"message": _user_msg("没跑完")}))
        assert json.loads(body)["result"]["status"]["state"] == "working"
    finally:
        ch.stop()
        thread.join(5)
    task = ch._tasks.all()[0]
    assert task["status"]["state"] == "failed"
    assert "退出" in _text_of(task)


# --- blocking -------------------------------------------------------------
def test_blocking_true_returns_the_answer_when_it_arrives_in_time(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    ch.block_wait = 5.0
    ch._on_message = lambda msg: ch.send_text(msg, "跑完了")   # 立刻就有答案
    _, body = _call(ch, "message/send",
                    {"message": _user_msg("你好"), "configuration": {"blocking": True}})
    assert body["result"]["status"]["state"] == "completed"
    assert _text_of(body["result"]) == "跑完了"


def test_blocking_true_times_out_to_working(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    ch.block_wait = 0.2
    ch._on_message = lambda msg: None                          # 收下了，没人回
    started = time.monotonic()
    _, body = _call(ch, "message/send",
                    {"message": _user_msg("你好"), "configuration": {"blocking": True}})
    assert time.monotonic() - started >= 0.15
    # 超时不是失败：任务还在跑，客户端自己 tasks/get
    assert body["result"]["status"]["state"] == "working"


def test_absent_blocking_waits_and_hands_the_answer_back(tmp_path):
    """缺省就等 —— 这条守着实测出来的缺口（OACP 面板从不轮询）。

    对方收到 `working` 之后再也不会问（它的「刷新」只重读自己的账本），答案只有
    进这个响应才看得见；回得早等于把结论丢进没人读的地方：任务 5 秒跑完，面板上
    却永远「执行中」。
    """
    ch = _channel(tmp_path)
    ch._load_options()
    ch.block_wait = 5.0
    ch._on_message = lambda msg: ch.send_text(msg, "跑完了")
    _, body = _call(ch, "message/send", {"message": _user_msg("你好")})
    assert body["result"]["status"]["state"] == "completed"
    assert _text_of(body["result"]) == "跑完了"


def test_absent_blocking_times_out_to_working(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    ch.block_wait = 0.2
    ch._on_message = lambda msg: None                          # 收下了，没人回
    started = time.monotonic()
    _, body = _call(ch, "message/send", {"message": _user_msg("你好")})
    assert time.monotonic() - started >= 0.15
    # 超时不是失败：任务还在跑，之后一次 tasks/get 仍能拿到结论
    assert body["result"]["status"]["state"] == "working"


def test_explicit_false_returns_without_waiting(tmp_path):
    """显式 `blocking: false` 的调用方知道自己要什么（它有能力轮询）—— 立刻 ack。"""
    ch = _channel(tmp_path)
    ch._load_options()
    ch.block_wait = 5.0
    ch._on_message = lambda msg: None
    started = time.monotonic()
    _, body = _call(ch, "message/send",
                    {"message": _user_msg("你好"), "configuration": {"blocking": False}})
    assert time.monotonic() - started < 1.0
    assert body["result"]["status"]["state"] == "working"


def test_unreadable_blocking_value_waits_rather_than_guessing(tmp_path):
    """读不懂的值（字符串 `true` 之类）按「等」处理。

    赌错这一边的代价只是慢一点；赌错另一边，对方永远看不到结果。
    """
    ch = _channel(tmp_path)
    ch._load_options()
    ch.block_wait = 0.2
    ch._on_message = lambda msg: None
    started = time.monotonic()
    _call(ch, "message/send",
          {"message": _user_msg("你好"), "configuration": {"blocking": "true"}})
    assert time.monotonic() - started >= 0.15


def test_default_block_wait_stays_under_the_panel_client_timeout():
    """默认窗口必须小于对面客户端的超时（OACP 的 `a2a_timeout` 默认 60 秒）：
    越过它，一个一定能返回的 `working` 会变成对方眼里的连接超时。"""
    from openthree.channels.a2a import DEFAULT_BLOCK_WAIT

    assert 0 < DEFAULT_BLOCK_WAIT < 60


# --- 端口：显式模式 -------------------------------------------------------
def test_explicit_port_is_used_as_is(tmp_path):
    port = _free_port()
    ch = _channel(tmp_path, port=str(port))
    server = ch.bind_server()
    try:
        assert server.server_address[1] == port
        assert ch.mode == "explicit"
    finally:
        server.server_close()


def test_explicit_port_taken_raises(tmp_path):
    with _occupy(_free_port()) as sock:
        port = sock.getsockname()[1]
        ch = _channel(tmp_path, port=str(port))
        with pytest.raises(A2aError) as exc:
            ch.bind_server()
    message = str(exc.value)
    assert "被占用" in message
    assert "不会自动换" in message        # 错误信息必须说清「不会偷偷漂」


def test_explicit_port_never_falls_back_to_last_port(tmp_path):
    """决定性的一条：`last_port` 空闲也不能用它 —— 显式模式没有降级路径。

    写成「候选序列 + 降级」时，这条会起在 `last_port` 上、测试变红。
    """
    base = _three_free_ports()
    with _occupy(base) as sock:
        taken = sock.getsockname()[1]
        ch = _channel(tmp_path, port=str(taken))
        ch._load_state()
        ch._state["last_port"] = base + 1          # 空闲，且是候选序列的第 2 位
        with pytest.raises(A2aError):
            ch.bind_server()


def test_explicit_port_taken_in_yaml_behaves_the_same(tmp_path):
    """CLI 与 `channels.yaml` 给的端口地位相同 —— 都是「人写的值」。"""
    port = _free_port()
    cfg = ChannelsConfig(path=tmp_path / "channels.yaml", channels=[
        ChannelConfig(name="a2a", type="a2a", options={"port": str(port)})])
    ch = a2a_mod.A2aChannel("a2a", dict(cfg.channels[0].options),
                            workspace_dir=tmp_path / ".openthree")
    with _occupy(port):
        with pytest.raises(A2aError):
            ch.bind_server()


# --- 端口：自动模式 -------------------------------------------------------
def test_auto_mode_skips_taken_port(tmp_path, monkeypatch):
    base = _three_free_ports()
    monkeypatch.setattr(a2a_mod, "PORT_RANGE", (base, base + 2))
    with _occupy(base):
        ch = _channel(tmp_path, port="")
        server = ch.bind_server()
        try:
            assert server.server_address[1] == base + 1
            assert ch.mode == "auto"
        finally:
            server.server_close()


def test_auto_mode_reuses_last_port(tmp_path, monkeypatch):
    """「别让自己漂」：正常重启复用上次端口 ⇒ 同一 card_url ⇒ 同一个 registry 条目。"""
    base = _three_free_ports()
    monkeypatch.setattr(a2a_mod, "PORT_RANGE", (base, base + 2))
    ch = _channel(tmp_path, port="")
    ch._load_state()
    ch._state["last_port"] = base + 2              # 不是范围里最小的那个
    server = ch.bind_server()
    try:
        assert server.server_address[1] == base + 2
    finally:
        server.server_close()


def test_auto_mode_all_taken_lists_every_port(tmp_path, monkeypatch):
    base = _three_free_ports()
    monkeypatch.setattr(a2a_mod, "PORT_RANGE", (base, base + 2))
    socks = []
    try:
        for i in range(3):
            s = socket.socket()
            s.bind(("127.0.0.1", base + i))
            s.listen(1)
            socks.append(s)
        ch = _channel(tmp_path, port="")
        with pytest.raises(A2aError) as exc:
            ch.bind_server()
        message = str(exc.value)
        for i in range(3):
            assert str(base + i) in message
    finally:
        for s in socks:
            s.close()


def test_corrupt_last_port_does_not_abort_auto_mode(tmp_path, monkeypatch):
    """手改坏的状态文件只该让**这一项**失效，不该让整个自动模式起不来。"""
    base = _three_free_ports()
    monkeypatch.setattr(a2a_mod, "PORT_RANGE", (base, base + 1))
    ch = _channel(tmp_path, port="")
    ch._load_state()
    ch._state["last_port"] = "not-a-port"
    server = ch.bind_server()
    try:
        assert server.server_address[1] in (base, base + 1)
    finally:
        server.server_close()


def test_bad_advertise_port_is_a_config_error(tmp_path):
    """`http://host:abc` 会让 urlsplit 抛裸 ValueError —— 该是带人话的 A2aError。"""
    ch = _channel(tmp_path, port="0", advertise="http://example.com:abc")
    with pytest.raises(A2aError) as exc:
        ch.run()
    assert "advertise" in str(exc.value)


def test_allow_reuse_address_follows_platform(tmp_path):
    """探测不会「假成功」的唯一保证。

    Windows 上 `SO_REUSEADDR` 允许抢占正在被监听的端口 —— 保持为真时，
    「被占用」永远不报错，自动探测招招成功；POSIX 上关掉它会让紧接着的重启
    撞 `TIME_WAIT`，端口被迫漂移。两侧都不能想当然。
    """
    ch = _channel(tmp_path)
    server = ch.bind_server()
    try:
        assert server.allow_reuse_address == (not sys.platform.startswith("win"))
    finally:
        server.server_close()


# --- 生命周期 -------------------------------------------------------------
def test_stop_before_run_is_safe(tmp_path):
    """`stop()` 早于 `serve_forever` 时不能死锁（manager.stop 可能在 bind 期间就来了）。"""
    ch = _channel(tmp_path)
    ch.stop()
    ch.run()                                        # 绑定后立刻发现已 stop，直接返回


def test_stop_makes_run_return_within_five_seconds(tmp_path):
    ch = _channel(tmp_path)
    thread = _start(ch)
    started = time.monotonic()
    ch.stop()
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 5


# --- 注册 ----------------------------------------------------------------
class _FakeRegistry:
    """假 registry：记下每次调用，按 `routes` 回状态码。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.routes: dict[tuple[str, str], tuple[int, dict]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):          # noqa: A003 - 别刷屏
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                path = urlsplit(self.path).path
                outer.calls.append((method, path, body))
                status, payload = outer.resolve(method, path)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):      # noqa: N802
                self._handle("GET")

            def do_POST(self):     # noqa: N802
                self._handle("POST")

            def do_DELETE(self):   # noqa: N802
                self._handle("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

    def resolve(self, method: str, path: str) -> tuple[int, dict]:
        for (m, prefix), result in self.routes.items():
            if m == method and path.startswith(prefix):
                return result
        return 404, {"error": "not found"}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextlib.contextmanager
def _registry():
    reg = _FakeRegistry()
    try:
        yield reg
    finally:
        reg.close()


def _reg_channel(tmp_path: Path, registry_base: str, base_url: str,
                 **options: str) -> A2aChannel:
    """构造一个「已经在跑」的频道，地址从 base_url 派生。

    **两个地址必须分开设**：`card_url` 是 RPC 端点（写进卡片的 url 字段，客户端
    调它），`card_endpoint` 是交给 registry 的卡片地址。两者混成一件事，注册会
    稳定地拿到 422 `CARD_UNREACHABLE` —— registry 会去 GET 一个只收 POST 的路径。
    """
    ch = _channel(tmp_path, registry=registry_base, no_register="false", **options)
    ch._load_options()
    ch._load_state()
    ch.base_url = base_url.rstrip("/")
    ch.card_url = ch.base_url + a2a_mod.RPC_PATH
    ch.card_endpoint = ch.base_url + a2a_mod.CARD_PATH
    return ch


def test_register_sends_the_card_endpoint_not_the_card(tmp_path):
    with _registry() as reg:
        reg.routes[("POST", "/v1/agents")] = (201, {"id": "a_1", "manageToken": "tok"})
        ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9000")
        ch._registration_tick()
        method, path, body = reg.calls[-1]
        assert (method, path) == ("POST", "/v1/agents")
        # 交的只是地址（registry 自己去 GET 卡片），而且必须是**卡片地址**。
        assert body == {"url": "http://127.0.0.1:9000/.well-known/agent-card.json"}
        assert body["url"] != ch.card_url          # 卡片地址 ≠ RPC 端点
        assert ch._state["agent_id"] == "a_1"
        assert ch._state["manage_token"] == "tok"
        assert ch._state["card_endpoint"] == "http://127.0.0.1:9000/.well-known/agent-card.json"


def test_only_the_card_endpoint_answers_a_GET(tmp_path):
    """交错的地址会**真的**失败，不是措辞问题。

    上面那条守「我们交了什么」，这条守**为什么**必须那样交：`/a2a` 只收 POST，
    registry 拿它当卡片地址去 GET 只会 404（OACP 那边回 422 CARD_UNREACHABLE）。
    """
    ch = _channel(tmp_path, port="0")
    th = _start(ch)
    try:
        assert _http(ch.card_endpoint)[0] == 200
        assert _http(ch.card_url)[0] == 404
        assert ch.card_endpoint != ch.card_url
    finally:
        ch.stop()
        th.join(timeout=5)


def test_startup_gets_instead_of_posting_when_nothing_changed(tmp_path):
    with _registry() as reg:
        reg.routes[("GET", "/v1/agents/a_1")] = (200, {"id": "a_1"})
        ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9000")
        ch._state = {"agent_id": "a_1",
                     "card_endpoint": "http://127.0.0.1:9000/.well-known/agent-card.json"}
        ch._registration_tick()
        # 先 GET 再 POST：ADP 按来源 IP 限流 20 次/小时，秒退秒起的循环会被限死
        assert [c[0] for c in reg.calls] == ["GET"]


def test_missing_entry_is_registered_again(tmp_path):
    with _registry() as reg:
        reg.routes[("POST", "/v1/agents")] = (201, {"id": "a_1", "manageToken": "tok"})
        ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9000")
        ch._state = {"agent_id": "a_old",
                     "card_endpoint": "http://127.0.0.1:9000/.well-known/agent-card.json"}
        ch._registration_tick()                    # GET 404 → 重新 POST
        assert [c[0] for c in reg.calls] == ["GET", "POST"]


def test_card_endpoint_change_forces_register_even_if_id_still_answers(tmp_path):
    """`id` 是**旧地址**派生的：只比 id 会跳过注册、留下一条永远 down 的记录。"""
    with _registry() as reg:
        reg.routes[("GET", "/v1/agents/a_1")] = (200, {"id": "a_1"})
        reg.routes[("POST", "/v1/agents")] = (201, {"id": "a_2", "manageToken": "tok2"})
        ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9001")
        ch._state = {"agent_id": "a_1",
                     "card_endpoint": "http://127.0.0.1:9000/.well-known/agent-card.json",
                     "manage_token": "tok1"}
        ch._registration_tick()
        assert ("POST", "/v1/agents") in [(c[0], c[1]) for c in reg.calls]
        assert ch._state["agent_id"] == "a_2"


def test_old_entry_is_buried_when_it_is_really_gone(tmp_path):
    with _registry() as reg:
        reg.routes[("POST", "/v1/agents")] = (201, {"id": "a_2", "manageToken": "tok2"})
        reg.routes[("DELETE", "/v1/agents/a_1")] = (204, {})
        dead = f"http://127.0.0.1:{_free_port()}/.well-known/agent-card.json"   # 没人听
        ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9001", port="")
        ch._state = {"agent_id": "a_1", "card_endpoint": dead, "manage_token": "tok1"}
        ch._registration_tick()
        deletes = [c for c in reg.calls if c[0] == "DELETE"]
        assert deletes and deletes[0][1] == "/v1/agents/a_1"


def test_old_entry_is_kept_when_something_still_answers_there(tmp_path):
    """旧地址上还有活的东西（很可能是另一个 openthree）→ 删了比留僵尸糟得多。"""
    with _registry() as reg:
        with _registry() as alive:            # 一个「还活着」的旧地址
            reg.routes[("POST", "/v1/agents")] = (201, {"id": "a_2", "manageToken": "tok2"})
            ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9001", port="")
            ch._state = {"agent_id": "a_1",
                         "card_endpoint": alive.base + "/.well-known/agent-card.json",
                         "manage_token": "tok1"}
            ch._registration_tick()
            assert not [c for c in reg.calls if c[0] == "DELETE"]


def test_burial_skipped_when_unreadable_401_is_reported_not_fatal(tmp_path):
    with _registry() as reg:
        reg.routes[("POST", "/v1/agents")] = (201, {"id": "a_2", "manageToken": "tok2"})
        reg.routes[("DELETE", "/v1/agents/a_1")] = (401, {"error": "not yours"})
        dead = f"http://127.0.0.1:{_free_port()}/.well-known/agent-card.json"
        ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9001", port="")
        ch._state = {"agent_id": "a_1", "card_endpoint": dead, "manage_token": "tok1"}
        ch._registration_tick()               # 不抛：401 只是一个要说明的答案
        assert ch._state["agent_id"] == "a_2"


def test_explicit_port_never_buries(tmp_path):
    with _registry() as reg:
        reg.routes[("POST", "/v1/agents")] = (201, {"id": "a_2", "manageToken": "tok2"})
        dead = f"http://127.0.0.1:{_free_port()}/.well-known/agent-card.json"
        ch = _reg_channel(tmp_path, reg.base, "http://127.0.0.1:9000",
                          port=str(_free_port()))
        ch._state = {"agent_id": "a_1", "card_endpoint": dead, "manage_token": "tok1"}
        ch._registration_tick()
        # 显式端口不漂 ⇒ 没有旧条目可收尸（人改了端口就是一次明确的配置变更）
        assert not [c for c in reg.calls if c[0] == "DELETE"]


def test_registry_down_does_not_raise_transport_error_into_the_service(tmp_path):
    ch = _reg_channel(tmp_path, f"http://127.0.0.1:{_free_port()}", "http://127.0.0.1:9000")
    with pytest.raises(a2a_mod._TransportError):
        ch._registration_tick()            # 注册失败只该让这条路径退避，不挡服务


# --- 装配 ----------------------------------------------------------------
def test_apply_extra_replaces_same_type_and_keeps_config_options():
    cfg = ChannelsConfig(path=Path("channels.yaml"), channels=[
        ChannelConfig(name="mine", type="a2a",
                      options={"registry": "http://10.0.0.5:8000", "host": "127.0.0.1"})])
    notes = apply_extra(cfg, [ChannelConfig(name="a2a", type="a2a", options={"port": "9000"})])
    # 同一进程里只留一个 a2a 实例（两个会各自抢端口、各自注册成一条记录）
    assert [c.name for c in cfg.channels] == ["mine"]
    assert cfg.channels[0].options == {"registry": "http://10.0.0.5:8000",
                                       "host": "127.0.0.1", "port": "9000"}
    assert notes and "mine" in notes[0]


def test_apply_extra_appends_when_nothing_to_replace():
    cfg = ChannelsConfig(path=Path("channels.yaml"), channels=[])
    apply_extra(cfg, [ChannelConfig(name="a2a", type="a2a", options={})])
    assert [c.name for c in cfg.channels] == ["a2a"]


def test_apply_extra_does_not_overwrite_a_channel_of_another_type():
    cfg = ChannelsConfig(path=Path("channels.yaml"), channels=[
        ChannelConfig(name="a2a", type="dingtalk",
                      options={"client_id": "id", "client_secret": "s"})])
    apply_extra(cfg, [ChannelConfig(name="a2a", type="a2a", options={})])
    assert [c.type for c in cfg.channels] == ["dingtalk", "a2a"]
    assert cfg.channels[1].name == "a2a-2"


def test_only_exclusion_is_recorded_not_silent():
    """`-A2A --only wx` 曾经会静默丢掉 A2A 频道 —— 静默是这条判据的反面。"""
    cfg = ChannelsConfig(path=Path("channels.yaml"), channels=[
        ChannelConfig(name="a2a", type="a2a", options={})])
    mgr = ChannelManager(cfg, bridge=None, only=["wx"])
    mgr.build()
    assert mgr.statuses == []
    assert any("a2a" in s and "--only" in s for s in mgr.skipped)


# --- 任务表上限 -----------------------------------------------------------
def test_evicted_task_gets_an_artifact_so_the_panel_is_not_blank():
    from openthree.channels.a2a import _TaskStore

    store = _TaskStore(limit=1)
    first = store.new(task_id="t-1", context_id="c-1", text="会被挤出去的那条")
    store.new(task_id="t-2", context_id="c-2", text="后来者")
    assert first["status"]["state"] == "failed"
    # 面板只从 artifact 取文本：只写 status.message 的话界面上是一片空白
    assert first["artifacts"][0]["parts"][0]["text"]
    assert first["_done"].is_set()          # 别让 blocking 的调用方一直等


# --- 命令行 --------------------------------------------------------------
def test_a2a_suboptions_without_the_switch_are_rejected(tmp_path, capsys):
    """`openthree --a2a-port 9100 -i` 曾经把参数静默丢掉 —— 用户以为端口生效了。"""
    from openthree.cli import main

    code = main(["--a2a-port", "9100", "-C", str(tmp_path)])
    assert code == 2
    assert "-A2A" in capsys.readouterr().err


def test_a2a_with_only_that_excludes_it_fails(tmp_path, capsys):
    """端到端：`-A2A --only wx` 必须报错，而不是起一个收不到任务的服务。"""
    from openthree.cli import main

    code = main(["-A2A", "--only", "wx", "-C", str(tmp_path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "A2A" in err and "--only" in err


def test_a2a_conflicts_with_upgrade_check(tmp_path, capsys):
    """`-u --check` 在 A2A 校验之前就早返回过 —— 那条缝现在堵上了。"""
    from openthree.cli import main

    code = main(["-A2A", "-u", "--check", "-C", str(tmp_path)])
    assert code == 2
    assert "-A2A" in capsys.readouterr().err


def test_wants_interactive_is_one_judgement():
    """`-A2A` 与主流程读同一个判据：不进交互就必须走 `-d`。"""
    from openthree.cli import _wants_interactive, build_parser

    parser = build_parser()
    assert _wants_interactive(parser.parse_args([])) is False        # 测试里 stdin 不是终端
    assert _wants_interactive(parser.parse_args(["-i"])) is True
    assert _wants_interactive(parser.parse_args(["--auto", "3"])) is False
    assert _wants_interactive(parser.parse_args(["--no-interactive"])) is False
    assert _wants_interactive(parser.parse_args(["跑个任务"])) is False


# --- 端口：一个绑不上的 last_port 不该拖死自动模式 -------------------------
def test_auto_mode_survives_an_unbindable_last_port(tmp_path, monkeypatch):
    """`last_port` 可能是上一次**显式**指定的特权端口（80/443）。

    今天它绑不上（EACCES）时，自动模式该继续往下试，而不是把整个频道判死。
    """
    import errno as errno_mod

    base = _three_free_ports()
    monkeypatch.setattr(a2a_mod, "PORT_RANGE", (base, base + 2))
    real = a2a_mod._Server
    broken = base + 2

    class Flaky(real):
        def __init__(self, addr, handler, channel):
            if addr[1] == broken:
                raise OSError(errno_mod.EACCES, "permission denied")
            super().__init__(addr, handler, channel)

    monkeypatch.setattr(a2a_mod, "_Server", Flaky)
    ch = _channel(tmp_path, port="")
    ch._load_state()
    ch._state["last_port"] = broken
    server = ch.bind_server()
    try:
        assert server.server_address[1] == base       # 没被 last_port 卡住
    finally:
        server.server_close()


def test_auto_mode_keeps_permission_errors_in_the_range_fatal(tmp_path, monkeypatch):
    """范围里的 EACCES 必须直接抛：否则试完 1000 个，用户只知道「全占满了」。"""
    import errno as errno_mod

    base = _three_free_ports()
    monkeypatch.setattr(a2a_mod, "PORT_RANGE", (base, base))
    real = a2a_mod._Server

    class Broken(real):
        def __init__(self, addr, handler, channel):
            raise OSError(errno_mod.EACCES, "permission denied")

    monkeypatch.setattr(a2a_mod, "_Server", Broken)
    ch = _channel(tmp_path, port="")
    with pytest.raises(A2aError) as exc:
        ch.bind_server()
    assert "无法监听" in str(exc.value)


def test_bad_advertise_releases_the_bound_port(tmp_path):
    """地址配错时已经把端口 bind 住了 —— 抛之前必须还回去，否则端口泄漏到进程结束。"""
    port = _free_port()
    ch = _channel(tmp_path, port=str(port),
                  advertise=f"http://example.com:{port + 1}")   # 与端口矛盾
    with pytest.raises(A2aError):
        ch.run()
    with _occupy(port):          # 能重新绑上 = 已经释放
        pass


# --- 回调（push notifications）-------------------------------------------
# 这里守的判据全部来自 A2A 协议本身，不来自某个具体实现：
#   * 卡片 `capabilities.pushNotifications` 为 true，客户端才会递 `{url, token}`；
#   * 配置有两个入口 —— `message/send` 里内联，与 `tasks/pushNotificationConfig/set`
#     （1.x 叫 `CreateTaskPushNotificationConfig`）；
#   * 终态时把**整个 Task 对象** POST 到那个 url，token 走 `X-A2A-Notification-Token`。
#
# 被保护的不变量：
#   6. **有回调配置时 `blocking` 缺省是「不等」**（答案另行送达），没有才等 ——
#      这是上一轮那个「面板永远执行中」的决定在协议下的正确收窄。
#   7. **回调的 body 与 `tasks/get` 的视图逐字节一致**：对面解析的是同一份东西。
#   8. **token 不进 Task 对象**（只走请求头 / 配置里），`tasks/get` 拿不到它。
#   9. **投递在另一条线程上**：接收端连不上也不拖慢 `message/send` 的响应。
def _wait_until(predicate, timeout: float = 5.0, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{timeout} 秒内没等到：{what}")


class _Receiver:
    """一个真的 HTTP 接收端：拿到什么记什么，状态码可调。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):            # 静音
                pass

            def do_POST(self):                       # noqa: N802 - 基类的约定名
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.calls.append({
                    "path": self.path,
                    "token": self.headers.get("X-A2A-Notification-Token"),
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(raw.decode("utf-8") or "null"),
                })
                payload = b"{}"
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/cb"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def wait_for(self, count: int = 1, timeout: float = 5.0) -> list[dict]:
        _wait_until(lambda: len(self.calls) >= count, timeout,
                    f"{count} 次回调（实收 {len(self.calls)} 次）")
        return self.calls

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@contextlib.contextmanager
def _receiver():
    box = _Receiver()
    try:
        yield box
    finally:
        box.close()


def _send_with_push(channel: A2aChannel, url: str, *, token: str = "",
                    text: str = "你好", **config) -> dict:
    """带内联回调地址发一条消息，返回 `message/send` 的 result。"""
    push = {"url": url}
    if token:
        push["token"] = token
    configuration = {"pushNotificationConfig": push}
    configuration.update(config)
    _, body = _call(channel, "message/send",
                    {"message": _user_msg(text), "configuration": configuration})
    return body["result"]


def test_card_advertises_push_notifications():
    """能力位就是那扇门：不置它，客户端根本不会把回调地址递过来。"""
    card = build_card(name="小明", url="http://127.0.0.1:9000/a2a")
    assert card["capabilities"]["pushNotifications"] is True


def test_inline_push_config_gets_the_finished_task_posted_back(tmp_path):
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "跑完了")
        result = _send_with_push(ch, box.url, token="t-1")
        # 答案照样进了响应（内联登记时任务已经跑完了），同时也会回调一次 ——
        # 对面是幂等的，多一条不算错，少一条才是。
        assert result["status"]["state"] == "completed"
        call = box.wait_for(1)[0]
        assert call["path"] == "/cb"
        assert call["token"] == "t-1"
        assert call["body"]["id"] == result["id"]
        assert call["body"]["status"]["state"] == "completed"
        assert _text_of(call["body"]) == "跑完了"


def test_push_payload_is_exactly_the_tasks_get_view(tmp_path):
    """同一份东西：回调 body == `tasks/get` 的 result（对面不用写两套解析）。"""
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        result = _send_with_push(ch, box.url)
        pushed = box.wait_for(1)[0]["body"]
        _, got = _call(ch, "tasks/get", {"id": result["id"]})
        assert pushed == got["result"]


@pytest.mark.parametrize("method,key", [
    ("tasks/pushNotificationConfig/set", "pushNotificationConfig"),   # A2A 0.3
    ("CreateTaskPushNotificationConfig", "config"),                    # A2A 1.x
    ("SetTaskPushNotificationConfig", "pushNotificationConfig"),       # 驼峰别名
])
def test_push_set_answers_every_dialect(tmp_path, method, key):
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: None            # 收下就跑，没人回
        _, sent = _call(ch, "message/send",
                        {"message": _user_msg("q"), "configuration": {"blocking": False}})
        task_id = sent["result"]["id"]
        _, body = _call(ch, method, {"taskId": task_id, key: {"url": box.url, "token": "x"}})
        assert body["result"]["pushNotificationConfig"]["url"] == box.url
        # 登记立刻生效：之后置终态就回调
        ch.send_text(InboundMessage(text="q", channel="a2a", channel_type="a2a",
                                    reply_token=task_id, raw={"task": task_id}), "结论")
        assert box.wait_for(1)[0]["body"]["status"]["state"] == "completed"


def test_inline_push_config_is_also_read_from_the_1x_key(tmp_path):
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        _, sent = _call(ch, "message/send", {
            "message": _user_msg("q"),
            "configuration": {"taskPushNotificationConfig": {"url": box.url}},
        })
        assert sent["result"]["status"]["state"] == "completed"
        assert box.wait_for(1)[0]["body"]["id"] == sent["result"]["id"]


def test_push_config_authentication_becomes_the_authorization_header(tmp_path):
    """`authentication.credentials` 是完整的 Authorization 头值（协议这么定义）。"""
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        _, sent = _call(ch, "message/send", {
            "message": _user_msg("q"),
            "configuration": {"pushNotificationConfig": {
                "url": box.url,
                "authentication": {"schemes": ["Bearer"], "credentials": "Bearer abc123"},
            }},
        })
        assert sent["result"]["status"]["state"] == "completed"
        assert box.wait_for(1)[0]["authorization"] == "Bearer abc123"


@pytest.mark.parametrize("url", ["ftp://example.com/cb", "/relative/cb",
                                 "example.com/cb", ""])
def test_push_url_must_be_an_absolute_http_url(tmp_path, url):
    """坏地址**当场拒**：静默收下等于承诺「会回调」，然后永远没有回调。"""
    ch = _channel(tmp_path)
    ch._load_options()
    ch._on_message = lambda msg: None
    _, sent = _call(ch, "message/send",
                    {"message": _user_msg("q"), "configuration": {"blocking": False}})
    task_id = sent["result"]["id"]
    _, body = _call(ch, "tasks/pushNotificationConfig/set",
                    {"taskId": task_id, "pushNotificationConfig": {"url": url}})
    assert body["error"]["code"] == -32602
    # 任务本身没被这个坏配置带走
    _, got = _call(ch, "tasks/get", {"id": task_id})
    assert got["result"]["status"]["state"] == "working"


def test_push_get_reports_the_registered_config_and_delete_removes_it(tmp_path):
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: None
        _, sent = _call(ch, "message/send",
                        {"message": _user_msg("q"), "configuration": {"blocking": False}})
        task_id = sent["result"]["id"]
        _call(ch, "tasks/pushNotificationConfig/set",
              {"taskId": task_id, "pushNotificationConfig": {"url": box.url, "token": "t"}})
        _, got = _call(ch, "tasks/pushNotificationConfig/get", {"taskId": task_id})
        assert got["result"] == {"taskId": task_id,
                                 "pushNotificationConfig": {"url": box.url, "token": "t"}}
        _, deleted = _call(ch, "tasks/pushNotificationConfig/delete", {"taskId": task_id})
        assert "error" not in deleted
        _, gone = _call(ch, "tasks/pushNotificationConfig/get", {"taskId": task_id})
        assert gone["error"]["code"] == -32001
        assert "回调地址" in gone["error"]["message"]      # 说清是配置没有，不是任务没有
        # 撤掉之后置终态：不该再有人收到回调
        ch.send_text(InboundMessage(text="q", channel="a2a", channel_type="a2a",
                                    reply_token=task_id, raw={"task": task_id}), "结论")
        time.sleep(0.3)
        assert box.calls == []


def test_push_delete_is_idempotent(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    ch._on_message = lambda msg: None
    _, sent = _call(ch, "message/send",
                    {"message": _user_msg("q"), "configuration": {"blocking": False}})
    task_id = sent["result"]["id"]
    _, body = _call(ch, "tasks/pushNotificationConfig/delete", {"taskId": task_id})
    assert "error" not in body


def test_push_set_for_an_unknown_task_is_32001(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "tasks/pushNotificationConfig/set",
                    {"taskId": "t-nope", "pushNotificationConfig": {"url": "http://x/cb"}})
    assert body["error"]["code"] == -32001


def test_push_list_stays_unsupported(tmp_path):
    """一个 task 只有一个回调地址，`list` 永远只有那一条 —— 拒它是准确的答复。"""
    ch = _channel(tmp_path)
    ch._load_options()
    _, body = _call(ch, "tasks/pushNotificationConfig/list", {"taskId": "t-1"})
    assert body["error"]["code"] == -32004


def test_push_token_never_leaks_into_the_task_view(tmp_path):
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        _, sent = _call(ch, "message/send", {
            "message": _user_msg("q"),
            "configuration": {"pushNotificationConfig": {"url": box.url, "token": "s3cret"}},
        })
        task_id = sent["result"]["id"]
        box.wait_for(1)
        assert "s3cret" not in json.dumps(sent, ensure_ascii=False)
        _, got = _call(ch, "tasks/get", {"id": task_id})
        assert "s3cret" not in json.dumps(got, ensure_ascii=False)


def test_blocking_defaults_to_no_wait_once_a_callback_is_registered(tmp_path):
    """登记了回调就按规范立刻 ack：答案另行送达，不必占着这条连接等。

    这是上一轮那个决定（缺省就等，因为调用方从不轮询）的**收窄**：等的理由
    是「对方拿不到答案」，它现在拿得到了。
    """
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch.block_wait = 5.0
        ch._on_message = lambda msg: None                  # 收下了，没人回
        started = time.monotonic()
        result = _send_with_push(ch, box.url)
        assert time.monotonic() - started < 1.0
        assert result["status"]["state"] == "working"


def test_explicit_blocking_true_still_waits_even_with_a_callback(tmp_path):
    """显式要等的调用方一律照办 —— 包括「登记了回调但仍然要等」。"""
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch.block_wait = 5.0
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        result = _send_with_push(ch, box.url, blocking=True)
        assert result["status"]["state"] == "completed"
        assert _text_of(result) == "答案"
        # 回调也还是会发（对面幂等；少发一条才是问题）
        assert box.wait_for(1)[0]["body"]["status"]["state"] == "completed"


def test_follow_up_reuses_the_registered_callback(tmp_path):
    """回调地址跟着 **task** 走：follow up 常常不带配置，新结论照样要送出去。"""
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        replies = iter(["第一次", "第二次"])
        ch._on_message = lambda msg: ch.send_text(msg, next(replies))
        first = _send_with_push(ch, box.url, token="t")
        box.wait_for(1)
        _, second = _call(ch, "message/send",
                          {"message": _user_msg("再问一句", taskId=first["id"])})
        assert second["result"]["id"] == first["id"]
        calls = box.wait_for(2)
        assert calls[1]["body"]["id"] == first["id"]
        assert _text_of(calls[1]["body"]) == "第二次"


def test_cancel_reports_the_canceled_state_back(tmp_path):
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: None
        result = _send_with_push(ch, box.url)
        _call(ch, "tasks/cancel", {"id": result["id"]})
        assert box.wait_for(1)[0]["body"]["status"]["state"] == "canceled"


def test_a_dead_callback_endpoint_does_not_slow_the_reply(tmp_path):
    """投递在另一条线程上：接收端连不上，`message/send` 也不该被拖着等。"""
    dead = f"http://127.0.0.1:{_free_port()}/cb"       # 没有人在听
    ch = _channel(tmp_path)
    ch._load_options()
    ch.block_wait = 5.0
    ch._on_message = lambda msg: ch.send_text(msg, "答案")
    started = time.monotonic()
    _, body = _call(ch, "message/send", {
        "message": _user_msg("q"),
        "configuration": {"blocking": True,
                          "pushNotificationConfig": {"url": dead}},
    })
    assert body["result"]["status"]["state"] == "completed"
    assert time.monotonic() - started < 2.0


def test_push_gives_up_after_the_retry_budget_on_5xx(tmp_path, monkeypatch):
    monkeypatch.setattr(a2a_mod, "PUSH_BACKOFF", (0.0, 0.0, 0.0))
    with _receiver() as box:
        box.status = 500
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        _send_with_push(ch, box.url)
        _wait_until(lambda: len(box.calls) >= a2a_mod.PUSH_TRIES, 5.0, "重试用完")
        time.sleep(0.2)
        assert len(box.calls) == a2a_mod.PUSH_TRIES
        ch._push_stop()


def test_push_does_not_retry_when_the_receiver_refuses(tmp_path, monkeypatch):
    """4xx 是「对方明确拒收」：重试只是把同一个拒绝重放几遍。"""
    monkeypatch.setattr(a2a_mod, "PUSH_BACKOFF", (0.0, 0.0, 0.0))
    with _receiver() as box:
        box.status = 404
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        _send_with_push(ch, box.url)
        box.wait_for(1)
        time.sleep(0.3)
        assert len(box.calls) == 1
        ch._push_stop()


def test_push_does_not_follow_redirects(tmp_path, monkeypatch):
    """3xx 是一次投递失败，不是一次跳转 —— 否则调用方能借我们打别处。"""
    monkeypatch.setattr(a2a_mod, "PUSH_BACKOFF", (0.0, 0.0, 0.0))

    class _Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):                       # noqa: N802
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/cb"
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案")
        _send_with_push(ch, url)
        ch._push_stop()        # 排空：3xx 会走完重试预算，不该去碰那个 Location
        assert ch._push_q.unfinished_tasks == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


# --- 跨站 / 走私 / 泄露防线（ocr 第六轮）-----------------------------------
# 这几条不是「更严一点更好」：每条都对应一个具体的、可被外部触发的症状。
def _post_raw(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def test_post_requires_json_content_type(tmp_path):
    """只听回环**挡不住浏览器**：`text/plain` 的「简单请求」不需要 preflight。

    要求 `application/json` 就够 —— 浏览器为它会先发 preflight，而本服务不实现
    OPTIONS 这个动词，请求根本发不出去。（谁将来加了 CORS，就得同时加 Host 校验。）
    """
    ch = _channel(tmp_path)
    ch._load_options()
    ch._on_message = lambda msg: None
    thread = _start(ch)
    try:
        url = f"http://127.0.0.1:{ch.port}{RPC_PATH}"
        body = json.dumps(_rpc("tasks/get", {"id": "t-1"})).encode("utf-8")
        assert _post_raw(url, body, {"Content-Type": "text/plain"})[0] == 415
        assert _post_raw(url, body, {})[0] == 415                 # 一个头都不给
        # 规范要求的那个 Content-Type（带 charset 参数）照常通
        status, text = _post_raw(url, body,
                                 {"Content-Type": "application/json; charset=utf-8"})
        assert status == 200
        assert json.loads(text)["error"]["code"] == -32001
    finally:
        ch.stop()
        thread.join(5)


def test_get_with_a_body_is_refused_instead_of_desyncing(tmp_path):
    """GET 带正文时正文没被读走 —— 不关连接就等于请对面把残留字节当下一行解析。"""
    ch = _channel(tmp_path)
    thread = _start(ch)
    try:
        sock = socket.create_connection(("127.0.0.1", ch.port), timeout=5)
        try:
            sock.sendall(b"GET " + CARD_PATH.encode() + b" HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: 5\r\n\r\nhello")
            first = sock.recv(200)
        finally:
            sock.close()
        assert b"400" in first.split(b"\r\n")[0]
    finally:
        ch.stop()
        thread.join(5)


def test_get_with_content_length_zero_is_still_fine(tmp_path):
    """`Content-Length: 0` 是合法的（有些客户端习惯带上），不能因此拒收卡片。"""
    ch = _channel(tmp_path)
    thread = _start(ch)
    try:
        sock = socket.create_connection(("127.0.0.1", ch.port), timeout=5)
        try:
            sock.sendall(b"GET " + CARD_PATH.encode() + b" HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: 0\r\n\r\n")
            first = sock.recv(400)
        finally:
            sock.close()
        assert b"200" in first.split(b"\r\n")[0]
    finally:
        ch.stop()
        thread.join(5)


def test_follow_up_does_not_wait_when_the_callback_is_registered(tmp_path):
    """「要不要等」看的是 **task** 有没有回调地址，不是这次请求有没有带。

    配置跟着 task 走（follow up 复用同一个地址），所以判据也必须跟着 task 走 ——
    只看这次请求的话，一个已经登记了回调的追加消息会白等满窗口，而答案马上就会由
    回调送到。
    """
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch.block_wait = 5.0
        ch._on_message = lambda msg: None
        first = _send_with_push(ch, box.url)
        started = time.monotonic()
        _, second = _call(ch, "message/send",
                          {"message": _user_msg("再问一句", taskId=first["id"])})
        assert time.monotonic() - started < 1.0
        assert second["result"]["status"]["state"] == "working"


def test_a_lone_surrogate_in_the_answer_still_gets_delivered(tmp_path):
    """`json.dumps(..., ensure_ascii=False).encode()` 会为孤立代理项抛 UnicodeEncodeError，
    而那会从 `except _TransportError` 旁边漏过去：这条回调**永远发不出去、也不重试**。
    """
    with _receiver() as box:
        ch = _channel(tmp_path)
        ch._load_options()
        ch._on_message = lambda msg: ch.send_text(msg, "答案\ud800")
        _send_with_push(ch, box.url)
        call = box.wait_for(1)[0]
        assert call["body"]["status"]["state"] == "completed"
        assert "答案" in call["body"]["artifacts"][0]["parts"][0]["text"]


def test_transport_failures_do_not_print_the_callback_token(tmp_path, monkeypatch):
    """日志与终端都不能复述 token —— 它常常就是路径的一段（`/a2a/push/<token>`）。"""
    monkeypatch.setattr(a2a_mod, "PUSH_BACKOFF", (0.0,))
    # 连一个没人听的端口在 Windows 上可能不是「立刻拒绝」而是**等到超时**，
    # 所以把超时与次数都调小：这条测的是「日志里不出现 token」，不是「连不上有多快」。
    monkeypatch.setattr(a2a_mod, "PUSH_TIMEOUT", 0.2)
    monkeypatch.setattr(a2a_mod, "PUSH_TRIES", 2)
    notes: list[str] = []
    ch = _channel(tmp_path)
    ch._load_options()
    ch._on_message = lambda msg: ch.send_text(msg, "答案")     # 跑到终态才谈得上回调
    ch._notice = lambda text: notes.append(text)
    dead = f"http://127.0.0.1:{_free_port()}/push/s3cret-token"
    _send_with_push(ch, dead)
    ch._push_stop()
    joined = "\n".join(notes)
    assert "s3cret-token" not in joined
    assert "127.0.0.1" in joined            # 但地址（主机部分）要留着，不然没法排查


def test_redact_drops_the_userinfo_too():
    assert a2a_mod._redact("https://user:pw@example.com:9000/hook/x") == "https://example.com:9000/…"
    assert a2a_mod._redact("http://127.0.0.1:8000/a2a/push/tok") == "http://127.0.0.1:8000/…"


def test_reuse_port_is_pinned_off(tmp_path):
    """`SO_REUSEPORT` 会让两个实例同时绑同一个端口 —— 「被占就报错」立刻变谎话。"""
    ch = _channel(tmp_path)
    server = ch.bind_server()
    try:
        assert server.allow_reuse_port is False
    finally:
        server.server_close()


# --- 收图：三套线形状、两道闸、一个落盘目录 ------------------------
# 这几条保护的不变量（动收图之前先读一遍）：
#
# 1. **形状靠字段存在性认，不靠 `kind`/`type`**：0.3 有判别符、老 proto-JSON 是
#    `type:"FilePart"`、1.x 压根没有。认错一代 = 对端发的图静默丢弃。
# 2. **类型只认 magic bytes**：对端声明的 `mimeType` / `mediaType` 不参与判定，
#    也不参与命名 —— 声明只是它的一句话。
# 3. **命名规则只有一处**（`multimodal.save_image_bytes`）：CLI `--image` 与频道
#    入站必须落到同一个文件名，否则同一张图会落两份。
# 4. **base64 不进 task 历史**：`tasks/get` 会原样回吐，一张图 = 此后每次查询几十 MB。
# 5. **URI 一律不下载**：零出站请求。

def _png(size: int = 1) -> bytes:
    """造一张真的 PNG（`size`×`size` 纯黑）。

    不用外部夹具：这几条测的是「字节进来之后发生了什么」，图只要能过 magic bytes
    且长度可控就够了 —— 但也没必要为此放一张假图。
    """
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _file_msg(parts: list, **extra) -> dict:
    message = {"role": "user", "parts": parts}
    message.update(extra)
    return message


def _img_channel(tmp_path: Path, **options: str) -> tuple[A2aChannel, list]:
    """一个能看见「桥收到了什么」的端点（不开 HTTP，直接 handle_rpc）。

    `block_wait` 必须**直接落到属性上**：它只在 `_load_options()` 里从 options 读
    （`a2a.py:852`），而这个 helper 从不跑 `run()` —— 传在 `opts` 里不生效，每条
    `message/send` 都会白等满默认的 45 秒（走两次 send 的用例要等 90 秒）。这与
    `allow_images` 做成 property 的理由是同一个坑：**`_load_options` 那时还没跑**。
    """
    seen: list[InboundMessage] = []
    opts = {"no_register": "true", "port": "0"}
    opts.update(options)
    ch = A2aChannel("a2a", opts, workspace_dir=tmp_path / ".openthree",
                    on_message=seen.append)
    ch.block_wait = float(opts.get("block_wait") or 0)
    return ch, seen


def _assets(tmp_path: Path) -> list[Path]:
    root = tmp_path / ".openthree" / "assets"
    return sorted(root.iterdir()) if root.exists() else []


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_a2a_03_file_with_bytes_becomes_a_real_local_image(tmp_path):
    png = _png()
    ch, seen = _img_channel(tmp_path)
    _, resp = _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "这张图里是什么？"},
        {"kind": "file", "file": {"bytes": _b64(png), "mimeType": "image/png",
                                  "name": "shot.png"}},
    ])})
    assert "result" in resp, resp
    assert len(seen) == 1
    msg = seen[0]
    assert "这张图里是什么" in msg.text
    assert len(msg.images) == 1
    path = Path(msg.images[0])
    assert path.parent == tmp_path / ".openthree" / "assets"
    assert path.read_bytes() == png                 # 一个字节都不差
    assert path.suffix == ".png"                    # 后缀由内容定，不由声明
    assert "shot.png" not in path.name              # 对端给的名字不参与命名


def test_a2a_1x_raw_part_is_the_same_image_to_us(tmp_path):
    """1.x 的 `oneof content` 没有判别符，图片是 `raw` + `mediaType`。"""
    png = _png()
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"raw": _b64(png), "mediaType": "image/png", "filename": "x.png"},
    ])})
    assert len(seen) == 1
    assert len(seen[0].images) == 1
    assert Path(seen[0].images[0]).read_bytes() == png


def test_old_proto_json_file_part_is_accepted_too(tmp_path):
    """更老的一代：`{"type":"FilePart","file":{"fileWithBytes":…}}`。"""
    png = _png()
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"type": "FilePart",
         "file": {"fileWithBytes": _b64(png), "mimeType": "image/png"}},
    ])})
    assert len(seen[0].images) == 1


def test_message_parts_keeps_text_and_files_apart():
    """纯解析层：文本归文本、文件归文件，两边都不吞掉对方。"""
    text, files = message_parts({"parts": [
        {"kind": "text", "text": "看这个"},
        {"kind": "file", "file": {"bytes": "AAAA", "mimeType": "image/png"}},
        {"kind": "data", "data": {"k": 1}},
    ]})
    assert text == "看这个"
    assert files == [{"bytes": "AAAA", "uri": "", "mime": "image/png", "name": ""}]


def test_a_message_with_only_an_image_is_not_rejected(tmp_path):
    """纯图消息曾经直接 `-32602` —— 图能收之后它必须照常建任务。"""
    png = _png()
    ch, seen = _img_channel(tmp_path)
    _, resp = _call(ch, "message/send", {"message": _file_msg([
        {"kind": "file", "file": {"bytes": _b64(png), "mimeType": "image/png"}},
    ])})
    assert "error" not in resp, resp
    assert resp["result"]["status"]["state"] == "working"    # ack：block_wait=0
    assert len(seen[0].images) == 1
    # 历史里必须有一句能读的话，而不是一条空 text part
    assert "图片 1: image/png" in json.dumps(resp["result"]["history"], ensure_ascii=False)


def test_a_file_that_is_not_really_an_image_is_refused_not_crashed(tmp_path):
    """声明 image/png、内容是 HTML：拒掉这一张，但整条消息照跑（不是 500）。"""
    ch, seen = _img_channel(tmp_path)
    _, resp = _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "看图"},
        {"kind": "file", "file": {"bytes": _b64(b"<html>not an image</html>"),
                                  "mimeType": "image/png"}},
    ])})
    assert "result" in resp, resp
    assert seen[0].images == []
    assert "未接收" in seen[0].text
    assert "看图" in seen[0].text          # 文本那部分照样交给模型
    assert _assets(tmp_path) == []         # 认不出的内容一个字节都不落盘


def test_an_image_over_the_per_image_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTHREE_MAX_IMAGE_BYTES", "40")   # 比任何真 PNG 都小
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "看图"},
        {"kind": "file", "file": {"bytes": _b64(_png(2))}},
    ])})
    assert seen[0].images == []
    assert "上限" in seen[0].text


def test_uri_images_are_never_downloaded(tmp_path, monkeypatch):
    """零出站请求：一个无鉴权端点 + 服务端主动取任意 URL 是本文件能引入的最大的洞。"""
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "看这张图"},
        {"kind": "file", "file": {"uri": "http://169.254.169.254/latest/meta-data/",
                                  "mimeType": "image/png"}},
    ])})
    assert calls == []                     # 一次都没发出去
    assert seen[0].images == []
    assert "不下载" in seen[0].text


def test_a_message_with_only_an_unusable_image_says_why(tmp_path):
    """没有文本、唯一的图又收不下 —— 说清原因，别回一句空洞的 -32602。"""
    ch, _ = _img_channel(tmp_path)
    _, resp = _call(ch, "message/send", {"message": _file_msg([
        {"kind": "file", "file": {"uri": "https://example.com/x.png"}},
    ])})
    assert resp["error"]["code"] == -32602
    assert "不下载" in resp["error"]["message"]


def test_card_declares_only_the_modes_the_endpoint_really_accepts(tmp_path):
    """卡片不撒谎：声明了 image/* 就必须真能收。"""
    ch, _ = _img_channel(tmp_path)
    modes = ch.card()["defaultInputModes"]
    assert modes[0] == "text/plain"
    assert set(modes[1:]) == {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
    # 默认开启出站文件 ⇒ 默认就声明能回文件（卡片不撒谎）；文本永远是第一个 mode。
    assert ch.card()["defaultOutputModes"] == ["text/plain", "application/octet-stream"]


def test_allow_images_false_keeps_the_card_honest(tmp_path):
    png = _png()
    ch, seen = _img_channel(tmp_path, allow_images="false")
    assert ch.card()["defaultInputModes"] == ["text/plain"]
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "看图"},
        {"kind": "file", "file": {"bytes": _b64(png)}},
    ])})
    assert seen[0].images == []
    assert "allow_images" in seen[0].text
    assert _assets(tmp_path) == []


def test_bridge_hands_images_to_the_runtime():
    """桥是唯一那道缝：`images` 必须原样到 `Runtime.run`，别的地方都看不见图。"""
    class _Rec:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def run(self, text, **kwargs):
            self.calls.append((text, kwargs))
            return {"answer": "ok"}

    rt = _Rec()
    bridge = AgentBridge(rt, echo=False)
    assert bridge.ask("看这张", images=["D:/x/a.png"]) == "ok"
    assert rt.calls[0][1]["images"] == ["D:/x/a.png"]
    bridge.ask("没带图")
    assert rt.calls[1][1]["images"] is None      # 空 = None，别把 [] 当成「有图」


def test_compose_input_tells_the_model_that_images_came_with_it():
    pure = InboundMessage(text="", channel="a2a", channel_type="a2a", images=["a.png"])
    assert "1 张图片" in compose_input(pure)
    both = InboundMessage(text="看这两张", channel="a2a", channel_type="a2a",
                          images=["a.png", "b.png"])
    composed = compose_input(both)
    assert "看这两张" in composed and "2 张图片" in composed


def test_task_history_holds_a_placeholder_not_the_base64(tmp_path):
    """`tasks/get` 会原样回吐 history：base64 进去 = 此后每次查询几十 MB。"""
    png = _png(4)
    ch, _ = _img_channel(tmp_path)
    _, resp = _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "看图"},
        {"kind": "file", "file": {"bytes": _b64(png)}},
    ])})
    _, got = _call(ch, "tasks/get", {"id": resp["result"]["id"]})
    blob = json.dumps(got, ensure_ascii=False)
    assert _b64(png)[:16] not in blob
    assert "图片 1: image/png" in blob
    assert len(blob) < 4000


def test_the_same_image_twice_lands_on_disk_once(tmp_path):
    """sha1 去重 + 一条命名规则：同一张图发两次，`assets/` 里只有一个文件。"""
    png = _png()
    ch, seen = _img_channel(tmp_path)
    part = {"kind": "file", "file": {"bytes": _b64(png)}}
    for _ in range(2):
        _call(ch, "message/send", {"message": _file_msg([dict(part)])})
    assert len(seen) == 2
    assert seen[0].images == seen[1].images
    assert len(_assets(tmp_path)) == 1


def test_cli_path_and_channel_path_share_one_naming_rule(tmp_path):
    """命名规则只许有一处：CLI 落过的图，频道再收一次不该多出第二个文件。"""
    from openthree.multimodal import save_asset

    png = _png()
    src = tmp_path / "from_cli.png"
    src.write_bytes(png)
    dest = save_asset(tmp_path / ".openthree" / "assets", src)
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "file", "file": {"bytes": _b64(png)}},
    ])})
    assert Path(seen[0].images[0]) == dest
    assert len(_assets(tmp_path)) == 1


def test_a_message_with_too_many_images_keeps_only_the_cap(tmp_path):
    """8 MiB 请求体塞得下几万张 60 字节小图 —— 上限必须在**落盘之前**。

    判据：被拒的那几张连 `assets/` 都不该留痕（否则「只收 N 张」只是一句文案）。
    """
    from openthree.channels.a2a import MAX_IMAGES_PER_MESSAGE

    ch, seen = _img_channel(tmp_path)
    parts = [{"kind": "text", "text": "一堆图"}]
    for n in range(1, MAX_IMAGES_PER_MESSAGE + 4):
        parts.append({"kind": "file", "file": {"bytes": _b64(_png(n))}})   # 内容互不相同
    _, resp = _call(ch, "message/send", {"message": _file_msg(parts)})
    assert "result" in resp, resp
    assert len(seen[0].images) == MAX_IMAGES_PER_MESSAGE
    assert len(_assets(tmp_path)) == MAX_IMAGES_PER_MESSAGE
    assert "最多收" in seen[0].text


def test_a_malformed_uri_does_not_take_the_message_down(tmp_path):
    """`http://[::1` 会让 `urlsplit` 抛 ValueError —— 一条畸形地址不该变成 -32603。"""
    ch, seen = _img_channel(tmp_path)
    _, resp = _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "看图"},
        {"kind": "file", "file": {"uri": "http://[::1"}},
    ])})
    assert "result" in resp, resp
    assert seen[0].images == []
    assert "无法解析" in seen[0].text


def test_uri_label_drops_credentials_and_long_hosts():
    """这条 note 会进 history 回给对端：凭据与超长 host 都不该跟着走。"""
    assert a2a_mod._uri_label("https://user:pw@example.com:9000/x.png") == \
        "https://example.com:9000/…"
    assert len(a2a_mod._uri_label("http://" + "a" * 5000 + "/x.png")) < 120
    assert a2a_mod._uri_label("data:image/png;base64,AAAA") == "data:…"
    assert a2a_mod._uri_label("") == "空地址"
    assert a2a_mod._uri_label("relative/path.png") == "相对地址"
    # scheme-relative：没有 scheme，别拼成 `://host/…` 那句坏话
    assert a2a_mod._uri_label("//host/x.png") == "//host/…"
    # scheme 的长度同样由对端决定（urlsplit 会一直取到第一个 `:`）—— 两个分支都要截断
    assert len(a2a_mod._uri_label("a" * 5000 + ":x")) < 40
    assert len(a2a_mod._uri_label("a" * 5000 + "://host/x.png")) < 40
    # 控制字符：这条 note 会进终端回显（桥打印 msg.text），`\x1b]0;…` 就是一次终端注入
    assert "\x1b" not in a2a_mod._uri_label("http://evil\x1b]0;pwned\x07/x.png")


def test_save_asset_keeps_a_local_file_with_a_weird_suffix(tmp_path):
    """`save_asset` 走的是**本地可信**文件：怪后缀不该被那条严格规则拒掉。

    严格后缀校验是为「对端给的字节」那条路设的。把 CLI 也卡住就是回归：
    `stage_images()` 会退回原引用，而相对路径在回放时可能已经解析不到了。
    """
    from openthree.multimodal import save_asset

    assets = tmp_path / ".openthree" / "assets"
    weird = tmp_path / "shot.png~"
    weird.write_bytes(b"not any known magic type at all")
    dest = save_asset(assets, weird)
    assert dest.parent == assets
    assert dest.suffix == ".bin"                    # 兜底，不是拒收
    assert dest.read_bytes() == weird.read_bytes()


def test_the_assets_budget_stops_a_disk_fill(tmp_path, monkeypatch):
    """`assets/` 只增不减 + 无鉴权 ⇒ 必须有一条总量上限，否则收图 = 磁盘填充入口。

    判据：预算在**落盘之前**判；拒掉的那张一个字节都不写。
    """
    png = _png(4)
    monkeypatch.setattr(a2a_mod, "ASSETS_BUDGET_BYTES", len(png) + 1)    # 只够放一张
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "第一张"},
        {"kind": "file", "file": {"bytes": _b64(png)}},
    ])})
    assert len(seen[-1].images) == 1                # 挤进去了
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "第二张"},
        {"kind": "file", "file": {"bytes": _b64(_png(5))}},    # 内容不同：绕开 sha1
    ])})
    assert seen[-1].images == []
    assert "上限" in seen[-1].text
    assert len(_assets(tmp_path)) == 1              # 拒掉的一个字节都没写


def test_a_deduped_image_is_not_charged_against_the_budget(tmp_path, monkeypatch):
    """预算只挡**新占用**：目录里已经有的那张再发一次不会让目录长大，不该被拒。

    判据：先用 `asset_dest()` 算名字判「是否已存在」，再决定要不要查预算 ——
    反过来（先按 len(data) 计费）会让一张重发的图在接近上限时莫名其妙地失败。
    """
    png = _png(4)
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "第一次"},
        {"kind": "file", "file": {"bytes": _b64(png)}},
    ])})
    assert len(seen[-1].images) == 1
    used = sum(p.stat().st_size for p in _assets(tmp_path))
    monkeypatch.setattr(a2a_mod, "ASSETS_BUDGET_BYTES", used)     # 正好用满
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "同一张再来一次"},
        {"kind": "file", "file": {"bytes": _b64(png)}},
    ])})
    assert len(seen[-1].images) == 1                              # 去重命中，不该被拒
    assert len(_assets(tmp_path)) == 1


def test_leftover_temp_files_do_not_eat_the_budget(tmp_path, monkeypatch):
    """崩溃留下的 `<digest>.png.tmp-xxxx` 不占上传预算：它是碎片，不是一张图。

    否则一次崩溃就永久吃掉一截预算，而运维在目录里看不出原因（那种文件没人清）。
    """
    assets = tmp_path / ".openthree" / "assets"
    assets.mkdir(parents=True)
    (assets / "deadbeef.png.tmp-1234abcd").write_bytes(b"x" * 4096)
    monkeypatch.setattr(a2a_mod, "ASSETS_BUDGET_BYTES", 120)      # 比碎片本身还小
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "text", "text": "看图"},
        {"kind": "file", "file": {"bytes": _b64(_png(2))}},
    ])})
    assert len(seen[-1].images) == 1        # 4096 字节的碎片没有被算进预算


def test_declared_mime_never_decides_the_type(tmp_path):
    """对端声明 image/jpeg、内容是 png：后缀按内容走，声明只进日志。"""
    png = _png()
    ch, seen = _img_channel(tmp_path)
    _call(ch, "message/send", {"message": _file_msg([
        {"kind": "file", "file": {"bytes": _b64(png), "mimeType": "image/jpeg"}},
    ])})
    assert Path(seen[0].images[0]).suffix == ".png"


def test_concurrent_writes_of_the_same_image_never_leave_a_half_file(tmp_path):
    """收图是并发入口（每条连接一个线程）：写盘必须原子，读到的一半也是错的。"""
    from openthree.multimodal import save_image_bytes, sniff_mime

    png = _png(8)
    assets = tmp_path / ".openthree" / "assets"
    results: list[Path] = []

    def worker() -> None:
        results.append(save_image_bytes(assets, png))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(results) == 8
    assert len({str(p) for p in results}) == 1        # 同一个名字
    assert results[0].read_bytes() == png              # 内容完整，不是半张
    assert sniff_mime(results[0].read_bytes()) == "image/png"
    assert [p.name for p in assets.iterdir() if ".tmp-" in p.name] == []


# --- 出站文件 -------------------------------------------------------------
# 被测的东西只有一句：**工具负责记下，频道负责校验并发送**。所以这里既测频道层
# （`send_text` 之后 artifact 里有什么），也测工具层（槽在不在、回执说什么）。


def _round_with_files(ch, seen, body: str, attachments: list[dict]) -> dict:
    """跑一轮：发消息 → 把附件挂到那条 msg 上 → 回话 → 拉回 task 视图。"""
    _, sent = _call(ch, "message/send", {"message": _user_msg("做个报告")})
    task_id = sent["result"]["id"]
    seen[-1].attachments.extend(attachments)
    ch.send_text(seen[-1], body)
    _, got = _call(ch, "tasks/get", {"id": task_id})
    return got["result"]


def test_an_attachment_becomes_a_file_part_after_the_text(tmp_path):
    """文本永远第一位：老面板只读 `parts[].text`，它不该因为多了一个 file 而空白。"""
    src = tmp_path / "report.bin"
    payload = bytes(range(256)) * 4
    src.write_bytes(payload)
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "报告生成好了，见附件。", [{"path": str(src)}])
    parts = task["artifacts"][0]["parts"]
    assert [p["kind"] for p in parts] == ["text", "file"]
    assert parts[0]["text"] == "报告生成好了，见附件。"
    assert parts[1]["file"]["name"] == "report.bin"
    assert parts[1]["file"]["mimeType"] == "application/octet-stream"
    assert base64.b64decode(parts[1]["file"]["bytes"]) == payload
    assert task["status"]["state"] == "completed"


def test_the_status_message_carries_text_only(tmp_path):
    """同一份字节不在一个响应里出现两次。"""
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 32)
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": str(src)}])
    assert [p["kind"] for p in task["status"]["message"]["parts"]] == ["text"]
    assert "bytes" not in json.dumps(task["status"])


def test_history_keeps_a_placeholder_not_the_bytes_or_the_local_path(tmp_path):
    """与入站「历史里不存 base64」对称：否则此后每次 `tasks/get` 都背上这份字节。"""
    src = tmp_path / "报告.bin"
    src.write_bytes(b"payload")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": str(src)}])
    agent_turn = task["history"][-1]
    assert [p["kind"] for p in agent_turn["parts"]] == ["text", "text"]
    dumped = json.dumps(task["history"], ensure_ascii=False)
    assert "payload" not in dumped
    assert str(tmp_path) not in dumped          # 本机路径不进历史
    assert "附件" in agent_turn["parts"][1]["text"]


def test_a_file_over_the_per_file_limit_is_refused_but_the_answer_still_lands(
        tmp_path, monkeypatch):
    """一个文件超限只拒这一个 —— 文本那部分照样值得送回去。"""
    monkeypatch.setattr(a2a_mod, "MAX_OUTBOUND_FILE_BYTES", 100)
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 200)
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": str(src)}])
    assert task["status"]["state"] == "completed"
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "超过单文件上限" in _text_of(task)


def test_too_many_files_are_refused_one_by_one(tmp_path, monkeypatch):
    monkeypatch.setattr(a2a_mod, "MAX_OUTBOUND_FILES", 2)
    paths = []
    for i in range(3):
        p = tmp_path / f"f{i}.bin"
        p.write_bytes(b"y" * 10)
        paths.append(str(p))
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": p} for p in paths])
    parts = task["artifacts"][0]["parts"]
    assert [p["kind"] for p in parts] == ["text", "file", "file"]
    assert "最多附" in _text_of(task)


def test_the_total_budget_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(a2a_mod, "MAX_OUTBOUND_TOTAL_BYTES", 100)
    paths = []
    for i in range(2):
        p = tmp_path / f"t{i}.bin"
        p.write_bytes(b"z" * 80)
        paths.append(str(p))
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": p} for p in paths])
    # 第一个 80 ≤ 100 放行；第二个会到 160 → 拒
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text", "file"]
    assert "合计超过" in _text_of(task)


def test_a_path_outside_the_allowed_roots_is_refused(tmp_path):
    outside = tmp_path.parent / "outside-secret.bin"
    outside.write_bytes(b"secret")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": str(outside)}])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "不在允许发送的目录内" in _text_of(task)


def test_missing_directory_and_empty_files_are_refused(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    directory = tmp_path / "somedir"
    directory.mkdir()
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [
        {"path": str(tmp_path / "nope.bin")},
        {"path": str(directory)},
        {"path": str(empty)},
    ])
    text = _text_of(task)
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "读不到这个路径" in text
    assert "不是普通文件" in text
    assert "空文件" in text


def test_a_symlink_escaping_the_root_is_refused(tmp_path):
    """`resolve()` 会解开符号链接 —— 判的是它真正落到的位置。"""
    import os

    outside = tmp_path.parent / "outside-link-target.bin"
    outside.write_bytes(b"secret")
    link = tmp_path / "link.bin"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("这个平台 / 账户建不了符号链接")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": str(link)}])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "不在允许发送的目录内" in _text_of(task)


def test_the_workspace_config_and_keys_are_never_sent(tmp_path):
    ws = tmp_path / ".openthree"
    ws.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "identity.json", "limits.json"):
        (ws / name).write_text("secret: sk-xxxx\n", encoding="utf-8")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [
        {"path": str(ws / "config.yaml")},
        {"path": str(ws / "identity.json")},
        {"path": str(ws / "limits.json")},
    ])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "密钥黑名单" in _text_of(task)


def test_outbound_roots_empty_turns_the_feature_off(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    ch, seen = _img_channel(tmp_path, outbound_roots="[]")
    task = _round_with_files(ch, seen, "好了", [{"path": str(src)}])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "关掉了出站文件" in _text_of(task)
    assert ch.card()["defaultOutputModes"] == ["text/plain"]


def test_a_bridge_notice_never_carries_files(tmp_path):
    """桥的通知不是结论：一条失败的答复不该顺手带出一个文件。"""
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    ch, seen = _img_channel(tmp_path)
    _, sent = _call(ch, "message/send", {"message": _user_msg("hi")})
    seen[0].attachments.append({"path": str(src)})
    seen[0].raw["notice"] = True
    ch.send_text(seen[0], "[openthree] 正在退出")
    _, got = _call(ch, "tasks/get", {"id": sent["result"]["id"]})
    task = got["result"]
    assert task["status"]["state"] == "failed"
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]


def test_a_follow_up_resets_the_previous_attachments(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    ch, seen = _img_channel(tmp_path)
    first = _round_with_files(ch, seen, "第一版", [{"path": str(src)}])
    assert [p["kind"] for p in first["artifacts"][0]["parts"]] == ["text", "file"]
    task_id = first["id"]
    _call(ch, "message/send", {"message": _user_msg("再来一版", taskId=task_id)})
    ch.send_text(seen[-1], "第二版")          # 这一轮没挂附件
    _, got = _call(ch, "tasks/get", {"id": task_id})
    assert [p["kind"] for p in got["result"]["artifacts"][0]["parts"]] == ["text"]
    assert _text_of(got["result"]) == "第二版"


def test_task_file_budget_downgrades_the_oldest_files_first(tmp_path, monkeypatch):
    """条数上限对 20 MiB 的文件毫无约束 —— 字节预算超了就把最旧的降级成文本。"""
    monkeypatch.setattr(a2a_mod, "MAX_TASK_FILE_BYTES", 400)
    ch, seen = _img_channel(tmp_path)
    ids = []
    for i in range(3):
        src = tmp_path / f"f{i}.bin"
        src.write_bytes(b"z" * 300)           # base64 后 400 字节
        task = _round_with_files(ch, seen, f"第{i}版", [{"path": str(src)}])
        ids.append(task["id"])
    first = _call(ch, "tasks/get", {"id": ids[0]})[1]["result"]
    last = _call(ch, "tasks/get", {"id": ids[-1]})[1]["result"]
    assert "文件已过期" in json.dumps(first, ensure_ascii=False)
    assert any(p["kind"] == "file" for p in last["artifacts"][0]["parts"])


def test_a_channel_that_cannot_carry_files_publishes_no_turn():
    """微信 / 钉钉回合里槽不存在 → 工具必须说实话，不能假装附上了。"""
    from openthree.attach import TURN_SLOT
    from openthree.kernel import KERNEL

    seen: dict = {}

    class _Rec:
        def run(self, text, **kwargs):
            from openthree.attach import attach_file

            seen["slot"] = any(k[0] == TURN_SLOT for k in list(KERNEL.state))
            seen["reply"] = attach_file(str(Path(__file__)))
            return {"answer": "ok"}

    class _Plain:
        supports_files = False

        def send_text(self, msg, text):
            seen["answer"] = text

    bridge = AgentBridge(_Rec(), echo=False)
    bridge.start()
    try:
        bridge.submit(InboundMessage(text="hi"), _Plain().send_text)
        _wait_until(lambda: "answer" in seen, what="桥跑完这条")
    finally:
        bridge.stop(timeout=5)
    assert seen["slot"] is False
    assert "这次答复发不出文件" in seen["reply"]


def test_a_channel_that_can_carry_files_hands_the_path_to_the_reply(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"abcdef")
    seen: dict = {}

    class _Rec:
        def run(self, text, **kwargs):
            from openthree.attach import attach_file

            seen["reply"] = attach_file(str(src))
            return {"answer": "ok"}

    class _Carrier:
        supports_files = True

        def send_text(self, msg, text):
            seen["attachments"] = list(msg.attachments)

    bridge = AgentBridge(_Rec(), echo=False)
    bridge.start()
    try:
        bridge.submit(InboundMessage(text="hi"), _Carrier().send_text)
        _wait_until(lambda: "attachments" in seen, what="桥回话")
    finally:
        bridge.stop(timeout=5)
    assert seen["attachments"] == [{"path": str(src), "name": ""}]
    assert "已记下" in seen["reply"]


def test_push_timeout_covers_a_task_carrying_a_27mb_file():
    """27 MB 的 Task 在公网 20 Mbps 上行要 11 秒 —— 5 秒会让回调必然超时。"""
    assert a2a_mod.PUSH_TIMEOUT >= 60.0


def test_a_push_snapshot_carries_the_file_parts(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 8)
    ch, seen = _img_channel(tmp_path)
    _, sent = _call(ch, "message/send", {"message": _user_msg("hi")})
    task = ch._tasks.get(sent["result"]["id"])
    seen[0].attachments.append({"path": str(src)})
    ch.send_text(seen[0], "好了")
    with ch._tasks.lock:
        view = a2a_mod._public(task)          # 回调送的就是这个视图
    assert any(p["kind"] == "file" for p in view["artifacts"][0]["parts"])


def test_attach_file_is_registered_only_inside_an_a2a_process():
    from openthree.builtins import installed_builtins

    assert "attach_file" not in installed_builtins()
    assert "attach_file" in installed_builtins(a2a=True)
    assert "finish" in installed_builtins(a2a=True)      # 常驻集不受影响


def test_the_a2a_gate_has_four_distinct_states(monkeypatch):
    """每一态都对应一条真实的错法 —— 漏一个就会给某一类启动方式装错工具集。"""
    from openthree import cli as cli_mod

    class _Args:
        def __init__(self, **kw):
            self.a2a = False
            self.no_channels = False
            self.upgrade = False
            self.daemon = False
            self.interactive = False
            self.no_interactive = False
            self.task = []
            self.auto = 0
            self.resume = 0
            for k, v in kw.items():
                setattr(self, k, v)

    monkeypatch.setattr(cli_mod, "_collect_images", lambda a: [])
    monkeypatch.setattr(cli_mod, "_stdin_is_tty", lambda: True)

    assert cli_mod._a2a_flag(_Args(a2a=True)) is True            # -A2A：必然起
    assert cli_mod._a2a_flag(_Args(interactive=True)) is None     # -i：读 channels.yaml
    assert cli_mod._a2a_flag(_Args(daemon=True)) is None          # -d：同上
    assert cli_mod._a2a_flag(_Args(task=["干点活"])) is False      # 单次任务
    assert cli_mod._a2a_flag(_Args(interactive=True, no_channels=True)) is False
    assert cli_mod._a2a_flag(_Args(upgrade=True, interactive=True)) is False


def test_declares_a2a_reads_channels_yaml(tmp_path):
    from openthree.channels.config import ChannelConfig, ChannelsConfig
    from openthree.channels.manager import declares_a2a

    class _WS:
        dir = tmp_path / ".openthree"

    ws = _WS()
    ws.dir.mkdir(parents=True, exist_ok=True)
    assert declares_a2a(ws) is False                    # 没有配置 = 不声明
    cfg = ChannelsConfig(path=ws.dir / "channels.yaml", channels=[])
    cfg.add(ChannelConfig(name="a2a", type="a2a", enabled=False))
    cfg.save()
    assert declares_a2a(ws) is False                    # disabled 不算
    cfg.get("a2a").enabled = True
    cfg.save()
    assert declares_a2a(ws) is True


def test_channel_credentials_are_never_sent(tmp_path):
    """`channels.yaml` 里是 client_secret，`channels/` 下是各渠道的 token。

    它们都在默认根（工作区）里 —— 少了这条闸，任何连得上端口的人都能把钥匙要走。
    """
    ws = tmp_path / ".openthree"
    (ws / "channels").mkdir(parents=True, exist_ok=True)
    (ws / "channels.yaml").write_text("channels: []\n", encoding="utf-8")
    (ws / "channels" / "wechat.json").write_text('{"context_token": "x"}', encoding="utf-8")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [
        {"path": str(ws / "channels.yaml")},
        {"path": str(ws / "channels" / "wechat.json")},
    ])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "密钥黑名单" in _text_of(task)


def test_dotenv_variants_are_never_sent(tmp_path):
    (tmp_path / ".env.production").write_text("API_KEY=x\n", encoding="utf-8")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了",
                             [{"path": str(tmp_path / ".env.production")}])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "密钥黑名单" in _text_of(task)


def test_supports_files_follows_the_outbound_switch(tmp_path):
    """关掉出站文件时桥不该发布回合 —— 否则工具先说「已记下」、频道层再拒。"""
    on, _ = _img_channel(tmp_path)
    off, _ = _img_channel(tmp_path, outbound_roots="[]")
    assert on.supports_files is True
    assert off.supports_files is False


def test_the_tool_says_no_above_the_count_limit(tmp_path):
    """回执必须为真：第 5 个文件一定发不出去，就不能说「会随本次答复发出」。"""
    from openthree.attach import MAX_OUTBOUND_FILES, TURN_SLOT, attach_file
    from openthree.kernel import KERNEL

    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    msg = InboundMessage(text="hi")
    slot = (TURN_SLOT, threading.get_ident())
    KERNEL.state[slot] = msg
    try:
        replies = [attach_file(str(src)) for _ in range(MAX_OUTBOUND_FILES + 1)]
    finally:
        KERNEL.state.pop(slot, None)
    assert len(msg.attachments) == MAX_OUTBOUND_FILES
    assert "附满" in replies[-1]


def test_a_reply_with_no_answer_and_every_file_refused_is_failed(tmp_path):
    """被拒的说明不是结论：它不该把一条「什么也没有」的答复变成 completed。"""
    outside = tmp_path.parent / "o3-outside-2.bin"
    outside.write_bytes(b"secret")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "", [{"path": str(outside)}])
    assert task["status"]["state"] == "failed"
    assert "不在允许发送的目录内" in _text_of(task)


def test_the_upgrade_staging_copy_of_config_is_never_sent(tmp_path):
    """升级模式把旧文件 `copy2` 到 `.openthree/upgrade/<ts>/files/…`。

    那是同一份 `config.yaml` 的副本、同样带着 `llm.api_key` —— 枚举式黑名单会漏掉
    它。反向白名单（`.openthree/` 下除 `assets/` 外全拒）才守得住。
    """
    stage = tmp_path / ".openthree" / "upgrade" / "20261112" / "files"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "config.yaml").write_text("llm:\n  api_key: sk-x\n", encoding="utf-8")
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": str(stage / "config.yaml")}])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text"]
    assert "工作区状态不发" in _text_of(task)


def test_assets_are_still_sendable(tmp_path):
    """`.openthree/assets/` 是反向白名单里唯一放行的子树 —— 收来的图正是要发的。"""
    assets = tmp_path / ".openthree" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    src = assets / "in.png"
    src.write_bytes(_png())
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "图在这", [{"path": str(src)}])
    assert [p["kind"] for p in task["artifacts"][0]["parts"]] == ["text", "file"]


def test_the_just_finished_task_is_never_expired_by_its_own_budget_check(
        tmp_path, monkeypatch):
    """调用方不该在同一次响应里拿到 `completed` + 一个「已过期」占位。"""
    monkeypatch.setattr(a2a_mod, "MAX_TASK_FILE_BYTES", 100)
    src = tmp_path / "a.bin"
    src.write_bytes(b"z" * 90)                  # base64 后 120 > 预算 100
    ch, seen = _img_channel(tmp_path)
    task = _round_with_files(ch, seen, "好了", [{"path": str(src)}])
    assert any(p["kind"] == "file" for p in task["artifacts"][0]["parts"])
    assert "文件已过期" not in json.dumps(task, ensure_ascii=False)


# --- 过程显形：working 里到底在干什么 -------------------------------------
#
# 被保护的不变量：`tasks/get` 的 `working` 必须能回答「跑到第几步 / 在干什么 /
# 已经多久」，而**终态之后不留任何过程残影**。只给一个光秃秃的 `working`，
# 轮询十几分钟也读不出「正在调 run_shell」和「进程已经卡死」的区别 —— 那正是
# 这个频道此前最要命的盲区。

def _open_task(ch: A2aChannel, text: str = "q"):
    """发一条「收下不回」的消息，返回 `(task_id, 频道收到的 InboundMessage)`。"""
    seen: list[InboundMessage] = []
    ch._on_message = seen.append
    _, body = _call(ch, "message/send", {"message": _user_msg(text)})
    return body["result"]["id"], seen[0]


def _get(ch: A2aChannel, task_id: str) -> dict:
    return _call(ch, "tasks/get", {"id": task_id})[1]["result"]


def _status_text(task: dict) -> str:
    return task["status"]["message"]["parts"][0]["text"]


def test_durations_are_written_the_way_a_human_reads_them():
    """`status.message` 里那句时长要一眼有量感 —— `130s` 得心算，`2m10s` 不用。"""
    assert a2a_mod._dur(0) == "0s"
    assert a2a_mod._dur(42.7) == "42s"
    assert a2a_mod._dur(80) == "1m20s"
    assert a2a_mod._dur(7500) == "2h05m"
    assert a2a_mod._dur(-5) == "0s"


def test_submitted_already_says_it_is_queued():
    """`submitted` 也得有一句人话：分不出「刚收到」和「压根没被处理」就是黑箱。"""
    store = a2a_mod._TaskStore()
    task = store.new(task_id="t-1", context_id="c-1", text="q")
    assert task["status"]["state"] == "submitted"
    assert "排队" in _status_text(task)
    assert task["status"]["timestamp"]
    # 还没开跑：没有进展可报，也就不该有 metadata
    assert "metadata" not in task


def test_working_reports_the_step_the_tool_and_the_elapsed_time(tmp_path):
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    echo = ch.progress_for(msg)
    assert echo is not None
    echo.step(7, 200)
    echo.tool_start("run_shell", {"command": "pytest"})

    task = _get(ch, task_id)
    assert task["status"]["state"] == "working"
    text = _status_text(task)
    assert "第 7/200 步" in text and "调用 run_shell" in text and "已运行" in text
    info = task["metadata"]["openthree"]
    assert (info["step"], info["maxSteps"], info["activity"]) == (7, 200, "调用 run_shell")
    # `TaskStatus` 只有 state / message / timestamp 三个字段，结构化细节只能住
    # 在 `Task.metadata`：程序化的调用方读它，不必去解析那句中文
    assert isinstance(info["elapsedS"], float) and info["elapsedS"] >= 0


def test_a_late_heartbeat_never_rewinds_the_activity(tmp_path):
    """心跳只证明「还活着」，绝不把活动描述写回上一个阶段。

    `Progress` 只等心跳线程 0.3 秒，所以一枚 `idle` 完全可能落在 `tool_start`
    之后 —— 它若写回「等待模型返回」，界面就从「调用 run_shell」倒退回去，
    看起来像卡在了上一步。
    """
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    echo = ch.progress_for(msg)
    echo.step(3, 200)
    echo.tool_start("run_shell")
    echo.idle("等待模型返回", 45.0)

    task = _get(ch, task_id)
    assert task["metadata"]["openthree"]["activity"] == "调用 run_shell"
    assert "调用 run_shell" in _status_text(task)


def test_a_terminal_task_carries_no_leftover_progress(tmp_path):
    """终态之后不许留「第 9 步 · 调用 run_shell」—— 那是会让人以为没收工的证据。"""
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    echo = ch.progress_for(msg)
    echo.step(9, 200)
    echo.tool_start("run_shell")
    ch.send_text(msg, "答案")

    task = _get(ch, task_id)
    assert task["status"]["state"] == "completed"
    assert "metadata" not in task
    assert "第 9" not in json.dumps(task, ensure_ascii=False)


def test_progress_arriving_after_the_terminal_state_is_ignored(tmp_path):
    """一枚迟到的心跳点不亮一个已经完成的任务。"""
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    echo = ch.progress_for(msg)
    ch.send_text(msg, "答案")
    before = _get(ch, task_id)
    echo.step(11, 200)
    echo.idle("等待模型返回", 3.0)
    after = _get(ch, task_id)
    assert after["status"] == before["status"]
    assert "metadata" not in after


def test_notify_writes_the_queue_position_without_touching_the_state(tmp_path):
    """桥的排队位落进 `status.message`，但不替 task 推进状态机。"""
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    state_before = _get(ch, task_id)["status"]["state"]
    ch.notify(msg, "queued", "前面还有 1 条")
    task = _get(ch, task_id)
    assert task["status"]["state"] == state_before
    text = _status_text(task)
    assert "排队中" in text and "前面还有 1 条" in text
    # 终态只由 `_finish` 写（那才是带 artifact 的一份）；桥的终态通知不改任何东西
    ch.notify(msg, "done", "用时 3s")
    assert _get(ch, task_id)["status"]["state"] == state_before


def test_progress_for_a_forgotten_task_is_none(tmp_path):
    """被挤出表的 task 不再订阅进展 —— 它的答案已经无处可交。"""
    ch = _channel(tmp_path)
    ch._load_options()
    ch._tasks = a2a_mod._TaskStore(limit=1)     # 只留最近一条
    ch._tasks.on_terminal = ch._notify_push
    first_id, first_msg = _open_task(ch, "一")
    _open_task(ch, "二")
    assert ch._tasks.get(first_id) is None
    assert ch.progress_for(first_msg) is None


def test_follow_up_clears_the_previous_round_progress(tmp_path):
    """上一轮的「第 40 步」不能出现在这一轮的第一帧里。"""
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    echo = ch.progress_for(msg)
    echo.step(40, 200)
    echo.tool_start("run_shell")
    assert "第 40/200 步" in _status_text(_get(ch, task_id))

    ch._on_message = lambda m: None
    _call(ch, "message/send", {"message": _user_msg("再问", taskId=task_id)})
    task = _get(ch, task_id)
    assert task["status"]["state"] == "working"
    # 复位后的第一帧是「已收到」，不该带着上一轮那个步数
    assert "step" not in task["metadata"]["openthree"]
    assert "第 40" not in json.dumps(task, ensure_ascii=False)


class _EchoRuntime:
    """假运行期：按真 `Progress` 的顺序发事件，中途停住等测试看一眼。

    这个替身存在的唯一理由：接线（桥 → `Progress(observer=…)` → `_Echo` →
    `status.message`）只有真跑一遍才证明得了，单独测 `_Echo` 只能证明它自己。
    """

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.started = threading.Event()

    def run(self, text, *, session=False, images=None, cancel_token=None, observer=None):
        progress = Progress(stream=io.StringIO(), heartbeat=0.0, observer=observer)
        progress.step(3, 200)
        progress.llm_start("fake-model")
        progress.tool_start("run_shell", {"command": "pytest -q"})
        self.started.set()
        self.gate.wait(5)
        progress.tool_done("run_shell", 1500, 10)
        progress.close()
        return {"answer": "好了", "status": "done"}


def test_the_real_bridge_puts_progress_into_tasks_get(tmp_path):
    """端到端：真桥 + 真 `Progress` 的步级回声，轮询 `tasks/get` 就看得见。"""
    gate = threading.Event()
    rt = _EchoRuntime(gate)
    bridge = AgentBridge(rt, echo=False)
    bridge.start()
    ch = _channel(tmp_path)
    ch._load_options()
    ch._on_message = lambda msg: bridge.submit(msg, ch.send_text)
    try:
        _, body = _call(ch, "message/send", {"message": _user_msg("q")})
        task_id = body["result"]["id"]
        assert rt.started.wait(5), "桥没有把这条消息跑起来"
        task = _get(ch, task_id)
        assert task["status"]["state"] == "working"
        text = _status_text(task)
        assert "第 3/200 步" in text and "调用 run_shell" in text
    finally:
        gate.set()
        bridge.stop()

    # 收工之后这份过程残影必须消失
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = _get(ch, task_id)
        if task["status"]["state"] == "completed":
            break
        time.sleep(0.02)
    assert task["status"]["state"] == "completed"
    assert "metadata" not in task


def test_the_elapsed_clock_starts_at_the_first_real_event(tmp_path):
    """计时不在观察者构造时起表。

    观察者是桥在**拿到运行锁之前**造出来的（它得先有观察者才能传给
    `Runtime.run`）。控制台正跑着一条时，从造它开始计时，第一帧就会写
    「已运行 5m」—— 而这个数正是调用方用来判断「卡了多久」的。
    """
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    echo = ch.progress_for(msg)
    assert echo._started is None                  # 还没起表
    echo.step(1, 200)
    info = _get(ch, task_id)["metadata"]["openthree"]
    assert info["elapsedS"] < 5.0                 # 从第一个真事件算起，而不是从造它算起


def test_a_previous_rounds_progress_never_lands_on_the_reset_task(tmp_path):
    """follow up 复位之后，上一轮迟到的帧必须被丢弃（与 `_finish` 同一判据）。

    桥只有一条工作线程，而 follow up 是 HTTP 线程直接复位的 —— 上一轮的 ReAct
    还在跑，它的观察者也还在发事件。不比对 `_run`，它的「第 40 步」就写进了
    这一轮刚复位的 task，正是复位想避免的那件事。
    """
    ch = _channel(tmp_path)
    ch._load_options()
    task_id, msg = _open_task(ch)
    old = ch.progress_for(msg)
    old.step(40, 200)
    assert "第 40/200 步" in _status_text(_get(ch, task_id))

    ch._on_message = lambda m: None
    _call(ch, "message/send", {"message": _user_msg("再问", taskId=task_id)})
    old.tool_start("run_shell")           # 上一轮还没停，这一帧属于它
    dumped = json.dumps(_get(ch, task_id), ensure_ascii=False)
    assert "第 40" not in dumped
    assert "调用 run_shell" not in dumped


def test_a_run_that_raised_is_reported_as_failed_not_completed(tmp_path):
    """跑挂的那条也带着一句话正常返回 —— 只按「答复非空」判会记成成功。

    `Runtime.run()` 出异常时给的是 `{"answer": "[运行时异常] …", "status": "error"}`，
    桥走的是**正常**回复路径（`notice` 不设）。所以判词得单独带过来（`verdict`）：
    否则对面轮询 `tasks/get`，看到的是一个跑挂了的任务报 `completed` —— 正是
    「把失败报成成功」。
    """
    class _BoomRT:
        def run(self, text, **kwargs):
            return {"answer": "[运行时异常] RuntimeError: 炸了", "status": "error"}

    bridge = AgentBridge(_BoomRT(), echo=False)
    bridge.start()
    ch = _channel(tmp_path)
    ch._load_options()
    ch._on_message = lambda msg: bridge.submit(msg, ch.send_text)
    try:
        _, body = _call(ch, "message/send", {"message": _user_msg("q")})
        task_id = body["result"]["id"]
        deadline = time.monotonic() + 5
        task = _get(ch, task_id)
        while time.monotonic() < deadline:
            task = _get(ch, task_id)
            if task["status"]["state"] in ("failed", "completed"):
                break
            time.sleep(0.02)
        assert task["status"]["state"] == "failed"
        # 失败要说得清为什么：那句异常说明仍然原样进 artifact
        assert "运行时异常" in _text_of(task)
    finally:
        bridge.stop()
