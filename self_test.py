"""离线自测：用桩模型跑通 道→一→二→三→万物，不联网。

覆盖：
  1. workspace 自举（.openthree/ 铺好）
  2. registry 扫描 functions/*.py → 工具
  3. ReAct 循环多轮
  4. 自我演化（模型写 function → reload → 新工具可用）
  5. 可观测（logs/ 各类 json 落盘）
  6. three-report.html 生成
  7. 配置分层（env < 用户级 < 项目级；不再复用 local-llm 兜底）
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from openthree.kernel import KERNEL          # noqa: E402
from openthree.runtime import Runtime        # noqa: E402
from openthree.llm import load_config        # noqa: E402

SANDBOX = ROOT / "_sandbox"


class StubLLM:
    """按脚本走的桩模型：模拟「造工具 → 用它 → 收尾」。"""
    def __init__(self, cfg):
        self.config = cfg
        self.step = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.step += 1
        tool_names = {t["function"]["name"] for t in (tools or [])}

        if self.step == 1:
            code = '''\
"""自造的：斐波那契。"""

def fib(n: int) -> str:
    """算第 n 个斐波那契数。

    Args:
        n: 项数，从 0 开始
    """
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return f"fib({n}) = {a}"
'''
            asserted = "reload_functions" in tool_names and "write_function" in tool_names
            msg = {"role": "assistant",
                   "content": "内置工具里没有斐波那契。我造一个 function 给自己用——这就是演化。"}
            if asserted:
                msg["tool_calls"] = [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "write_function",
                                 "arguments": json.dumps({"name": "fib", "code": code})},
                }]
            return _ok(msg)

        if self.step == 2:
            return _ok({
                "role": "assistant", "content": "写好了，让注册器扫描它。",
                "tool_calls": [{"id": "call_2", "type": "function",
                                "function": {"name": "reload_functions", "arguments": "{}"}}],
            })

        if self.step == 3:
            has_fib = "fib" in tool_names
            return _ok({
                "role": "assistant",
                "content": f"现在 fib 在我的工具列表里吗？{has_fib}。用它算一下。",
                "tool_calls": [{"id": "call_3", "type": "function",
                                "function": {"name": "fib", "arguments": json.dumps({"n": 10})}}],
            })

        return _ok({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{self.step}", "type": "function",
                            "function": {"name": "finish",
                                         "arguments": json.dumps({
                                             "answer": "我为自己造出了 fib 工具，并验证 fib(10) = 55。演化成功。"})}}],
        })


def _ok(msg):
    return {"message": msg, "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                      "total_tokens": 150},
            "elapsed_ms": 12.3, "raw_request": {"model": "stub", "messages": []},
            "raw_response": {"choices": [{"message": msg, "finish_reason": "tool_calls"}]},
            "error": None}


def _fresh_dir(path: Path) -> Path:
    """清空并重建沙箱（重试以对抗 Windows 文件句柄延迟释放）。"""
    # 先清扫上次回退时遗留的带时间戳沙箱目录，别让残渣无声堆积。
    for stale in path.parent.glob(f"{path.name}-[0-9]*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    if path.exists():
        for _ in range(3):
            try:
                shutil.rmtree(path)
                break
            except PermissionError:
                time.sleep(0.4)
        else:
            # 退而求其次：换一个带时间戳的目录，保证测试能跑
            # （正常路径上面已清空，能走到这里说明目录仍被占用）
            fallback = path.with_name(f"{path.name}-{int(time.time())}")
            print(f"警告：无法清空 {path}（句柄未释放），改用 {fallback}",
                  file=sys.stderr)
            path = fallback
    path.mkdir(parents=True)
    return path


def main() -> int:
    global SANDBOX
    SANDBOX = _fresh_dir(SANDBOX)
    print(f"沙箱 {SANDBOX}")

    rt = Runtime(SANDBOX, quiet=False)
    rt.config.api_key = "stub"     # 绕开配置检查
    rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = StubLLM(rt.config)

    print("\n===== 开跑 =====")
    doc = rt.run('造一个 fib 工具并验证 fib(10)')

    print("\n===== 断言 =====")
    checks = []

    def check(name, cond, extra=""):
        checks.append((name, bool(cond), extra))
        print(f"  {'✓' if cond else '✗'} {name} {extra}")

    # 1 工作区
    check("workspace 初始化", (SANDBOX / ".openthree" / "prompts" / "system-prompts.md").exists())
    check("user-prompt 落盘", (SANDBOX / ".openthree" / "prompts" / "user-prompts.md").exists()
          and "fib" in (SANDBOX / ".openthree" / "prompts" / "user-prompts.md").read_text(encoding="utf-8"))

    # 2 自我演化
    fib_py = SANDBOX / ".openthree" / "functions" / "fib.py"
    check("模型自造了 fib.py", fib_py.exists())
    check("fib 成为工具", "fib" in rt.registry.names(), f"工具数={len(rt.registry)}")
    check("fib 分类为自造", any(t.name == "fib" and t.origin == "user"
                              for t in rt.registry.tools.values()))

    # 3 循环
    check("ReAct 跑满 4 步", doc["stats"]["steps"] == 4, f"steps={doc['stats']['steps']}")
    check("工具调用 4 次", doc["stats"]["tool_calls"] == 4, f"={doc['stats']['tool_calls']}")
    check("最终答复含结论", "55" in doc["answer"] or "演化成功" in doc["answer"])

    # 4 可观测
    logs = SANDBOX / ".openthree" / "logs"
    check("session json", len(list(logs.glob("session-*.json"))) == 1)
    check("events.jsonl", (logs / "events" / "events.jsonl").exists())
    check("steps json", len(list(logs.glob("steps/step-*.json"))) == 4,
          f"{len(list(logs.glob('steps/step-*.json')))} 个")
    check("llm json", len(list(logs.glob("llm/llm-*.json"))) == 4)
    check("evolution.jsonl", (logs / "evolution.jsonl").exists())

    evo = [json.loads(l) for l in (logs / "evolution.jsonl").read_text(encoding="utf-8").splitlines()]
    actions = [e["data"].get("action") for e in evo]
    check("演化史记录了 write_function", "write_function" in actions, str(actions))
    check("演化史记录了 reload", "reload_functions" in actions)

    # tool 事件里能查到 fib 的执行结果
    sess = json.loads(list(logs.glob("session-*.json"))[0].read_text(encoding="utf-8"))
    fib_out = [e for e in sess["events"] if e["kind"] == "tool"
               and e["data"]["name"] == "fib"]
    check("fib 调用结果被记录", fib_out and "55" in fib_out[0]["data"]["output"],
          fib_out[0]["data"]["output"] if fib_out else "无")

    # 5 报告
    report = SANDBOX / ".openthree" / "three-report.html"
    check("three-report.html 生成", report.exists(), f"{report.stat().st_size if report.exists() else 0}B")
    html = report.read_text(encoding="utf-8")
    check("报告含演化代码", "def fib" in html or "fib(n) = " in html or "write_function" in html)
    check("报告含时间线", "timeline" in html and "三生万物" in html)

    # 6 配置分层：不再复用 local-llm 的 ~/.local-llm/config.yaml 兜底
    #    （空项目 + 无用户级配置时，任何一层都不该给出 api_key）
    #
    #    注意：只清环境变量不够 —— 本机若存在用户级 ~/.openthree/config.yaml，
    #    它仍是合法的一层，断言会失败。这里把 HOME / USERPROFILE 指向空目录，
    #    真正隔离掉「用户级」这一层，测的才是「没有配置就不该有 key」。
    import os
    _saved_env = {k: os.environ.pop(k, None)
                  for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL",
                            "OPENTHREE_MODEL", "OPENAI_MODEL")}
    _saved_home = {k: os.environ.get(k)
                   for k in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")}
    empty = SANDBOX / "_cfg_empty"
    empty.mkdir(parents=True, exist_ok=True)
    fake_home = SANDBOX / "_cfg_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    try:
        os.environ["HOME"] = str(fake_home)
        os.environ["USERPROFILE"] = str(fake_home)
        isolated = load_config(empty)
    finally:
        for k, v in _saved_env.items():
            if v is not None:
                os.environ[k] = v
        for k, v in _saved_home.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("配置不从 local-llm 兜底（api_key 为空）", isolated.api_key == "",
          f"api_key={isolated.api_key!r}")
    check("配置 model 回落内置默认", isolated.model == "deepseek-v4.1-flash", isolated.model)
    check("配置不可用（usable=False）", isolated.usable is False)
    check("llm.py 不含 local-llm 字样",
          "local-llm" not in (ROOT / "openthree" / "llm.py").read_text(encoding="utf-8"))

    ok = all(c[1] for c in checks)
    print(f"\n===== {'全部通过' if ok else '有失败'} ({sum(c[1] for c in checks)}/{len(checks)}) =====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
