"""A2A「真实停止」的验收：取消之后不再发 LLM 请求、不再调工具。

被保护的不变量（动 `cancel.py` / `agent.py` / `runtime.py` / `a2a.py` 之前先读）：

1. **停止的判据是事件不增长**，不是「状态变 canceled」：数 `llm` / `tool` 事件。
   所以下面的断言都落在事件条数与 `status` 上，而不是「函数没抛异常」。
2. **`Cancelled` 必须在 `except Exception` 之前**（`runtime._run_round`、
   `agent._execute` 两处）：顺序反了，一次奉命取消会变成「运行时异常」或一条
   工具错误字符串（后者更坏 —— ReAct 会继续跑到下一步）。
3. **取消了就不开下一轮**（`Runtime.run` / `run_auto` 的轮循环头）：上一轮撞到
   步数上限时，现有代码会自动续轮 —— 漏了检查点等于「取消了，重启了，继续烧」。
4. **令牌是线程本地的**：`LLM.chat` / `post_json` / `run_shell` 的签名里没有它，
   所以这些用例能同时证明「工具 schema 没被污染」。
5. **取消是请求，不是既成事实**：`tasks/cancel` 等到了才说「停在第 N 步」，
   没等到就说「已请求」—— 两句话不能混用。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from openthree import cancel
from openthree.agent import Agent
from openthree.channels.base import InboundMessage
from openthree.channels.bridge import AgentBridge
from openthree.kernel import KERNEL
from openthree.recorder import Recorder
from openthree.runtime import Runtime

from test_a2a_channel import _call, _channel, _text_of, _user_msg


def _wire(ws, registry, rec, ag) -> None:
    KERNEL.workspace = ws
    KERNEL.registry = registry
    KERNEL.recorder = rec
    KERNEL.agent = ag


# ---------------------------------------------------------------- 桩
class _EveryStepToolLLM:
    """每步都调一次 get_time，从不 finish —— 步数只受 max_iterations 约束。"""

    def __init__(self, cfg=None):
        from openthree.llm import LLMConfig
        self.config = cfg or LLMConfig(api_key="k", base_url="u", model="stub-model")
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls += 1
        msg = {"role": "assistant", "content": f"第 {self.calls} 步",
               "tool_calls": [{"id": f"c{self.calls}", "type": "function",
                               "function": {"name": "get_time", "arguments": "{}"}}]}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


class _BatchLLM:
    """一步里给 3 个 tool_call（用来验证「同批剩余调用一次都不调」）。"""

    def __init__(self, cfg=None):
        from openthree.llm import LLMConfig
        self.config = cfg or LLMConfig(api_key="k", base_url="u", model="stub-model")
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls += 1
        calls = [{"id": f"b{self.calls}-{i}", "type": "function",
                  "function": {"name": "get_time", "arguments": "{}"}} for i in range(3)]
        msg = {"role": "assistant", "content": "一次调三个", "tool_calls": calls}
        return {"message": msg, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "elapsed_ms": 0.0, "raw_request": {"messages": []},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


class _CancelOnToolDone:
    """在第 N 次 `tool_done` 时置令牌 —— 模拟「调用方正好在这一刻取消」。

    选 `tool_done` 而不是别的钩子：它在工具**返回之后、下一件事之前**被调用，
    于是「已经发生的事」与「不该再发生的事」边界最清楚。
    """

    def __init__(self, tok, after: int = 1):
        self.tok = tok
        self.after = after
        self.n = 0

    def step(self, *a, **k):
        pass

    def llm_start(self, *a, **k):
        pass

    def llm_done(self, *a, **k):
        pass

    def tool_start(self, *a, **k):
        pass

    def tool_done(self, *a, **k):
        self.n += 1
        if self.n >= self.after:
            self.tok.request("测试：调用方取消")

    def thought(self, *a, **k):
        pass

    def note(self, *a, **k):
        pass

    def close(self, *a, **k):
        pass


def _count(events, kind: str) -> int:
    return sum(1 for e in events if e.kind == kind)


# ---------------------------------------------------------------- 1. 步首
def test_cancel_stops_before_next_step(ws, registry):
    """第 2 次工具调用之后取消 ⇒ 2 条 llm、2 条 tool、绝不发第 3 次请求。"""
    tok = cancel.CancelToken(task_id="t-1")
    rec = Recorder(ws, user_input="t", model="stub-model")
    ag = Agent(ws, registry, _EveryStepToolLLM(), rec,
               progress=_CancelOnToolDone(tok, after=2), cancel_token=tok)
    _wire(ws, registry, rec, ag)

    with pytest.raises(cancel.Cancelled):
        ag.run()

    assert ag.cancelled is True
    doc = rec.finalize(answer=ag.cancel_summary(), status="canceled")
    assert _count(rec.events, "llm") == 2
    assert _count(rec.events, "tool") == 2
    # 结论里要能看出「烧到哪儿了」—— 只说「已取消」等于没说
    assert "停在第" in doc["answer"] and "2 次 LLM 请求" in doc["answer"]


# ---------------------------------------------------------------- 2. 批内
def test_cancel_skips_remaining_tool_calls_in_a_batch(ws, registry):
    """一步 3 个 call，跑完第 1 个就取消 ⇒ 只有 1 条 tool 事件。"""
    tok = cancel.CancelToken(task_id="t-2")
    rec = Recorder(ws, user_input="t", model="stub-model")
    ag = Agent(ws, registry, _BatchLLM(), rec,
               progress=_CancelOnToolDone(tok, after=1), cancel_token=tok)
    _wire(ws, registry, rec, ag)

    with pytest.raises(cancel.Cancelled):
        ag.run()

    assert _count(rec.events, "tool") == 1
    assert ag.tool_calls == 1


# ---------------------------------------------------------------- 3. 在飞的请求
def test_cancel_aborts_inflight_llm_request(monkeypatch):
    """等模型返回时不靠超时：`shutdown()` 让 read 立刻抛，且**不重试**。"""
    from openthree import llm as llm_mod

    tok = cancel.CancelToken(task_id="t-3")
    reading = threading.Event()
    released = threading.Event()

    class FakeSock:
        def shutdown(self, how):
            released.set()

        def settimeout(self, timeout):
            pass

    class FakeConn:
        """`getresponse()` 阻塞到 socket 被 shutdown —— 就像一次真的 read。"""

        def __init__(self, *a, **k):
            self.sock = FakeSock()

        def connect(self):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            reading.set()
            released.wait(5.0)
            raise OSError("socket 被关闭")

        def close(self):
            pass

    monkeypatch.setattr(llm_mod.http.client, "HTTPConnection", FakeConn)
    cfg = llm_mod.LLMConfig(api_key="k", base_url="http://127.0.0.1:1",
                            model="stub-model", retries=3)
    out = {}

    def _run():
        cancel.bind(tok)              # 令牌是线程本地的：这里绑，`chat` 就地取到
        try:
            out["r"] = llm_mod.LLM(cfg).chat([{"role": "user", "content": "hi"}])
        finally:
            cancel.unbind(tok)

    worker = threading.Thread(target=_run)
    worker.start()
    assert reading.wait(2.0), "假连接没有进入 getresponse"

    started = time.monotonic()
    tok.request("测试：取消")
    worker.join(2.0)
    elapsed = time.monotonic() - started

    assert not worker.is_alive(), "取消没能让阻塞中的 read 返回"
    assert elapsed < 1.0, f"停止慢了：{elapsed:.2f}s"
    assert out["r"]["error_kind"] == "cancelled"
    assert out["r"]["attempts"] == 1, "取消了却还重试了一次"


# ---------------------------------------------------------------- 4. 不续轮
def test_cancel_does_not_open_next_round(tmp_path):
    """撞到步数上限后被取消 ⇒ 不再自动开下一轮（漏了这处就是「取消了还在烧」）。"""
    tok = cancel.CancelToken(task_id="t-4")
    rt = Runtime(tmp_path, quiet=True, heartbeat=0, max_iterations=1, max_rounds=5)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _EveryStepToolLLM(rt.config)

    said = []

    def _say(msg: str) -> None:
        said.append(msg)
        # 「自动开始第 2 轮」这句话说完，循环立刻回到顶部 —— 正是检查点的位置
        if "自动开始第 2 轮" in msg:
            tok.request("测试：取消")

    rt.say = _say
    doc = rt.run("做点事", rounds=5, cancel_token=tok)

    assert doc["rounds_run"] == 1, f"取消之后还开了下一轮：{doc.get('rounds_run')} 轮"
    assert any("自动开始第 2 轮" in s for s in said), "用例没走到续轮那一步"


# ---------------------------------------------------------------- 5. 终态
def test_canceled_is_a_terminal_status_in_recorder(ws):
    """`canceled` 必须原样落盘，不被压成 `done`。"""
    rec = Recorder(ws, user_input="t", model="stub-model")
    doc = rec.finalize(answer="[已按调用方要求停止]", status="canceled")
    assert doc["status"] == "canceled"
    on_disk = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert on_disk["status"] == "canceled"


# ---------------------------------------------------------------- 6. 频道：令牌落在 task 上
def test_a2a_cancel_sets_the_token_on_that_task(tmp_path):
    ch = _channel(tmp_path, cancel_grace="0")
    ch._load_options()
    ch.emit = lambda msg: None

    _, body = _call(ch, "message/send",
                    {"message": _user_msg("跑很久的任务", taskId="t-c")})
    assert body["result"]["status"]["state"] == "working"
    task = ch._tasks.get("t-c")
    assert task["_cancel"].requested is False

    _, body = _call(ch, "tasks/cancel", {"id": "t-c"})
    assert task["_cancel"].requested is True
    assert body["result"]["status"]["state"] == "canceled"


# ---------------------------------------------------------------- 7. 频道：artifact 说实话
def test_a2a_cancel_artifact_says_where_it_stopped(tmp_path):
    """桥在 grace 内收工 ⇒ 终态由**它自己**落，artifact 里带「停在第 N 步」。"""
    ch = _channel(tmp_path, cancel_grace="2")
    ch._load_options()
    ch.emit = lambda msg: None

    _, _ = _call(ch, "message/send",
                 {"message": _user_msg("跑很久的任务", taskId="t-s")})
    task = ch._tasks.get("t-s")

    result = {}

    def _cancel():
        result["r"] = _call(ch, "tasks/cancel", {"id": "t-s"})

    worker = threading.Thread(target=_cancel)
    worker.start()
    time.sleep(0.2)                       # 让 tasks/cancel 先发出信号、进到 wait
    msg = InboundMessage(text="x", channel="a2a", channel_type="a2a",
                         reply_token="t-s", cancel=task["_cancel"],
                         raw={"run": task["_run"]})
    # 「请求过取消」≠「真的被中止」：Agent 真的抛出 Cancelled 时才会置这一位，
    # `send_text` 判的是它（否则落在最后一个检查点之后的取消会骗掉一份完整答案）。
    task["_cancel"].mark_aborted()
    ch.send_text(msg, "[已按调用方要求停止] 停在第 3 步（最后动作：run_shell）。")
    worker.join(5.0)

    assert not worker.is_alive()
    status, body = result["r"]
    assert status == 200
    assert body["result"]["status"]["state"] == "canceled"
    assert "停在第 3 步" in _text_of(task), "取消了却没说清停在哪"


# ---------------------------------------------------------------- 8. 队列里未开跑
def test_cancel_before_start_does_not_run():
    """排队时被取消 ⇒ ReAct 一次都没跑。"""
    class _RT:
        def __init__(self):
            self.calls = 0

        def run(self, *a, **k):
            self.calls += 1
            return {"answer": "不该跑到这里"}

    rt = _RT()
    bridge = AgentBridge(rt, echo=False)
    tok = cancel.CancelToken(task_id="t-8")
    tok.request("排队时取消")
    msg = InboundMessage(text="hi", channel="a2a", channel_type="a2a", cancel=tok)
    replies = []
    bridge._handle(msg, lambda m, text: replies.append(text))

    assert rt.calls == 0, "被取消的消息仍然进了 ReAct"
    assert replies and "取消" in replies[0]


# ---------------------------------------------------------------- 9. follow up 换令牌
def test_follow_up_gets_a_fresh_token(tmp_path):
    """同一 taskId 追问换新令牌：旧 run 的取消不该杀掉新 run，反之亦然。"""
    ch = _channel(tmp_path, cancel_grace="0")
    ch._load_options()
    sent = []
    ch.emit = sent.append

    _call(ch, "message/send", {"message": _user_msg("第一句", taskId="t-f")})
    first = sent[-1].cancel
    _call(ch, "message/send", {"message": _user_msg("第二句", taskId="t-f")})
    second = sent[-1].cancel

    assert first is not None and second is not None
    assert first is not second, "follow up 复用了同一个令牌"

    first.request("取消上一轮")
    assert second.requested is False, "上一轮的取消污染了这一轮"


# ---------------------------------------------------------------- 10. 卡死的命令（阶段 2）
def test_run_shell_can_be_killed_while_blocked():
    """阶段 2 的核心：卡在命令里也能被停，而不是等它自己的 timeout。

    阶段 1 对「已经卡死的命令」上界是 `limit + 15s`（最长 3615s），等于没治；
    这里断言的是**毫秒级** —— 它靠 `proc.kill()` 唤醒阻塞中的 `communicate`。
    """
    from openthree.builtins import run_shell

    tok = cancel.CancelToken(task_id="t-shell")
    out = {}

    def _run():
        cancel.bind(tok)
        try:
            out["r"] = run_shell('python -c "import time; time.sleep(30)"', timeout=60)
        finally:
            cancel.unbind(tok)

    worker = threading.Thread(target=_run)
    worker.start()
    time.sleep(0.8)                  # 让子进程真的起来、communicate 进到阻塞
    started = time.monotonic()
    tok.request("测试：取消卡死的命令")
    worker.join(10.0)
    elapsed = time.monotonic() - started

    assert not worker.is_alive(), "卡死的命令没能被停下来"
    assert elapsed < 5.0, f"停止用了 {elapsed:.1f}s —— 阶段 2 应当是毫秒级"
    assert "已取消" in out["r"], "返回值没说清它是被取消结束的"


# ---------------------------------------------------------------- 11. 硬退出的两个判据
def test_hard_exit_is_not_armed_when_nothing_is_running(tmp_path, monkeypatch):
    """没有「正在跑的那一轮」时**不能布防**。

    这条是实测踩出来的：`running_of()` 返回 None 时，`None is not token` 恒为
    False（轮询永远不返回），grace 到点就 `os._exit(137)` —— 全量 pytest 因此
    一次以 exit=137 中止。没有在跑的令牌就没有「卡住」。
    """
    from openthree.channels import a2a as a2a_mod

    ch = _channel(tmp_path, cancel_hard_grace="0.3")
    ch._load_options()
    fired = []
    monkeypatch.setattr(a2a_mod.os, "_exit", lambda code: fired.append(code))

    ch._arm_hard_exit("t-nothing", None)
    time.sleep(0.8)
    assert fired == [], "没有在跑的任务却布防了硬退出"


def test_hard_exit_fires_when_the_run_never_stops(tmp_path, monkeypatch):
    """真的卡住（令牌一直绑着）⇒ 到点必须开火，否则阶段 3 等于没做。

    `_finish()` 会 set `done`，所以判据只能是「这一轮还绑着」（`running_of`）。
    这个用例把 `os._exit` 换掉，于是既能验证「会开火」，又不会真的杀掉 pytest。
    """
    from openthree.channels import a2a as a2a_mod

    ch = _channel(tmp_path, cancel_hard_grace="0.3")
    ch._load_options()
    tok = cancel.CancelToken(task_id="t-stuck")
    fired = []
    monkeypatch.setattr(a2a_mod.os, "_exit", lambda code: fired.append(code))

    cancel.bind(tok)                     # 模拟「这一轮还在跑，且停不下来」
    try:
        ch._arm_hard_exit("t-stuck", tok)
        time.sleep(1.0)
    finally:
        cancel.unbind(tok)

    assert fired == [137], "卡住的任务没有被硬退出兜住"


# ---------------------------------------------------------------- 12. 硬退出旋钮的坏值
def test_cancel_hard_grace_bad_or_false_means_off(tmp_path):
    """`false` / `no` / `off` / 乱写 一律落到 **0（关）**，绝不退回默认 30。

    这个旋钮会 `os._exit(137)`。YAML 里写 `false` 会变成字符串 `"False"`，
    `_as_float` 抛 `A2aError` —— 若那时退回默认，用户明确写下的「关」就被静默
    改成了「取消一个任务就把整个进程杀掉」。对一个会杀进程的旋钮，坏值只有
    一个安全的去处。
    """
    for raw in ("false", "False", "no", "off", "关", "abc"):
        ch = _channel(tmp_path, cancel_hard_grace=raw)
        ch._load_options()
        assert ch.cancel_hard_grace == 0.0, f"{raw!r} 没有落到 0（当前 {ch.cancel_hard_grace}）"

    # 没配置 = 默认开（这是默认值，不是坏值）
    ch = _channel(tmp_path)
    ch._load_options()
    assert ch.cancel_hard_grace == 30.0

    ch = _channel(tmp_path, cancel_hard_grace="12")
    ch._load_options()
    assert ch.cancel_hard_grace == 12.0
