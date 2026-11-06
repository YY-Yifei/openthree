"""`decide` 内置工具的测试：**不打真网**。

传输层用 `monkeypatch.setattr(decide, "post_json", fake)` 换掉，于是这里验的是
内核自己那部分 —— 门控、入参校验、URL 原样、重试、错误文案、模型可见的文案。
"""
from __future__ import annotations

import json

import pytest

from openthree import decide as D
from openthree.builtins import installed_builtins
from openthree.llm import TransportError
from openthree.registry import Registry
from openthree.tool import build_tool

URL = "https://jev.example/systemone/v1/systemone"
KEY = "sk-test-abcdefghijklmnop"

QUESTIONS = {
    "team": {
        "type": "choice",
        "instructions": "这条消息该由哪个团队处理？",
        "criteria": {"billing": "账单", "technical": "故障"},
    },
}

# jev-test/jev.md:11-22 的样本（含 `usage`：服务端会回，内核不转发）
SAMPLE = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "team": {"type": "choice", "choice": "billing",
                 "probabilities": {"billing": 0.84, "technical": 0.16},
                 "confidence": 0.76},
    },
    "usage": {"input_tokens": 467, "output_tokens": 73, "cost": 0.000019614},
    "id": "gen-dec-1793843181-9ub8fnJeibxXff0iSap8",
}

# 模型可见的文本里一律不该出现的词（人类裁决：不提价格，连文档也不提）
_PRICE_WORDS = ("计费", "费用", "价格", "价钱", "单价", "收费", "花钱", "成本",
                "cost", "usage")


@pytest.fixture
def jev_env(monkeypatch):
    monkeypatch.setenv("JEV_BASE_URL", URL)
    monkeypatch.setenv("JEV_API_KEY", KEY)
    monkeypatch.setenv("JEV_MODEL", "jev-1.13.0")


def _install(monkeypatch, *outcomes):
    """换掉传输层，按调用序号依次返回 outcomes（dict 当成功，异常当失败）。"""
    calls = []

    def fake(url, api_key, payload, *, timeout=None, connect_timeout=None):
        idx = len(calls)
        calls.append({"url": url, "key": api_key, "payload": payload,
                      "timeout": timeout, "connect_timeout": connect_timeout})
        out = outcomes[idx] if idx < len(outcomes) else outcomes[-1]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(D, "post_json", fake)
    monkeypatch.setattr(D.time, "sleep", lambda _s: None)   # 重试不真等
    return calls


# ---------------------------------------------------------------- 门控


@pytest.mark.parametrize("missing", ["JEV_BASE_URL", "JEV_API_KEY", "JEV_MODEL"])
def test_missing_any_var_disables(missing, monkeypatch, jev_env):
    monkeypatch.delenv(missing)
    assert D._env_configured() is False
    assert "decide" not in installed_builtins()


def test_all_three_enables(jev_env):
    assert D._env_configured() is True
    assert "decide" in installed_builtins()


def test_no_diagnostic_entry(jev_env):
    """以 `decide` 开头的工具只有它自己：没有「解释它为什么不在」的常驻入口。

    专为一个**默认不存在**的工具服务的常驻入口，会让每一步的工具清单都为它付字节，
    也让模型有机会去调一个不存在的功能。要确认配没配，看环境变量本身。
    """
    assert [n for n in installed_builtins() if n.startswith("decide")] == ["decide"]
    public = [n for n in dir(D) if n.startswith("decide") and not n.startswith("_")]
    assert public == ["decide"], "decide 模块不该有第二个公开的 decide* 入口"


def test_no_defaults_when_empty(monkeypatch):
    for key in ("JEV_BASE_URL", "JEV_API_KEY", "JEV_MODEL"):
        monkeypatch.setenv(key, "   ")      # 只有空白 = 没配，绝不回落到默认值
    assert D._env_configured() is False
    assert "decide" not in installed_builtins()


@pytest.mark.parametrize("bad", ["jev.example/x", "ftp://jev.example/x", "/v1/decide",
                                 "https://jev.example:abc/x", "https://jev.example:99999/x",
                                 "https://jev.example:0/x", "https://:80/x", "https://user@/x",
                                 "https://jev.example/a b"])
def test_bad_base_url_disables(bad, monkeypatch, jev_env):
    """漏 scheme、端口写错（含 `:0`）、没有主机名、带空格，都要在门控这一步被拒。

    `urlsplit` 既不校验端口也不认为 `:80` 这种 netloc 有问题：`.port` / `.hostname`
    取用时才看得出毛病；而空格会让 `http.client` 在写请求行时抛 `InvalidURL`，被包成
    status=0 的连接失败再白重试两次。这些都不该等到发请求那一刻才暴露。"""
    monkeypatch.setenv("JEV_BASE_URL", bad)
    assert D._env_configured() is False
    assert "decide" not in installed_builtins()


