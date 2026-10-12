"""Runtime：把「一、二、三」装配起来。

启动顺序（很重要，这就是「生」的顺序）：

    道   LLM 就绪
    一   workspace.init() 铺好 .openthree/，读出系统提示词
    二   registry reload，把 functions/*.py 变成工具
    三   agent 进入 ReAct 循环
    万物 模型自己写 function / 改提示词 → 下一轮生效 → 能力自生长
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .agent import Agent
from .builtins import BUILTIN_FUNCTIONS
from .kernel import KERNEL
from .llm import LLM, LLMConfig, load_config
from .recorder import Recorder
from .registry import Registry
from .report import write_report
from .workspace import Workspace


class Runtime:
    def __init__(self, project_dir: Optional[Path] = None, *,
                 model: Optional[str] = None,
                 max_iterations: int = 200,
                 force_prompts: bool = False,
                 quiet: bool = False) -> None:
        self.project_dir = Path(project_dir or Path.cwd()).resolve()
        self.quiet = quiet
        self.max_iterations = max_iterations

        self.ws = Workspace(self.project_dir).init(force_prompts=force_prompts)
        self.config: LLMConfig = load_config(self.project_dir, model_override=model)
        self.registry = Registry(self.ws)
        for name, fn in BUILTIN_FUNCTIONS.items():
            self.registry.register_builtin(name, fn)
        self.registry.freeze_builtins()
        self.registry.reload()

        self.llm = LLM(self.config)
        self.recorder: Optional[Recorder] = None
        self.agent: Optional[Agent] = None

        # 挂进内核，用户写的 function 就能拿到
        KERNEL.workspace = self.ws
        KERNEL.registry = self.registry
        KERNEL.config = {
            "model": self.config.model,
            "project_dir": str(self.project_dir),
        }

    # ---------------------------------------------------------------- 对外
    def say(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)

    def check(self) -> Optional[str]:
        if not self.config.usable:
            return (
                "缺少模型配置。请任选一种方式提供 api_key / base_url / model：\n"
                "  1) 环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / OPENTHREE_MODEL\n"
                "  2) ~/.openthree/config.yaml  或  <项目>/.openthree/config.yaml\n\n"
                "  llm:\n    api_key: sk-xxx\n    base_url: https://api.deepseek.com\n"
                "    model: deepseek-chat\n"
            )
        return None

    def run(self, user_input: Optional[str] = None, *,
            append: bool = False, session_id: Optional[str] = None) -> Dict[str, Any]:
        """跑一轮任务。user_input 为 None 时沿用已有的 user-prompts.md。"""
        problem = self.check()
        if problem:
            raise RuntimeError(problem)

        if user_input is not None:
            self.ws.submit(user_input, append=append)

        self.recorder = Recorder(self.ws, session_id=session_id,
                                 user_input=self.ws.read_user_prompt(),
                                 model=self.config.model)
        KERNEL.recorder = self.recorder

        self.agent = Agent(self.ws, self.registry, self.llm, self.recorder,
                           max_iterations=self.max_iterations)
        KERNEL.agent = self.agent

        self.say(f"道生一  工作区 {self.ws.dir}")
        self.say(f"一生二  {len(self.registry)} 个工具"
                 f"（内置 {len(self.registry.builtins)} / 自造 {len(self.registry.user_tools())}）")
        self.say(f"二生三  ReAct 循环开始（上限 {self.max_iterations} 步，模型 {self.config.model}）")

        try:
            answer = self.agent.run()
            status = "done" if self.agent.finished else "unfinished"
        except KeyboardInterrupt:  # pragma: no cover
            answer = "[用户中断]"
            status = "interrupted"
        except Exception as exc:  # noqa: BLE001
            import traceback
            answer = f"[运行时异常] {type(exc).__name__}: {exc}"
            status = "error"
            if self.recorder:
                self.recorder.record("error", summary=answer,
                                     data={"traceback": traceback.format_exc()[-4000:]})

        doc = self.recorder.finalize(answer=answer, status=status)

        # 每轮结束都刷新报告 —— 可观测是常态，不是事后
        try:
            write_report(self.ws)
        except Exception:  # noqa: BLE001
            pass

        stats = doc["stats"]
        self.say(f"三生万物 完成：{stats['steps']} 步 / {stats['tool_calls']} 工具调用 "
                 f"/ {stats['evolutions']} 次演化")
        self.say(f"报告 {self.ws.report_file}")
        self.say(f"记录 {self.recorder.session_file}")
        return doc

    def report(self) -> Path:
        return write_report(self.ws)

    # ---------------------------------------------------------------- 自省
    def self_inspect(self) -> str:
        """不需要模型的一句话自省摘要。"""
        reg = self.registry
        lines = [
            f"工作区   {self.ws.dir}",
            f"系统提示词 {self.ws.system_prompt_file} ({len(self.ws.read_system_prompt())} 字符)",
            f"任务     {self.ws.user_prompt_file}",
            f"工具     {len(reg)} 个（内置 {len(reg.builtins)} / 自造 {len(reg.user_tools())}）",
        ]
        for t in reg.user_tools():
            lines.append(f"         ↳ {t.name}  ← {Path(t.source_file or '').name}")
        if reg.load_errors:
            lines.append(f"⚠ 加载异常 {len(reg.load_errors)} 个：")
            for e in reg.load_errors:
                lines.append(f"         ↳ {Path(e.file).name}: {e.error}")
        return "\n".join(lines)
