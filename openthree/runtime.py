"""Runtime：把「一、二、三」装配起来。

启动顺序（很重要，这就是「生」的顺序）：

    道   LLM 就绪
    一   workspace.init() 铺好 .openthree/，读出系统提示词
    二   registry reload，把 functions/*.py 变成工具
    三   agent 进入 ReAct 循环
    万物 模型自己写 function / 改提示词 → 下一轮生效 → 能力自生长

可观测分两层：
    * Recorder —— 落盘的真相（logs/、three-report.html），回答「发生过什么」。
    * Progress —— 终端的回声（step / 思考 / 工具 / 心跳），回答「现在在干什么」。
      后者是纯展示，CLI 不会因为模型思考而长时间沉默。

多轮对话（会话）：
    记忆的寿命 = **进程内会话**，由 Runtime 持有，不再每次从磁盘读回。
      * 单次 `openthree "任务"`：不带会话 → 从零开跑，不读任何旧对话。
      * `-i` 交互模式：开一个会话，逐轮累积、跨轮可见（`--turns N` 限制播种轮数）。
      * `--auto` 自主接力：本次接力过程也开一个会话（或搭 `-i` 的会话），
        于是长任务逐轮连贯，收尾后不残留、不影响后续单次任务。
      * `--resume`：**显式**把 transcript 档案里最近若干轮载入为本会话起点。
    每轮收尾仍把 (user, answer) 追加进 append-only 的 transcript.jsonl 作档案/可观测，
    但它**不再被自动回读**。用户的完整原始输入因此永不丢失。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .agent import Agent, resolve_context_budget
from .builtins import BUILTIN_FUNCTIONS
from .kernel import KERNEL
from .llm import (DEFAULT_CONTEXT_WINDOW_TOKENS, LLM, LLMConfig, load_config,
                  parse_token_count)
from .progress import Progress
from .recorder import Recorder
from .registry import Registry
from .report import write_report
from .workspace import Workspace


class Runtime:
    def __init__(self, project_dir: Optional[Path] = None, *,
                 model: Optional[str] = None,
                 max_iterations: Optional[int] = None,
                 force_prompts: bool = False,
                 quiet: bool = False,
                 tool_result_limit: Optional[int] = None,
                 max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None,
                 context_budget_chars: Optional[int] = None,
                 context_window_tokens: Optional[int] = None,
                 heartbeat: float = 15.0,
                 turns: Optional[int] = None) -> None:
        self.project_dir = Path(project_dir or Path.cwd()).resolve()
        self.quiet = quiet
        self.max_iterations = max_iterations          # None = 交给模型（.openthree/limits.json）
        self.tool_result_limit = tool_result_limit    # 同上：显式才锁，否则模型可自管
        self.max_tokens = max_tokens
        self.temperature = temperature
        # 先记住是否显式给了预算/窗口，等 config 读出来后再统一解析。
        self._context_budget_arg = context_budget_chars
        self._context_window_arg = context_window_tokens
        self.heartbeat = heartbeat
        # 会话内播种多少轮（默认 4，0 = 关闭记忆，负数 = 全部）。
        # 注意：这只是「会话」的上限，没有会话时一律不播种、不读盘。
        self.turns = 4 if turns is None else int(turns)
        # 进程内会话：之前几轮的 (user, answer) 暂存在这里，供后续轮次播种。
        self._session_active = False
        self._session_turns: List[Dict[str, Any]] = []

        self.ws = Workspace(self.project_dir).init(force_prompts=force_prompts)
        self.config: LLMConfig = load_config(self.project_dir, model_override=model)
        # CLI 显式给的上下文窗口覆盖 config 里的值（config 已在 load_config 里兜过底）。
        if self._context_window_arg is not None:
            parsed_window = parse_token_count(self._context_window_arg)
            if parsed_window:
                self.config.context_window_tokens = int(parsed_window)
        self.context_window_tokens = int(self.config.context_window_tokens)
        # 预算来源只有这里一处：显式 --context-budget 优先，否则按窗口推导
        # （模型仍可在运行期用 context_budget 在策略文件里改它，见 agent）。
        budget_arg = (None if self._context_budget_arg is None
                      else parse_token_count(self._context_budget_arg))
        self._explicit_budget = budget_arg
        self.context_budget_chars = resolve_context_budget(
            budget_arg, self.context_window_tokens)
        self.context_budget_source = ("显式 --context-budget" if budget_arg is not None
                                      else f"按窗口 {_fmt_tokens(self.context_window_tokens)} 推导")
        self.registry = Registry(self.ws)
        for name, fn in BUILTIN_FUNCTIONS.items():
            self.registry.register_builtin(name, fn)
        self.registry.freeze_builtins()
        self.registry.reload()

        self.llm = LLM(self.config)
        self.recorder: Optional[Recorder] = None
        self.agent: Optional[Agent] = None
        self.user_input: str = ""
        self.user_text: str = ""

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
                "    model: deepseek-v4.1-flash\n"
            )
        return None

    # ---------------------------------------------------------------- 会话
    def begin_session(self, *, resume: int = 0, with_history: bool = True) -> int:
        """开一个进程内会话：后续每一轮都能看到「本次会话内之前几轮」。

        `-i` / `--auto` 用它来把逐轮对话串起来。记忆**只活在进程内**——
        默认不读盘，只有 `resume` 显式要求时才从 transcript 载入最近若干轮
        作为起点（`--resume`，answer 一并带回，让模型知道之前聊过什么）。

        Args:
            resume: 从 transcript 档案载入最近多少轮作为会话起点；0 = 不读盘。
            with_history: 是否播种（False = 开会话但记忆关闭，等价于自增计数）。

        Returns:
            载入的轮数（不读盘时为 0）。
        """
        self._session_active = True
        self._session_turns = []
        loaded = 0
        if resume and with_history:
            loaded = self.extend_session(resume)
        return loaded

    def end_session(self) -> None:
        """结束会话，丢弃进程内记忆（下一轮单次任务不受影响）。"""
        self._session_active = False
        self._session_turns = []

    def extend_session(self, n: int) -> int:
        """从 transcript 档案载入最近 n 轮，**追加**进当前会话起点。

        只有 `--resume` 这条显式路径会用到；返回实际载入的轮数。
        """
        n = int(n or 0)
        if n == 0:
            return 0
        try:
            turns = self.ws.read_turns(n)
        except Exception:  # noqa: BLE001
            return 0
        for t in turns:
            self._session_turns.append(dict(t))
        return len(turns)

    def _effective_turns(self) -> List[Dict[str, Any]]:
        """本轮该播种的会话轮次（旧 → 新，受 `--turns` 限制）。"""
        if not self._session_active:
            return []
        if self.turns == 0:
            return []
        turns = self._session_turns
        return turns if self.turns < 0 else turns[-self.turns:]

    def run(self, user_input: Optional[str] = None, *,
            append: bool = False, session_id: Optional[str] = None,
            images: Optional[Sequence[Union[str, Path]]] = None,
            session: bool = False) -> Dict[str, Any]:
        """跑一轮任务。user_input 为 None 时沿用已有的 user-prompts.md。

        images 是本轮携带的图片（本地路径或 http/data URL）。本地文件会先
        复制进 .openthree/assets/ 去重归档，再交给 Agent 注入进 user 消息。

        session 为 True（或已由 begin_session 开会话）时，本轮的播种来自
        进程内会话；否则从零开跑，**不读磁盘**。
        """
        problem = self.check()
        if problem:
            raise RuntimeError(problem)

        # 这一轮是否处于会话中：显式 session=True 或此前已 begin_session()。
        in_session = bool(session) or self._session_active
        if session:
            self._session_active = True

        # 图片先归置成本地绝对路径 / URL（本地文件复制进 assets 去重）
        image_refs: List[str] = []
        if images:
            image_refs = self.ws.stage_images([str(i) for i in images if str(i).strip()])
            if self.ws._last_image_error:
                self.say(f"[提示] 有图片未能归档：{self.ws._last_image_error}")

        if user_input is not None:
            self.ws.submit(user_input, append=append, images=image_refs or None)
        elif image_refs:
            # 沿用已有任务但补了图：把图片标记挂到当前 user-prompts.md 上
            base = self.ws.read_user_prompt()
            marker = self.ws.images_marker(image_refs)
            if marker and marker.strip() not in base:
                self.ws.write_user_prompt(base.rstrip() + "\n" + marker)

        # 本轮用户输入：显式传入就用原文（完整保留），否则沿用 user-prompts.md
        self.user_input = (user_input if user_input is not None
                           else self.ws.read_user_prompt())
        # 图片清单：显式传入优先；否则从 user-prompts.md 的标记里还原
        if not image_refs:
            image_refs = self.ws.parse_images_from_prompt(self.user_input)
        # 入档用的用户原文：剥掉图片标记（图片单独落在 transcript 的 images 字段）
        self.user_text = self.ws.strip_images_marker(self.user_input)

        self.recorder = Recorder(self.ws, session_id=session_id,
                                 user_input=self.user_text or self.user_input,
                                 model=self.config.model)
        KERNEL.recorder = self.recorder

        # 进度回声：CLI 上「还活着、在干什么」。quiet 时它什么都不说。
        progress = Progress(heartbeat=self.heartbeat, enabled=not self.quiet)

        self.agent = Agent(self.ws, self.registry, self.llm, self.recorder,
                           max_iterations=self.max_iterations,
                           tool_result_limit=self.tool_result_limit,
                           max_tokens=self.max_tokens,
                           temperature=self.temperature,
                           # 传**原始**显式参数（None = 未显式给）：Agent 据此决定
                           # 「策略文件里的预算」能否生效；已推导的默认值不当覆写。
                           context_budget_chars=self._explicit_budget,
                           seed_turns=self._effective_turns(),
                           progress=progress)
        self.agent.current_images = list(image_refs)
        self.agent.task_text = self.user_text
        KERNEL.agent = self.agent

        self.say(f"道生一  工作区 {self.ws.dir}")
        self.say(f"一生二  {len(self.registry)} 个工具"
                 f"（内置 {len(self.registry.builtins)} / 自造 {len(self.registry.user_tools())}）")
        self.say(f"二生三  ReAct 循环开始（上限 {self.agent.max_iterations} 步，模型 {self.config.model}）")
        if image_refs:
            self.say(f"       本轮输入含 {len(image_refs)} 张图片（多模态）")
        self.say(f"       上下文窗口 {_fmt_tokens(self.context_window_tokens)} token"
                 f" → 预算 {_fmt_chars(self.agent.context_budget_chars)} 字符"
                 f"（来源：{self.agent.context_budget_source()}；模型可用 context_budget 改）")
        self.say(f"       运行边界：步数 ≤ {self.agent.max_iterations}、"
                 f"生成 ≤ {self.agent.max_tokens} token、工具结果 ≤ {self.agent.tool_result_limit} 字符"
                 f"（模型可用 set_limits 改）")
        if self.heartbeat > 0 and not self.quiet:
            self.say(f"       每 {self.heartbeat:g}s 打一次心跳，长任务不会看起来卡死")

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
        finally:
            progress.close()

        doc = self.recorder.finalize(answer=answer, status=status)

        # 本轮对话入档：用户的完整原始输入 append 进 transcript，永不丢失。
        # 这只写不读（档案/可观测）；跨轮记忆走进程内会话，见 _effective_turns。
        try:
            self.ws.append_turn(self.user_text, answer, images=image_refs or None)
        except Exception as exc:  # noqa: BLE001
            if self.recorder:
                self.recorder.record("error", summary="对话记录写入失败",
                                     data={"error": f"{type(exc).__name__}: {exc}"})

        # 会话内累积本轮 (用户原文, 结论)，供同一会话的后续轮次播种。
        # 没有会话（单次任务）时不累积，于是收尾即忘、不留痕。
        if in_session:
            self._session_turns.append({
                "user": self.user_text,
                "answer": answer or "",
                "images": list(image_refs),
            })

        # 每轮结束都刷新报告 —— 可观测是常态，不是事后
        try:
            write_report(self.ws)
        except Exception as exc:  # noqa: BLE001
            if self.recorder:
                self.recorder.record("error", summary="报告生成失败",
                                     data={"error": f"{type(exc).__name__}: {exc}"})

        stats = doc["stats"]
        self.say(f"三生万物 完成：{stats['steps']} 步 / {stats['tool_calls']} 工具调用 "
                 f"/ {stats['evolutions']} 次演化")
        self.say(f"报告 {self.ws.report_file}")
        self.say(f"记录 {self.recorder.session_file}")
        return doc

    def run_auto(self, user_input: Optional[str] = None, *, rounds: int = 5,
                 append: bool = False, session_id: Optional[str] = None,
                 images: Optional[Sequence[Union[str, Path]]] = None) -> Dict[str, Any]:
        """长任务自主推进：跑一轮，若计划里还有未完成项，就自动接力下一轮。

        `rounds` 是自动接力的**总轮数上限**（含首轮）。每轮都是一次完整的
        ReAct 循环；轮与轮之间靠三样东西自然接续：

          * 会话内的跨轮记忆（模型记得住本次接力里说过什么）；
          * 每轮重新组装的 system（含模型自己维护的 plan.md 待办）；
          * 模型自己写进文件的进度（不随上下文折叠而丢失）。

        一次接力 = 一个进程内会话：接力过程中逐轮连贯，收尾后即弃，
        不会残留到下一次单次任务。若调用前已在会话中（如 `-i` 的会话），
        则沿用该会话，接力结束后它继续存在。

        停下条件（任一）：计划已无 `- [ ]` 未完成项 / 某轮出错或被中断 /
        达到轮数上限。于是「一个长任务」会自己跑到做完，而不是跑一轮就停。

        Returns:
            最后一轮的会话文档，额外带 `auto_rounds` 与 `auto_answers` 两个字段。
        """
        rounds = max(1, int(rounds or 1))
        # 自己开的会话，接力结束就关掉；沿用已有会话（如 -i 的）时不动它。
        owns_session = not self._session_active
        if owns_session:
            self.begin_session()
        docs: List[Dict[str, Any]] = []
        try:
            for i in range(rounds):
                first = (i == 0)
                try:
                    doc = self.run(
                        user_input if first else None,
                        append=append if first else False,
                        session_id=session_id,
                        images=images if first else None,
                        session=True,
                    )
                except RuntimeError as exc:
                    self.say(f"[auto] 第 {i + 1} 轮启动失败：{exc}")
                    break
                docs.append(doc)

                status = str(doc.get("status") or "")
                open_items = self.ws.plan_open_items()
                if status in ("error", "interrupted"):
                    self.say(f"[auto] 第 {i + 1} 轮 {status}，停止自动接力。")
                    break
                # 还有未完成项，或本轮被迭代上限截断（unfinished）——都算「没做完」。
                if open_items <= 0 and status != "unfinished":
                    self.say(f"[auto] 计划已无未完成项，收工（共 {i + 1} 轮）。")
                    break
                if i + 1 >= rounds:
                    why = (f"计划还剩 {open_items} 项未完成" if open_items
                           else "上一轮被步数上限截断（unfinished）")
                    self.say(f"[auto] 已达轮数上限 {rounds}，{why}；"
                             "再跑请加大 --auto 或用 openthree 继续。")
                    break
                nxt = i + 2
                why = (f"计划还有 {open_items} 项未完成" if open_items
                       else "上一轮未跑完（unfinished）")
                self.say(f"[auto] {why} → 自动开始第 {nxt} 轮")
        finally:
            if owns_session:
                self.end_session()

        if not docs:
            return {"status": "error", "answer": "[auto] 一轮都没跑起来",
                    "auto_rounds": 0, "auto_answers": []}
        last = dict(docs[-1])
        last["auto_rounds"] = len(docs)
        last["auto_answers"] = [d.get("answer", "") for d in docs]
        return last

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
            f"计划     {self.ws.plan_file}"
            f"（未完成 {self.ws.plan_open_items()} 项 / 历史版本 "
            f"{len(self.ws.list_prompt_versions())} 份）",
            f"对话档案 {self.ws.transcript_file}"
            f"（{len(self.ws.read_turns(-1))} 轮，只增不改；--resume 可续读）",
            f"长期记忆 {self.ws.memory_file}"
            f"（{self.ws.count_memory()} 条，每轮注入最近几条；recall_memory 可检索）",
            f"会话     {'进行中' if self._session_active else '无'}"
            f"（会话内记忆 {self.turns if self.turns >= 0 else '全部'} 轮；"
            "单次任务不读盘，-i / --auto 才逐轮累积）",
            f"工具     {len(reg)} 个（内置 {len(reg.builtins)} / 自造 {len(reg.user_tools())}）",
            f"上下文   窗口 {_fmt_tokens(self.context_window_tokens)} token"
            f" → 预算 {_fmt_chars(self.context_budget_chars)} 字符"
            f"（{self.context_budget_source}；模型可用 context_budget 改）"
            + ("（已关闭自动折叠）" if self.context_budget_chars <= 0 else ""),
        ]
        for t in reg.user_tools():
            lines.append(f"         ↳ {t.name}  ← {Path(t.source_file or '').name}")
        if reg.load_errors:
            lines.append(f"⚠ 加载异常 {len(reg.load_errors)} 个：")
            for e in reg.load_errors:
                lines.append(f"         ↳ {Path(e.file).name}: {e.error}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 格式化助手
def _fmt_tokens(n: int) -> str:
    """把 token 数写成易读形式：100000000 → 100M。"""
    n = int(n)
    for unit, mult in (("G", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if n >= mult:
            val = n / mult
            return f"{val:g}{unit}"
    return str(n)


def _fmt_chars(n: int) -> str:
    """把字符数写成易读形式：100000000 → 100M。"""
    n = int(n)
    if n <= 0:
        return f"{n}"
    for unit, mult in (("亿", 100_000_000), ("万", 10_000)):
        if n >= mult:
            return f"{n / mult:g}{unit}"
    return str(n)