def test_registered_as_builtin(jev_env, ws):
    reg = Registry(ws)
    for name, fn in installed_builtins().items():
        reg.register_builtin(name, fn)
    reg.freeze_builtins()
    reg.reload()
    tool = reg.get("decide")
    assert tool is not None
    assert tool.origin == "builtin"
    assert tool.signature_hint() == "decide(state: string, questions: object)"


# ---------------------------------------------------------------- 调用形状


def test_request_url_verbatim(jev_env, monkeypatch):
    """整串地址一个字符都不改：不拼路径、不补尾斜杠。"""
    calls = _install(monkeypatch, SAMPLE)
    D.decide("材料", QUESTIONS)
    assert calls[0]["url"] == URL
    assert calls[0]["payload"] == {"model": "jev-1.13.0", "state": "材料",
                                   "questions": QUESTIONS}


def test_send_timeouts(jev_env, monkeypatch):
    calls = _install(monkeypatch, SAMPLE)
    D.decide("材料", QUESTIONS)
    assert calls[0]["timeout"] == D._TIMEOUT
    assert calls[0]["connect_timeout"] == D._CONNECT_TIMEOUT


def test_success_roundtrip(jev_env, monkeypatch):
    _install(monkeypatch, SAMPLE)
    out = D.decide("材料", QUESTIONS)
    data = json.loads(out)
    assert data["answers"] == SAMPLE["answers"]      # 原样，不改写
    assert data["model"] == SAMPLE["model"]
    assert data["id"] == SAMPLE["id"]
    assert data["attempts"] == 1
    assert "usage" not in data and "cost" not in out  # 服务端回了也不转发


def test_questions_accepts_json_string(jev_env, monkeypatch):
    """命令行把 object 参数传成字符串，所以两种形态都要收。"""
    calls = _install(monkeypatch, SAMPLE)
    D.decide("材料", json.dumps(QUESTIONS, ensure_ascii=False))
    assert calls[0]["payload"]["questions"] == QUESTIONS


# ---------------------------------------------------------------- 重试


def test_retry_policy(jev_env, monkeypatch):
    calls = _install(monkeypatch, TransportError("boom"), TransportError("boom"),
                     SAMPLE)
    data = json.loads(D.decide("材料", QUESTIONS))
    assert len(calls) == 3                 # 2 次重试 = 共 3 次尝试
    assert data["attempts"] == 3


def test_retry_gives_up_after_three(jev_env, monkeypatch):
    calls = _install(monkeypatch, TransportError("boom"))
    out = D.decide("材料", QUESTIONS)
    assert len(calls) == 3
    assert out.startswith("[错误]")
    assert "已重试 2 次" in out


def test_401_not_retried(jev_env, monkeypatch):
    calls = _install(monkeypatch, TransportError("nope", status=401))
    out = D.decide("材料", QUESTIONS)
    assert len(calls) == 1                 # 鉴权失败重试只是把同一个错误再发一遍
    assert "JEV 鉴权失败（401）" in out


# ---------------------------------------------------------------- 入参校验


@pytest.mark.parametrize("state,questions,expect", [
    ("", QUESTIONS, "state 为空"),
    ("   ", QUESTIONS, "state 为空"),
    ("材料", {}, "questions 为空"),
    ("材料", {"q": {"type": "unknown", "instructions": "x"}}, "不认识"),
    ("材料", {"q": {"type": "noul"}}, "缺 instructions"),
    ("材料", {"q": {"type": "choice", "instructions": "x"}}, "没有 criteria"),
    ("材料", {"q": {"type": "score", "instructions": "x", "criteria": ["只有一档"]}},
     "等级列表"),
    ("材料", "not json", "不是合法 JSON"),
    ("材料", [1, 2], "要是 {题名: 题型定义}"),
])
def test_validation_sends_nothing(state, questions, expect, jev_env, monkeypatch):
    calls = _install(monkeypatch, SAMPLE)
    out = D.decide(state, questions)
    assert out.startswith("[错误]") and expect in out
    assert calls == []                     # 形状错了就不发：不为注定 400 的请求跑网络


def test_validation_does_not_rewrite(jev_env, monkeypatch):
    """不替模型补 instructions、不改写 state、不丢多余字段。"""
    calls = _install(monkeypatch, SAMPLE)
    qs = {"team": {"type": "choice", "instructions": "x",
                   "criteria": {"a": "A"}, "extra": "原样保留"}}
    D.decide("  材料 \n 原样 ", qs)
    assert calls[0]["payload"]["state"] == "  材料 \n 原样 "
    assert calls[0]["payload"]["questions"]["team"]["extra"] == "原样保留"


