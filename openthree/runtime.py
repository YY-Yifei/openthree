"""Runtime：把「一、二、三」装配起来。

启动顺序（很重要，这就是「生」的顺序）：

    道   LLM 就绪
    一   workspace.init() 铺好 .openthree/，读出系统提示词
    二   registry reload，把 functions/*.py 变成工具
    三   agent 进入 ReAct 循环
    万物 模型自己写 function / 改提示词 → 下一步生效 → 能力自生长

可观测分两层：
    * Recorder —— 落盘的真相（logs/、three-report.html），回答「发生过什么」。
    * Progress —— 终端的回声（step / 思考 / 工具 / 心跳），回答「现在在干什么」。
      后者是纯展示，CLI 不会因为模型思考而长时间沉默。

多轮对话（会话）：
    记忆的寿命 = **进程内会话**，由 Runtime 持有；磁盘上的 transcript 只是档案。
      * 单次 `openthree "任务"`：不带会话 → 从零开跑，不读任何旧对话。
      * `-i` 交互模式：开一个会话，逐轮累积、跨轮可见（`--turns N` 限制播种轮数）。
      * `--auto` 自主接力：接力过程也开一个会话（或搭 `-i` 的会话），
        于是长任务逐轮连贯，收尾后不残留、不影响后续单次任务。
      * `--resume`：**显式**把 transcript 档案里最近若干轮载入为本会话起点。
    每轮收尾仍把 (user, answer) 追加进 append-only 的 transcript.jsonl 作档案/可观测，
    但它不会被自动回读 —— 读盘只发生在 `--resume`。用户的完整原始输入因此永不丢失。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .agent import Agent, resolve_context_budget
from .builtins import BUILTIN_FUNCTIONS
from .kernel import KERNEL
from .llm import LLM, LLMConfig, load_config, parse_token_count
from .progress import Progress
from .recorder import Recorder
from .registry import Registry
from .report import write_report
from .workspace import DEFAULT_AGENT_NAME, Workspace


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
                 turns: Optional[int] = None,
                 max_rounds: Optional[int] = None,
                 name: Optional[str] = None) -> None:
        self.project_dir = Path(project_dir or Path.cwd()).resolve()
        self.quiet = quiet
        self.name_arg = (name or "").strip() or None    # --name：人类给的「命名种子」
        self.max_iterations = max_iterations          # 每轮步数；None = 交给模型（limits.json）
        self.tool_result_limit = tool_result_limit    # 同上：显式才锁，否则模型可自管
        self.max_tokens = max_tokens
        self.temperature = temperature
        # 整场最多几轮（每轮一次完整 ReAct）；None = 交给模型（limits.json），内核默认 3。
        self.max_rounds = max_rounds
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
        self._seed_identity()
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

    # ---------------------------------------------------------------- 身份
    def _seed_identity(self) -> None:
        """`--name`：把人类给的名字作为**种子**落一次盘（只作种子，不锁定）。

        已经有名字时绝不覆盖 —— 名字是智能体自己长出来的，人类只是初始投喂。
        """
        if self.name_arg and not self.ws.read_identity().get("name"):
            self.ws.write_identity(self.name_arg)

    def agent_name(self) -> Optional[str]:
        """当前名字；还没起名时返回 None。"""
        return self.ws.read_identity().get("name")

    def agent_label(self) -> str:
        """展示用标签：有名字用它，没有则 `openthree（未命名）`。"""
        return self.agent_name() or f"{DEFAULT_AGENT_NAME}（未命名）"

    def _evolution_count(self) -> int:
        """演化史条数（身份卡右栏「它长成什么样了」的硬证据）。"""
        path = self.ws.logs_dir / "evolution.jsonl"
        if not path.exists():
            return 0
        try:
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                       if line.strip())
        except Exception:  # noqa: BLE001
            return 0

    def identity_card(self) -> str:
        """终端身份卡：名字（它是谁） + 自造工具与演化次数（它长成什么样了）。"""
        user_tools = len(self.registry.user_tools())
        return (f"  {self.agent_label()}  ·  自造 {user_tools} 个工具"
                f" · {self._evolution_count()} 次演化\n"
                f"  工作区 {self.ws.dir}")

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

    def _effective_max_rounds(self) -> int:
        """读一次**生效的** max_rounds（显式 --max-rounds > limits.json > 内核默认）。

        用一个零成本 Agent 读一次边界（不跑 ReAct），读到的值与真正执行时同源。
        run() 用它给轮循环定死本场轮预算，使「模型看见的轮数 = 循环执行的轮数」。
        """
        probe = Agent(self.ws, self.registry, self.llm, self.recorder,
                      max_rounds=self.max_rounds)
        return max(1, int(probe.max_rounds))

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
            session: bool = False,
            rounds: Optional[int] = None) -> Dict[str, Any]:
        """跑一次任务 —— **一次任务 = 若干轮（round），每轮是一次完整 ReAct 循环**。

        轮与步是两层：`max_iterations` 是**一轮**的步预算，`max_rounds` 是**整场**的
        轮预算。于是「200 步」只是**一轮**的节奏，不是整场任务的硬墙 —— 一轮没跑完
        （撞步数上限还没 finish，或模型主动 `next_round`）运行期会**新开一轮接着干**，
        由轮预算兜住成本。

        单次任务的收尾条件（任一）：模型 finish（整件事做完）/ 某轮出错或被中断 /
        达到轮预算。计划里还有未完成项但本轮已正常 finish 时，**不**自动续轮 ——
        那是 `--auto`（run_auto）的语义，不是单次任务的。

        `rounds` 可显式给整场轮数上限；None = 用 `max_rounds`（显式参数 > limits.json > 内核默认）。

        user_input 为 None 时：首轮沿用磁盘上已有的「当前任务」（结构化真源
        current-task.json 的干净原文），续轮不读盘、不重复播种。images 是本轮携带
        的图片（本地路径或 http/data URL）。session 为 True（或已开会话）时，跨轮
        播种来自进程内会话；否则从零开跑，**不读磁盘**。
        """
        problem = self.check()
        if problem:
            raise RuntimeError(problem)

        # 会话归谁管：调用方显式 session=True 或已 begin_session() → 归调用方（跨调用累积）；
        # 否则本次任务自带一个**短命会话**（只为本任务的轮间连续），收尾即弃、不残留。
        owns_session = False
        if not self._session_active and not session:
            self.begin_session()
            owns_session = True

        docs: List[Dict[str, Any]] = []
        # 本场**生效轮预算**在开头就定死，之后各轮都把这个数交给 Agent —— 于是
        # 「模型看见的本场最多 M 轮」与「循环真正执行的轮数」是同一个数。
        # 显式 rounds 优先；否则读一次生效的旋钮（--max-rounds > limits.json > 内核默认）。
        budget: int = (max(1, int(rounds)) if rounds is not None
                       else self._effective_max_rounds())
        prev_summary = ""
        try:
            while True:
                first = not docs
                doc = self._run_round(
                    user_input if first else None,
                    append=append if first else False,
                    session_id=session_id,
                    images=images if first else None,
                    session=True,
                    round_index=len(docs) + 1,
                    first_round=first,
                    round_budget=budget,
                    next_round_summary=prev_summary,
                    reuse_task=(first and user_input is None),
                )
                docs.append(doc)
                prev_summary = doc.get("next_round_summary") or ""
                status = str(doc.get("status") or "")

                if status in ("error", "interrupted"):
                    break
                more = bool(doc.get("step_limit_hit")) or bool(doc.get("wants_next_round"))
                if not more:
                    break                          # 本轮正常 finish = 整件事收尾
                if len(docs) >= budget:
                    why = "撞到本轮步数上限" if doc.get("step_limit_hit") else "模型主动 next_round"
                    self.say(f"[轮] 第 {len(docs)} 轮{why}，已达轮数上限 {budget}；"
                             "再跑请加大 --max-rounds 或用 set_limits(max_rounds=…)")
                    break
                self.say(f"[轮] 第 {len(docs)} 轮未跑完（{budget} 轮预算内）→ 自动开始第 {len(docs) + 1} 轮")
        finally:
            if owns_session:
                self.end_session()

        last = dict(docs[-1])
        last["rounds_run"] = len(docs)
        last["round_answers"] = [d.get("answer", "") for d in docs]
        return last

    def _run_round(self, user_input: Optional[str] = None, *,
                   append: bool = False, session_id: Optional[str] = None,
                   images: Optional[Sequence[Union[str, Path]]] = None,
                   session: bool = False, round_index: int = 1,
                   first_round: bool = True,
                   round_budget: Optional[int] = None,
                   next_round_summary: str = "",
                   reuse_task: bool = False) -> Dict[str, Any]:
        """跑**一轮**：建 Agent → 起 ReAct → finalize，返回本轮的会话文档。

        `user_input=None` 有**两种**语义，必须显式区分：
          * `reuse_task=True` —— 显式沿用磁盘上的「当前任务」（`openthree` 无参数续跑）；
          * `reuse_task=False` —— 本轮**没有新输入**（多轮续跑）：不读盘、不重复播种。

        一次任务由若干轮组成（见 run / run_auto）；本函数只管一轮。
        `round_index` 是本轮序号；`round_budget` 是本场**生效轮预算**（Runtime 真正
        会执行的轮数，传给 Agent 后进处境，保证「模型看见的轮数 = 实际执行的轮数」）；
        `next_round_summary` 是上一轮 `next_round(summary=…)` 留下的交接说明，会播种进
        本轮 Agent 的历史开头 —— 让「交给下一轮的自己」真的能交接。
        """
        problem = self.check()
        if problem:
            raise RuntimeError(problem)

        # 这一轮是否处于会话中：显式 session=True 或此前已 begin_session()。
        if session:
            self._session_active = True

        # 图片先归置成本地绝对路径 / URL（本地文件复制进 assets 去重）
        image_refs: List[str] = []
        if images:
            image_refs = self.ws.stage_images([str(i) for i in images if str(i).strip()])
            if self.ws._last_image_error:
                self.say(f"[提示] 有图片未能归档：{self.ws._last_image_error}")

        if user_input is not None:
            task_text = user_input.strip()
            self.ws.submit(task_text, append=append, images=image_refs or None)
        elif reuse_task:
            # 显式沿用磁盘上的「当前任务」：只读**结构化真源**（干净原文），
            # 绝不读 user-prompts.md 的 markdown 渲染层（含头尾 / 累积块）。
            task_text = self.ws.current_task_text()
            if image_refs:
                self.ws.write_current_task(task_text, images=image_refs)
            else:
                image_refs = self.ws.current_task_images()
        else:
            # 多轮续跑：本轮**没有新输入** —— 不读盘、不重复播种（同一用户输入只播种一次）
            task_text = ""
            image_refs = []

        # 本轮用户输入 = 干净原文（不含 markdown 头尾 / 图片标记）
        self.user_input = task_text
        self.user_text = task_text

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
                           progress=progress,
                           max_rounds=self.max_rounds,
                           round_index=round_index,
                           round_budget=round_budget,
                           next_round_summary=next_round_summary)
        self.agent.current_images = list(image_refs)
        self.agent.task_text = self.user_text
        KERNEL.agent = self.agent

        # 首轮开头报一次「手上有多少工具」；续轮只交代「现在是第几轮」。
        if first_round:
            self.say(f"{len(self.registry)} 个工具"
                     f"（内置 {len(self.registry.builtins)} / 自造 {len(self.registry.user_tools())}）")
        self.say(f"{'ReAct 循环开始' if first_round else f'第 {round_index} 轮开始'}"
                 f"（每轮 ≤ {self.agent.max_iterations} 步 × 最多 {self.agent.max_rounds} 轮，"
                 f"模型 {self.config.model}）")
        if first_round and image_refs:
            self.say(f"       本轮输入含 {len(image_refs)} 张图片（多模态）")
        self.say(f"       上下文窗口 {_fmt_tokens(self.context_window_tokens)} token"
                 f" → 预算 {_fmt_chars(self.agent.context_budget_chars)} 字符"
                 f"（来源：{self.agent.context_budget_source()}；模型可用 context_budget 改）")
        self.say(f"       运行边界：每轮 ≤ {self.agent.max_iterations} 步、最多 {self.agent.max_rounds} 轮、"
                 f"生成 ≤ {self.agent.max_tokens} token、工具结果 ≤ {self.agent.tool_result_limit} 字符"
                 f"（模型可用 set_limits 改）")
        if first_round and self.heartbeat > 0 and not self.quiet:
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
        doc["round"] = self.agent.round_index
        doc["max_rounds"] = self.agent.max_rounds
        doc["step_limit_hit"] = self.agent.step_limit_hit
        doc["wants_next_round"] = self.agent.wants_next_round
        # 本轮给下一轮的交接说明（由 Runtime 带到下一轮，见 _run_round 的入参）。
        doc["next_round_summary"] = self.agent.next_round_summary

        # 本轮对话入档：用户的完整原始输入 append 进 transcript，永不丢失。
        # **一次任务只入档一次**：多轮任务里同一用户输入绝不被重复写 N 次。
        # 续轮（没有新输入）只记本轮结论。
        try:
            if first_round:
                self.ws.append_turn(self.user_text, answer, images=image_refs or None)
            else:
                self.ws.append_turn("", answer)
        except Exception as exc:  # noqa: BLE001
            if self.recorder:
                self.recorder.record("error", summary="对话记录写入失败",
                                     data={"error": f"{type(exc).__name__}: {exc}"})

        # 会话内累积本轮 (用户原文, 结论)，供同一会话的后续轮次播种。
        # 没有会话（单次任务）时不累积，于是收尾即忘、不留痕。
        # 续轮没有新输入：只累积结论，不再把同一条任务重复塞进记忆。
        if session or self._session_active:
            self._session_turns.append({
                "user": self.user_text if first_round else "",
                "answer": answer or "",
                "images": list(image_refs) if first_round else [],
            })

        # 每轮结束都刷新报告 —— 可观测是常态，不是事后
        try:
            write_report(self.ws)
        except Exception as exc:  # noqa: BLE001
            if self.recorder:
                self.recorder.record("error", summary="报告生成失败",
                                     data={"error": f"{type(exc).__name__}: {exc}"})

        stats = doc["stats"]
        self.say(f"第 {self.agent.round_index} 轮完成：{stats['steps']} 步 / "
                 f"{stats['tool_calls']} 工具调用 / {stats['evolutions']} 次演化"
                 + ("（本轮撞步数上限，未收尾）" if self.agent.step_limit_hit else ""))
        self.say(f"报告 {self.ws.report_file}")
        self.say(f"记录 {self.recorder.session_file}")
        return doc

    def run_auto(self, user_input: Optional[str] = None, *, rounds: int = 5,
                 append: bool = False, session_id: Optional[str] = None,
                 images: Optional[Sequence[Union[str, Path]]] = None) -> Dict[str, Any]:
        """长任务自主推进：跑一轮，若计划里还有未完成项，就自动接力下一轮。

        `rounds` 是自动接力的**总轮数上限**（含首轮）；若人类另用 `--max-rounds`
        显式锁定了整场轮数，则取二者较小者（两个数都在说同一件事：整场最多几轮）。
        实际执行 `min(rounds, 显式 max_rounds)` 轮。每轮都是一次完整的 ReAct 循环；
        轮与轮之间靠三样东西自然接续：

          * 会话内的跨轮记忆（模型记得住接力里说过什么）；
          * 每轮重新组装的 system（含模型自己维护的 plan.md 待办）；
          * 模型自己写进文件的进度（不随上下文折叠而丢失）。

        一次接力 = 一个进程内会话：接力过程中逐轮连贯，收尾后即弃，
        不会残留到下一次单次任务。若调用前已在会话中（如 `-i` 的会话），
        则沿用该会话，接力结束后它继续存在。

        停下条件（任一）：计划已无 `- [ ]` 未完成项且本轮已正常收尾 / 某轮出错或被中断 /
        达到轮数上限。于是「一个长任务」会自己跑到做完，而不是跑一轮就停。

        **计划未完成项只以「接力期间写过的计划」为准**：计划是单槽位的
        `plan.md`，磁盘上残留的 `- [ ]` 未必属于这次接力，不该逼着
        接力跑满轮数。判据是接力期间 `plan.md` 的文本变过没有。

        Returns:
            最后一轮的会话文档，额外带：
              * `auto_rounds` / `auto_answers` —— 实际轮数与逐轮结论；
              * `auto_open_items` —— 收尾时接力写过的计划里剩余的未完成项数；
              * `auto_unfinished` —— 是否因**撞到轮数上限而仍有活**（CI 可据此判非 0）。
        """
        rounds = max(1, int(rounds or 1))
        # 只有人类**显式**给了 --max-rounds 才把它当硬顶；否则 --auto N 就是 N 轮
        # （默认旋钮值不该悄悄把 --auto 5 截成 3）。生效轮数随后交给 Agent 进处境。
        if self.max_rounds is not None:
            rounds = min(rounds, max(1, int(self.max_rounds)))
        # 自己开的会话，接力结束就关掉；沿用已有会话（如 -i 的）时不动它。
        owns_session = not self._session_active
        if owns_session:
            self.begin_session()
        docs: List[Dict[str, Any]] = []
        prev_summary = ""
        # 磁盘残留的 `- [ ]` 不该拖满轮数。只有接力**动过** plan.md
        # 时，未完成项才算「接力还没做完」；开局就摆在那里的勾选项不属于这次接力。
        plan_at_start = self.ws.read_plan()
        plan_touched = False
        plan_left = 0
        # 是否因撞到轮数上限而停下（此时若仍有活，就是「没做完」）。
        hit_limit = False
        try:
            for i in range(rounds):
                first = (i == 0)
                try:
                    doc = self._run_round(
                        user_input if first else None,
                        append=append if first else False,
                        session_id=session_id,
                        images=images if first else None,
                        session=True,
                        round_index=i + 1,
                        first_round=first,
                        round_budget=rounds,
                        next_round_summary=prev_summary,
                        reuse_task=(first and user_input is None),
                    )
                except RuntimeError as exc:
                    self.say(f"[auto] 第 {i + 1} 轮启动失败：{exc}")
                    break
                docs.append(doc)
                prev_summary = doc.get("next_round_summary") or ""
                if self.ws.read_plan() != plan_at_start:
                    plan_touched = True          # 接力写过计划 → 未完成项与该任务相关

                status = str(doc.get("status") or "")
                open_items = self.ws.plan_open_items()
                if status in ("error", "interrupted"):
                    self.say(f"[auto] 第 {i + 1} 轮 {status}，停止自动接力。")
                    break
                # 未完成项只以「接力期间写过的计划」为准（否则是磁盘上的残留）；
                # 本轮撞了步数上限、或模型主动 next_round —— 这两样本就说明「没做完」。
                plan_left = open_items if plan_touched else 0
                unfinished = (plan_left > 0 or bool(doc.get("step_limit_hit"))
                              or bool(doc.get("wants_next_round")))
                if not unfinished:
                    self.say(f"[auto] 计划已无未完成项，收工（共 {i + 1} 轮）。")
                    break
                if i + 1 >= rounds:
                    hit_limit = True
                    why = (f"计划还剩 {plan_left} 项未完成" if plan_left
                           else ("上一轮撞到步数上限" if doc.get("step_limit_hit")
                                 else "上一轮主动 next_round"))
                    self.say(f"[auto] 已达轮数上限 {rounds}，{why}；"
                             "再跑请加大 --auto 或用 openthree 继续。")
                    break
                nxt = i + 2
                why = (f"计划还有 {plan_left} 项未完成" if plan_left
                       else ("上一轮撞到步数上限" if doc.get("step_limit_hit")
                             else "上一轮主动 next_round"))
                self.say(f"[auto] {why} → 自动开始第 {nxt} 轮")
        finally:
            if owns_session:
                self.end_session()

        if not docs:
            return {"status": "error", "answer": "[auto] 一轮都没跑起来",
                    "auto_rounds": 0, "auto_answers": [],
                    "auto_open_items": 0, "auto_unfinished": True}
        last = dict(docs[-1])
        last["auto_rounds"] = len(docs)
        last["auto_answers"] = [d.get("answer", "") for d in docs]
        # 撞到轮数上限时仍有活 = 「没做完」；CLI 据此返回非 0 退出码。
        last["auto_open_items"] = plan_left
        last["auto_unfinished"] = bool(hit_limit)
        return last

    def report(self) -> Path:
        return write_report(self.ws)

    # ---------------------------------------------------------------- 自省
    def self_inspect(self) -> str:
        """不需要模型的一句话自省摘要。"""
        reg = self.registry
        lines = [
            f"智能体   {self.agent_label()}"
            + (f"（set_identity 维护；改名 {self.ws.read_identity().get('renames') or 0} 次）"
               if self.agent_name() else "（用 set_identity 给自己起名）"),
            f"工作区   {self.ws.dir}",
            f"系统提示词 {self.ws.system_prompt_file} ({len(self.ws.read_system_prompt())} 字符)",
            f"任务     {self.ws.user_prompt_file}",
            f"计划     {self.ws.plan_file}"
            f"（未完成 {self.ws.plan_open_items()} 项 / 历史版本 "
            f"{len(self.ws.list_prompt_versions())} 份）",
            f"对话档案 {self.ws.transcript_file}"
            f"（{len(self.ws.read_turns(-1))} 轮对话，只增不改；--resume 可续读）",
            f"长期记忆 {self.ws.memory_file}"
            f"（{self.ws.count_memory()} 条，每步注入最近几条；recall_memory 可检索）",
            f"会话     {'进行中' if self._session_active else '无'}"
            f"（会话内记忆 {self.turns if self.turns >= 0 else '全部'} 轮对话；"
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