# ---------------------------------------------------------------- 错误映射


@pytest.mark.parametrize("status,expect", [
    (0, "连不上 JEV 端点"),
    (401, "鉴权失败（401）"),
    (403, "路由未授权（403）"),
    (404, "路由不存在（404）"),
    (429, "限流或额度用尽（429）"),
    (500, "服务端错误（500"),
])
def test_error_mapping(status, expect, jev_env, monkeypatch):
    _install(monkeypatch, TransportError("问题", status=status))
    out = D.decide("材料", QUESTIONS)
    assert out.startswith("[错误]") and expect in out


def test_non_json_response(jev_env, monkeypatch):
    _install(monkeypatch, TransportError("响应不是 JSON：<html>", status=200))
    out = D.decide("材料", QUESTIONS)
    assert "不是 JSON" in out


def test_missing_answers(jev_env, monkeypatch):
    _install(monkeypatch, {"model": "m", "id": "i"})
    out = D.decide("材料", QUESTIONS)
    assert "缺 answers" in out


def test_key_never_leaks(jev_env, monkeypatch):
    _install(monkeypatch, TransportError(f"boom {KEY} boom", status=500))
    out = D.decide("材料", QUESTIONS)
    assert KEY not in out
    assert "***" in out


def test_uncallable_without_env(monkeypatch):
    """缺变量时本工具不会被注册；万一被直接调用，也要说清而不是静默发请求。"""
    out = D.decide("材料", QUESTIONS)
    assert out.startswith("[错误]") and "未配置" in out


def test_local_error_not_labelled_as_network(jev_env, monkeypatch):
    """传输层之外的异常不冒充「连不上」：那条文案会把排障方向指向网络与代理。

    典型来源是端点写坏后 `post_json` 在发请求前抛的 `ValueError`。
    """
    _install(monkeypatch, ValueError("Port could not be cast to integer value as 'abc'"))
    out = D.decide("材料", QUESTIONS)
    assert "本地错误（ValueError）" in out
    assert "连不上" not in out


# ---------------------------------------------------------------- 模型看到的描述


def test_schema_shape():
    tool = build_tool(D.decide, origin="builtin")
    params = tool.schema()["function"]["parameters"]
    assert params["required"] == ["state", "questions"]
    assert params["properties"]["questions"] == {
        "type": "object",
        "description": params["properties"]["questions"]["description"],
    }
    assert "会外发" in tool.description.splitlines()[0]


def test_score_documented_as_continuous():
    """`score` 是期望值、不是等级下标 —— 描述写错，模型就会按错的意思用它。

    实网实测（三档）：criteria ["平静","不满","非常愤怒"] 回 score=1.09、
    probabilities={0:0, 1:0.91, 2:0.09}，即 score 是各等级按概率加权的期望值，
    落在两档之间。按「下标」理解就会取整成第 1 档，把 0.09 的那一档彻底丢掉。
    """
    doc = D.decide.__doc__
    score_block = doc.split('- "score"', 1)[1].split("形状", 1)[0]
    assert "连续" in score_block
    assert "不是等级下标" in score_block
    assert "1.09" in score_block          # 一个不可能是下标的实例，比形容词管用


def test_no_price_in_model_facing_text(jev_env, monkeypatch):
    """文案不变量：模型可见的四处（digest 行 / description / schema / 返回值）。"""
    _install(monkeypatch, SAMPLE)
    tool = build_tool(D.decide, origin="builtin")
    digest_line = f"- `{tool.signature_hint()}` — {tool.description.splitlines()[0]}"
    returned = D.decide("材料", QUESTIONS)
    schema_text = json.dumps(tool.schema()["function"]["parameters"],
                             ensure_ascii=False)
    for where, text in (("digest", digest_line),
                        ("description", tool.description),
                        ("schema", schema_text),
                        ("returned", returned)):
        hits = [w for w in _PRICE_WORDS if w in text.lower()]
        assert not hits, f"{where} 里出现了 {hits}"


def test_type_must_be_exact(jev_env, monkeypatch):
    """`"Choice"` 不做归一化：过了校验却原样发出，只会换来一次服务端 400。"""
    calls = _install(monkeypatch, SAMPLE)
    out = D.decide("材料", {"t": {"type": "Choice", "instructions": "判一下",
                                  "criteria": {"a": "A", "b": "B"}}})
    assert out.startswith("[错误]") and "type" in out
    assert calls == []          # 形状不对就不该花一次请求


def test_call_uses_registration_snapshot(jev_env, monkeypatch):
    """注册之后把端点换掉：工具不能照新地址发出去。

    门控是一次性判定，配置也一次定死 —— 否则「没配置就没有这个工具」只挡住了
    注册那一刻，之后的每一次调用都可能把材料发往一个从未被检查过的地方。
    """
    installed_builtins()                     # 注册期：按 URL / KEY 记账
    monkeypatch.setenv("JEV_BASE_URL", "https://evil.example/x")
    monkeypatch.setenv("JEV_API_KEY", "sk-evil-000000000000")

    calls = _install(monkeypatch, SAMPLE)
    D.decide("材料", QUESTIONS)
    assert calls[0]["url"] == URL
    assert calls[0]["key"] == KEY


def test_optional_builtins_freezes_after_first_call(jev_env, monkeypatch):
    """`installed_builtins()` 会被反复调用；第二次起必须是纯查询。

    否则已经注册的 `decide` 会跟着后来的环境变量换端点，甚至因为环境被清空而
    从工具清单里消失（测试与诊断代码都会调它）——「注册期一次判定」就成了空话。
    """
    assert "decide" in installed_builtins()      # 第一次：读环境并冻结
    monkeypatch.delenv("JEV_BASE_URL")
    assert "decide" in installed_builtins()      # 环境变了，冻结结果不变
    monkeypatch.setenv("JEV_BASE_URL", "https://evil.example/x")
    assert "decide" in installed_builtins()


def test_state_object_is_serialized_as_json(jev_env, monkeypatch):
    """模型传对象时要序列化成 JSON，而不是 Python repr（`{'a': 1}` 不是任何合法 JSON）。"""
    calls = _install(monkeypatch, SAMPLE)
    D.decide({"事实": "原话", "n": 1}, QUESTIONS)
    assert calls[0]["payload"]["state"] == '{"事实": "原话", "n": 1}'


def test_url_with_non_ascii_disables(jev_env, monkeypatch):
    """非 ASCII 地址会让 `http.client` 抛 `UnicodeEncodeError`（不在 post_json 的捕获里）。"""
    monkeypatch.setenv("JEV_BASE_URL", "https://jev.example/判定")
    assert D._env_configured() is False


def test_missing_answers_does_not_echo_usage(jev_env, monkeypatch):
    """错误路径也不回显整份响应：`usage` 这类字段不能从这条路漏出来。"""
    _install(monkeypatch, {"model": "m", "id": "i", "usage": {"cost": 0.1}})
    out = D.decide("材料", QUESTIONS)
    assert "缺 answers" in out
    assert "cost" not in out.lower() and "usage" not in out.lower()


def test_missing_answers_with_non_dict_response(jev_env, monkeypatch):
    """响应不是对象时也只给类型名：这条错误路径同样进模型上下文。"""
    _install(monkeypatch, [{"usage": {"cost": 0.1}}])
    out = D.decide("材料", QUESTIONS)
    assert "缺 answers" in out
    assert "cost" not in out.lower() and "usage" not in out.lower()


def test_instructions_must_be_string(jev_env, monkeypatch):
    """`123` 会被 `str()` 蒙过去，然后原样发出去换一个 400 —— 形状错不该花请求。"""
    calls = _install(monkeypatch, SAMPLE)
    out = D.decide("材料", {"t": {"type": "noul", "instructions": 123}})
    assert out.startswith("[错误]") and "instructions" in out
    assert calls == []


def test_non_json_error_text_not_duplicated(jev_env, monkeypatch):
    """`post_json` 的正文已含「响应不是 JSON」，别再叠一层前缀。"""
    _install(monkeypatch, TransportError("响应不是 JSON：<html>", status=200))
    out = D.decide("材料", QUESTIONS)
    assert out.count("不是 JSON") == 1


# ---------------------------------------------------------------- CLI


def _run_cli(args, env):
    """在子进程里跑本模块的 CLI。`env` 必须显式给：子进程不继承 monkeypatch。"""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, "-m", "openthree.decide", *args],
                          cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, env=env)


def test_cli_keeps_internal_settings_off_the_command_line(jev_free_env):
    """尾钩只点名 `decide`：取环境的内部函数不能变成子命令。

    取环境那个函数返回的是**原始** `JEV_API_KEY`；它一旦成为子命令，等于给 key
    开了一条在命令行上打印的路径。
    """
    env = dict(jev_free_env)
    env.update({"JEV_BASE_URL": URL, "JEV_API_KEY": KEY, "JEV_MODEL": "jev-1.13.0"})

    leak = _run_cli(["jev_settings"], env)
    assert leak.returncode != 0
    assert KEY not in leak.stdout + leak.stderr

    helptext = _run_cli(["--help"], env)
    assert KEY not in helptext.stdout + helptext.stderr
    for internal in ("jev_settings", "optional_builtins", "env_configured"):
        assert internal not in helptext.stdout
    assert helptext.returncode == 0 and "decide" in helptext.stdout
