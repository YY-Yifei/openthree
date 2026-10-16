"""Agent：「三」—— ReAct 循环。

    观（observe）→ 思（LLM）→ 行（tool）→ 观（结果）→ … → finish

极简的坚持：
  * 没有任何硬编码的智能。循环里没有 if「用户想干什么」的分支。
  * 每一步都重新组装 system（prompt.build_messages），
    所以模型改提示词 / 造工具 / 压缩上下文，下一步立刻生效 —— 这就是演化。
  * 一切所见所行都进 Recorder。

步 / 轮 / 会话（三层，别混）：
  * **步（step）** = ReAct 的一次「思 → 行」，由 `run()` 内的 while 循环推进。
  * **轮（round）** = 一次完整的 ReAct 循环 = 本 Agent 的一次 `run()`；
    **每轮有独立的一份步数预算**（`max_iterations` 只管一轮）。撞上限未 finish 时，
    Runtime 会视情况新建一个 Agent 续下一轮，由 `max_rounds` 兜住整场成本。
  * **会话（session）** = 进程内的多轮容器（-i / --auto），跨轮记忆长在这里。

工程加固（不改变设计）：
  * 工具结果进 history 前统一截断，超长输出落盘，history 只留预览 + 路径，
    避免同一份大输出被每步重发。
  * 上下文预算：超过阈值时自动把最老的若干 step 折叠成摘要，
    不指望模型自觉调用 compact_context。
  * 运行边界：每轮步数上限 / max_tokens / tool_result_limit / temperature / 最多轮数
    默认由内核给，但模型可用 set_limits 写进 .openthree/limits.json 自管（tick 每步
    重读）；显式启动参数与 OPENTHREE_LIMITS_LOCK 是它绕不过的两层。与「自管上下文」同源同构。
  * finish 提交后，同批剩余 tool_calls 一律跳过。
  * compact_context 的区间先在现有 history 上校验，如实返回成功/失败。
  * 进度回声：把「正在做什么」通过注入的 progress 打出去，CLI 不会长时间沉默。
    这是纯展示层，不参与决策；不注入时走 NullProgress，静默。
  * 跨轮记忆：会话里「之前几轮」由 Runtime 显式传入（seed_turns），以
    user/assistant 消息播种进 history，于是多轮会话里模型记得住用户说过什么。
    **Agent 自己不读磁盘** —— 没有会话（如单次任务）时 seed_turns 为空，
    历史就是空的，不会莫名背上别的对话；会话记忆只活在进程内
    （见 runtime.begin_session / workspace.append_turn）。
    system 依然每一步重组装，播种只发生在消息层，演化机制不受影响。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .kernel import KERNEL
from .llm import (CHARS_PER_TOKEN, DEFAULT_CONTEXT_WINDOW_TOKENS, LLM,
                  parse_token_count)
from .multimodal import (DEFAULT_DETAIL, ImageResult, build_content,
                         content_text, count_images, estimate_content_chars)
from .progress import NullProgress
from .prompt import build_messages
from .recorder import Recorder
from .registry import Registry
from .workspace import Workspace

# 单个工具结果进入 history 的字符上限；超出则落盘、history 留预览。
TOOL_RESULT_LIMIT = 4000
# 上下文预算（历史累计字符数）默认由「模型上下文窗口」推导，见 resolve_context_budget。
# 预算 ≈ min(窗口 token 数, MAX_CONTEXT_BUDGET_CHARS)：窗口越大给越多，但兜个上限。
#   窗口 = 1M token（默认，见 llm.DEFAULT_CONTEXT_WINDOW_TOKENS）→ 预算 = 1_000_000 字符。
# 预算的硬上限（= 默认窗口的推导值）：窗口更大时不再线性放大，避免阈值离谱。
# 想放开就调大这里，或用 --context-budget 显式给。
MAX_CONTEXT_BUDGET_CHARS = 1_000_000
# 历史至多占用上下文窗口的比例（留一半给 system/工具 schema/模型输出）。
CONTEXT_BUDGET_RATIO = 0.5

# 自动折叠的「硬闸」：设成 0/false/off 可在启动时彻底冻结自动折叠（模型改不了环境，
# 所以这是真·不可被模型绕过的开关；不设则由策略文件说了算）。
CONTEXT_AUTOFOLD_ENV = "OPENTHREE_CONTEXT_AUTOFOLD"

# 「运行边界」的硬闸：环境变量非空且为真值时，limits.json 里的一切旋钮都被冻结
# （模型改不动）。这是它绕不过的一层 —— 与 OPENTHREE_CONTEXT_AUTOFOLD 对称。
LIMITS_LOCK_ENV = "OPENTHREE_LIMITS_LOCK"

# 「轮」的内核默认上限：一次任务最多自动接力几轮。
#
# 先把「步」与「轮」两个层级分清楚（见 prompt.py）：
#   * **步（step）** = ReAct 的一次「思 → 行」。
#   * **轮（round）** = 一次完整的 ReAct 循环 = 一个 Agent 的生命周期。
#     **每轮拥有独立的一份 step 预算**（新 Agent → 新 max_iterations）。
#   * **会话（session）** = 进程内的多轮容器（-i / --auto）。
#
# 于是「200 步」只是**一轮**的预算，不是整场任务的硬墙：还没做完（撞步数上限
# 未 finish / 模型自己调了 next_round / 计划还有未完成项）就自动开下一轮，
# 由 max_rounds 兜住总成本（默认 3 轮 ≈ 600 步）。模型可 set_limits 改它，
# 人类可用 --max-rounds 锁定。
DEFAULT_MAX_ROUNDS = 3


def resolve_context_budget(context_budget_chars: Optional[int],
                           context_window_tokens: Optional[int]) -> int:
    """算出本轮的历史字符预算。

    优先用显式传入的 `context_budget_chars`（0 = 关闭自动折叠）；否则按模型
    上下文窗口推导：budget = min(窗口 token 数, MAX_CONTEXT_BUDGET_CHARS)。
    推导用到 CHARS_PER_TOKEN（1 token ≈ 2 字符）与 RATIO（历史至多占用窗口的一半），
    两者相乘、相消后「窗口 token 数」恰好等值于「字符预算」。

    Args:
        context_budget_chars: 显式预算（字符）；None 表示按窗口推导，0 表示关闭。
        context_window_tokens: 模型上下文窗口（token）；None 表示用默认窗口。
    """
    if context_budget_chars is not None:
        return int(context_budget_chars)
    window = int(context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS)
    derived = int(window * CHARS_PER_TOKEN * CONTEXT_BUDGET_RATIO)
    return int(min(derived, MAX_CONTEXT_BUDGET_CHARS))


def resolve_context_policy(budget: Optional[int] = None,
                           autofold: Optional[bool] = None, *,
                           window_tokens: Optional[int] = None,
                           explicit: Optional[int] = None,
                           env_value: Optional[str] = None) -> Tuple[int, bool]:
    """把「显式参数 / 策略文件 / 模型窗口 / 环境变量」折叠成一对 (预算, 是否自动折叠)。

    优先级（越靠前越硬，模型改不了前面的）：
      1. `env_value`（环境变量 OPENTHREE_CONTEXT_AUTOFOLD）：非空且为 0/false/off/no
         → 自动折叠**彻底冻结**（预算仍可被模型调，只是内核不再兜底折叠）。
      2. `explicit`（--context-budget）：显式启动参数一旦给出，它就是预算的上限/基调。
      3. `budget`（策略文件里的值）：模型用 context_budget 写下的预算。
      4. 窗口推导（window_tokens）：都没有时按模型窗口算。
      0 = 关闭自动折叠（预算为 0 时自动折叠不再触发）。

    返回 (budget_chars, allow_autofold)；两者都可被模型在运行期改写（策略文件），
    但环境变量那一层才是真正意义上的「硬闸」。
    """
    if explicit is not None:
        base = int(explicit)
    elif budget is not None:
        base = int(budget)
    else:
        base = resolve_context_budget(None, window_tokens)
    allow = base > 0                      # 预算为 0 时，内核不自动折叠
    if autofold is not None:
        allow = bool(autofold) and base > 0
    env = (env_value if env_value is not None
           else os.environ.get(CONTEXT_AUTOFOLD_ENV))
    if env is not None and str(env).strip() != "":
        if str(env).strip().lower() in ("0", "false", "off", "no"):
            allow = False
    return base, allow


class Agent:
    def __init__(self, workspace: Workspace, registry: Registry, llm: LLM,
                 recorder: Recorder, *, max_iterations: Optional[int] = None,
                 tool_result_limit: Optional[int] = None,
                 max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None,
                 context_budget_chars: Optional[int] = None,
                 seed_turns: Optional[Sequence[Dict[str, Any]]] = None,
                 progress: Optional[Any] = None,
                 max_truncations: int = 2,
                 max_rounds: Optional[int] = None,
                 round_index: int = 1,
                 round_budget: Optional[int] = None,
                 next_round_summary: str = "") -> None:
        self.ws = workspace
        self.reg = registry
        self.llm = llm
        self.rec = recorder
        # 运行边界：显式传入即「上锁」（模型改不动）；None = 交给模型/内核默认。
        # 与上下文预算同一套哲学：模型能读、能改（limits.json），除非人类显式锁定。
        self._explicit_max_iterations = max_iterations
        self._explicit_tool_result_limit = tool_result_limit
        self._explicit_max_tokens = max_tokens
        self._explicit_temperature = temperature
        self._explicit_max_rounds = max_rounds
        self._limits_env = os.environ.get(LIMITS_LOCK_ENV)
        # 生成参数的「内核默认」基线：**构造时快照一次，之后只读**。
        # 一旦让 apply_limits 原地改 llm.config，而「默认值」又取自 llm.config，
        # 模型调大一次后，跨轮（-i / --auto 复用同一个 LLM）「内核默认」就会被
        # 它自己的值污染 —— 基线必须与「模型能改的运行时值」分开。
        self._baseline_max_tokens = _as_int(
            getattr(getattr(self.llm, "config", None), "max_tokens", None), 8192, lo=1)
        self._baseline_temperature = _as_float(
            getattr(getattr(self.llm, "config", None), "temperature", None),
            0.3, lo=0.0, hi=2.0)
        self._limits_file_data: Dict[str, Any] = {}
        self._reload_limits_file()
        # 「轮」也是一条运行边界（第五个旋钮）：本 Agent 是第几轮、最多接力几轮。
        # 步预算（max_iterations）属于**一轮**，轮预算（max_rounds）属于**整场任务**。
        self.round_index = max(1, int(round_index))
        # 本场的**生效轮预算**：Runtime 逐轮驱动时显式传入（run 的 budget / run_auto 的
        # rounds），此时它就是 `max_rounds`，且**整场固定**（tick 不被 limits.json 改写）。
        # 单独构造（测试 / 单轮）时传 None → 走旋钮默认。
        # 只有一个真相：`self.max_rounds` 同时进「处境轮读数」与「运行边界读数」。
        self._round_budget_fixed = round_budget is not None
        (self.max_iterations, self.tool_result_limit, self.max_tokens,
         self.temperature, self.max_rounds) = self._compute_limits()
        if round_budget is not None:
            self.max_rounds = max(1, int(round_budget))
        # autonomous：finish / next_round 之后还会不会有人接着跑 ——
        # 即本 Agent 是否处在「运行期逐轮驱动」里（Runtime 显式给了轮预算）。
        # 单次任务（自带短命会话续轮）同样有人接着跑，故也为 True。
        self.autonomous = self._round_budget_fixed
        # 本轮用户输入携带的图片（绝对路径或 http/data URL）。
        # 在第一步注入成一条独立的 user 消息（与任务文本配对），供跨轮还原。
        self.current_images: List[str] = []
        self.task_text: str = ""
        self._images_injected = False
        # 上下文策略：预算与「是否自动折叠」。这是**模型可自管**的旋钮 ——
        # 来源优先级见 resolve_context_policy；模型用 context_budget 工具改策略文件，
        # tick() 每步重读，于是「改一下，下一步就生效」。
        # 显式 --context-budget 是启动基调（模型不能越过它），策略文件里的预算受它夹取。
        self._explicit_budget = context_budget_chars
        self._env_autofold = os.environ.get(CONTEXT_AUTOFOLD_ENV)
        budget, autofold = self._read_context_policy()
        self.context_budget_chars = budget
        self._auto_fold_enabled = autofold
        # 跨轮记忆：本会话「之前几轮」的对话，由 Runtime 显式传入。
        # Agent 不读盘 —— 没有会话（单次任务）时这里就是空的，从零开跑。
        self.seed_turns: List[Dict[str, Any]] = [
            dict(t) for t in (seed_turns or []) if isinstance(t, dict)]
        # 进度回声：只用来「说话」，不参与任何判断
        self.progress = progress if progress is not None else NullProgress()

        self.history: List[Dict[str, Any]] = self._seed_history()
        self.answer: Optional[str] = None
        self.finished = False
        self.iteration = 0
        self.last_action = ""
        # 撞到本轮的步数上限（没 finish 就退出循环）—— Runtime 据此决定是否续轮。
        self.step_limit_hit = False
        # 模型用 next_round 显式宣告「这一阶段做完了，开新一轮」。
        self.wants_next_round = False
        # 上一轮 next_round(summary=…) 的交接说明：由 Runtime 跨轮带来，
        # 播种进本轮历史开头 —— 否则那句「交给下一轮的自己」只会写进日志，
        # 下一轮其实收不到（next_round 的 docstring 却承诺了交接）。
        self.next_round_summary = (next_round_summary or "").strip()
        self._next_round_injected = False
        self._compact_marks: List[Tuple[int, int, str]] = []
        # 累计发生过多少次折叠（模型折叠 + 内核兜底），供 context_usage 读数
        self._compaction_count = 0
        # 输出被 max_tokens 截断时的重发次数（截断可恢复，不该杀死整场任务）
        self.max_truncations = max(0, int(max_truncations))
        self.truncations = 0

    # ---------------------------------------------------------------- 上下文策略
    def _read_context_policy(self) -> Tuple[int, bool]:
        """读取「上下文策略」：`.openthree/context.json`（模型可写）+ 环境硬闸。

        文件缺失/损坏时安静回退到「按模型窗口推导、自动折叠开启」—— 自管不是
        必需品，坏了也不能把循环拖死。返回 (预算字符, 是否自动折叠)。
        """
        budget: Optional[int] = None
        autofold: Optional[bool] = None
        try:
            fp = self.ws.context_policy_file
            if fp.exists():
                data = json.loads(fp.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict):
                    if data.get("budget_chars") is not None:
                        budget = parse_token_count(data.get("budget_chars"))
                    if data.get("auto_fold") is not None:
                        autofold = bool(data.get("auto_fold"))
        except Exception:  # noqa: BLE001
            budget, autofold = None, None
        window = getattr(getattr(self.llm, "config", None), "context_window_tokens", None)
        return resolve_context_policy(
            budget, autofold, window_tokens=window,
            explicit=self._explicit_budget, env_value=self._env_autofold)

    def context_budget_source(self) -> str:
        """预算的来源说明（给人看，也可回显）—— 让「这预算谁定的」一目了然。"""
        if self._explicit_budget is not None:
            return "显式 --context-budget（模型不能越过）"
        try:
            fp = self.ws.context_policy_file
            if fp.exists():
                data = json.loads(fp.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict) and data.get("budget_chars") is not None:
                    return "策略文件 .openthree/context.json（模型可改）"
        except Exception:  # noqa: BLE001
            pass
        win = getattr(getattr(self.llm, "config", None), "context_window_tokens", None)
        return f"按窗口 {win} token 推导"

    def tick(self) -> None:
        """每步开头重读「上下文策略」与「运行边界」—— 让「模型改了旋钮」下一步就生效。

        与提示词/工具一样「每步重组装」：改了就下一步生效，不做缓存。
        """
        budget, autofold = self._read_context_policy()
        if budget != self.context_budget_chars:
            self.rec.record("evolution",
                            summary=f"上下文预算改为 {budget} 字符（上一步 {self.context_budget_chars}）",
                            data={"action": "context_budget_changed",
                                  "from": self.context_budget_chars, "to": budget})
            self.progress.note(f"上下文预算改为 {budget} 字符")
        if autofold != self._auto_fold_enabled:
            self.rec.record("evolution",
                            summary=f"自动折叠{'开启' if autofold else '关闭'}",
                            data={"action": "auto_fold_toggled", "auto_fold": autofold})
            self.progress.note(f"自动折叠{'开启' if autofold else '关闭'}")
        self.context_budget_chars = budget
        self._auto_fold_enabled = autofold
        self.apply_limits()               # 运行边界同样每步重读、改一下下一步生效

    def set_context_budget(self, budget_chars: Optional[int] = None,
                           auto_fold: Optional[bool] = None) -> Tuple[bool, str]:
        """写下上下文策略（模型自管入口）。返回 (是否成功, 说明)。

        与 `set_limits` 同一套契约：启动参数显式锁定的旋钮（`--context-budget`）
        模型改不动 —— 此时**诚实拒绝**，而不是写下去却无效（那会让模型以为改成了）。
        自动折叠仍可单独调（`--context-budget` 锁的是预算，不是开关）。
        """
        path = self.ws.context_policy_file
        if budget_chars is None and auto_fold is None:
            return False, "[错误] 至少给 budget_chars 或 auto_fold 之一"
        if budget_chars is not None and self._explicit_budget is not None:
            return False, ("[错误] 上下文预算已由启动参数 --context-budget 显式锁定，"
                           "模型改不动（自动折叠仍可用 auto_fold 单独调）")
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:  # noqa: BLE001
                data = {}
        if budget_chars is not None:
            if int(budget_chars) < 0:
                return False, "[错误] budget_chars 不能为负（0 = 关闭自动折叠）"
            data["budget_chars"] = int(budget_chars)
        if auto_fold is not None:
            data["auto_fold"] = bool(auto_fold)
        try:
            self.ws.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return False, f"[错误] 无法写入 {path}：{exc}"
        self.tick()
        return True, (f"[ok] 已更新上下文策略：预算 {self.context_budget_chars} 字符、"
                      f"自动折叠{'开' if self._auto_fold_enabled else '关'}")

    # ---------------------------------------------------------------- 运行边界
    def _reload_limits_file(self) -> None:
        """读 `.openthree/limits.json`（模型可写）到 `self._limits_file_data`。

        文件缺失/损坏时安静回退到空 dict（= 全部走内核默认）—— 自管不是必需品，
        坏了也不能把循环拖死。只认已知的五个键。
        """
        data: Dict[str, Any] = {}
        try:
            fp = self.ws.limits_file
            if fp.exists():
                loaded = json.loads(fp.read_text(encoding="utf-8") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
        except Exception:  # noqa: BLE001
            data = {}
        self._limits_file_data = data

    def limits_locked(self) -> bool:
        """环境硬闸 OPENTHREE_LIMITS_LOCK 是否把「运行边界」整体冻结。"""
        env = self._limits_env
        return (env is not None and str(env).strip() != ""
                and str(env).strip().lower() not in ("0", "false", "off", "no"))

    def _compute_limits(self) -> Tuple[int, int, int, float, int]:
        """把「显式参数 / 策略文件 / 内核默认」折叠成五个边界值。

        优先级（越靠前越硬）：显式参数 > 策略文件 > 内核默认。
        环境硬闸开启时，策略文件整段被忽略 —— 只有显式参数与内核默认说话。
        返回 (max_iterations, tool_result_limit, max_tokens, temperature, max_rounds)。
        """
        fd = {} if self.limits_locked() else self._limits_file_data

        def _pick(explicit: Any, key: str, base: Any, coerce) -> Any:
            """显式参数 > 内核默认（基线）> 策略文件。

            两条铁律：
              * **基线不可写**。`base` 是构造时从 `llm.config` 快照一次的只读内核
                默认，绝不回写 `llm.config` —— 否则原地改 config、又拿 config 当
                默认值，跨轮（`-i` / `--auto` 复用同一个 LLM）就会把它自己的值当成
                「内核默认」。基准与运行时值必须分开。
              * **文件值才夹取**。`limits.json` 是模型/人能随手改的磁盘状态，坏值要
                回退、越界要夹到合法区间，不能让循环崩、也不能把非法值直通模型。
                显式参数是人类的**明确意图**（且 `set_limits` 根本改不动它），故原样
                采信、不夹取 —— 与 `--context-budget` 的语义一致。
            """
            if explicit is not None:
                return explicit
            raw = fd.get(key)
            if raw is not None:
                return coerce(raw)
            return base

        mi = _pick(self._explicit_max_iterations, "max_iterations", 200,
                   lambda v: _clamp_int(v, 200, lo=1))
        tr = _pick(self._explicit_tool_result_limit, "tool_result_limit",
                   TOOL_RESULT_LIMIT, lambda v: _clamp_int(v, TOOL_RESULT_LIMIT, lo=0))
        mt = _pick(self._explicit_max_tokens, "max_tokens",
                   self._baseline_max_tokens,
                   lambda v: _clamp_int(v, self._baseline_max_tokens, lo=1))
        tp = _pick(self._explicit_temperature, "temperature",
                   self._baseline_temperature,
                   lambda v: _clamp_float(v, self._baseline_temperature, lo=0.0, hi=2.0))
        mr = _pick(self._explicit_max_rounds, "max_rounds", DEFAULT_MAX_ROUNDS,
                   lambda v: _clamp_int(v, DEFAULT_MAX_ROUNDS, lo=1))
        return int(mi), int(tr), int(mt), float(tp), int(mr)

    def apply_limits(self) -> None:
        """重读「运行边界」并应用 —— 供 tick() 每步调用（改一下下一步生效）。"""
        self._reload_limits_file()
        mi, tr, mt, tp, mr = self._compute_limits()
        # 本场轮预算一旦由 Runtime 定死（逐轮驱动），tick 就不再用 limits.json 改写它 ——
        # 否则模型在某一轮里 set_limits(max_rounds=N) 会让「处境说的本场最多几轮」
        # 与「循环真正执行的轮数」错位（同一句话里出现两个数）。其余四个旋钮照常。
        if self._round_budget_fixed:
            mr = self.max_rounds
        changed = (mi != self.max_iterations or tr != self.tool_result_limit
                   or mt != self.max_tokens or tp != self.temperature
                   or mr != self.max_rounds)
        if changed:
            self.rec.record(
                "evolution",
                summary=(f"运行边界更新：步数上限 {mi}、生成上限 {mt}、"
                         f"工具结果上限 {tr} 字符、最多 {mr} 轮"),
                data={"action": "limits_changed", "max_iterations": mi,
                      "max_tokens": mt, "tool_result_limit": tr,
                      "temperature": tp, "max_rounds": mr})
            self.progress.note(f"运行边界更新：步数上限 {mi}、生成上限 {mt}、最多 {mr} 轮")
        self.max_iterations, self.tool_result_limit = mi, tr
        self.max_tokens, self.temperature = mt, tp
        self.max_rounds = mr

    def limits_stats(self) -> Dict[str, Any]:
        """把「运行边界」的读数交给模型 —— 自管的前提是看得见。"""
        locked = self.limits_locked()
        fd = {} if locked else self._limits_file_data
        # 文件里给了值、却解析不出数字的键（null = 有意交给内核默认，不算坏值）：
        # 它们实际走的是内核默认，标签如实写出来，免得「读数说模型自管、实际是默认」。
        bad = {k for k in ("max_iterations", "tool_result_limit", "max_tokens",
                           "temperature", "max_rounds")
               if fd.get(k) is not None and not _numeric(fd.get(k))}
        source: Dict[str, str] = {}
        for key, explicit in (("max_iterations", self._explicit_max_iterations),
                              ("tool_result_limit", self._explicit_tool_result_limit),
                              ("max_tokens", self._explicit_max_tokens),
                              ("temperature", self._explicit_temperature),
                              ("max_rounds", self._explicit_max_rounds)):
            if explicit is not None:
                source[key] = "显式（锁定）"
            elif fd.get(key) is not None and key not in bad:
                source[key] = "模型自管"
            elif key in bad:
                source[key] = "内核默认（文件里的值无效）"
            else:
                source[key] = "内核默认"
        return {
            "max_iterations": self.max_iterations,
            "max_tokens": self.max_tokens,
            "tool_result_limit": self.tool_result_limit,
            "temperature": self.temperature,
            "max_rounds": self.max_rounds,
            "source": source,
            "locked": locked,
        }

    def set_limits(self, max_iterations: Optional[int] = None,
                   max_tokens: Optional[int] = None,
                   tool_result_limit: Optional[int] = None,
                   temperature: Optional[float] = None,
                   max_rounds: Optional[int] = None) -> Tuple[bool, str]:
        """写下「运行边界」（模型自管入口）。返回 (是否成功, 说明)。

        - 环境硬闸（OPENTHREE_LIMITS_LOCK）开启时拒绝写入：那是模型绕不过的一层。
        - 被启动参数显式锁定的旋钮拒绝写入：人类锁定后模型不许改。
        - 其余写进 limits.json，`apply_limits()` 让它下一步生效。
        """
        given = [(k, v) for k, v in (("max_iterations", max_iterations),
                                     ("max_tokens", max_tokens),
                                     ("tool_result_limit", tool_result_limit),
                                     ("temperature", temperature),
                                     ("max_rounds", max_rounds))
                 if v is not None]
        if not given:
            return False, ("[错误] 至少给一个边界（max_iterations / max_tokens / "
                           "tool_result_limit / temperature / max_rounds）")
        if self.limits_locked():
            return False, (f"[错误] 运行边界被环境变量 {LIMITS_LOCK_ENV} 冻结，"
                           f"模型改不动（这是硬闸）")
        explicit = {"max_iterations": self._explicit_max_iterations,
                    "max_tokens": self._explicit_max_tokens,
                    "tool_result_limit": self._explicit_tool_result_limit,
                    "temperature": self._explicit_temperature,
                    "max_rounds": self._explicit_max_rounds}
        blocked = [k for k, _ in given if explicit.get(k) is not None]
        if blocked:
            return False, f"[错误] {'、'.join(blocked)} 已由启动参数显式锁定，模型改不动"

        data: Dict[str, Any] = {}
        path = self.ws.limits_file
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:  # noqa: BLE001
                data = {}
        for k, v in given:
            if k == "temperature":
                fv = float(v)
                if not (0.0 <= fv <= 2.0):
                    return False, "[错误] temperature 需在 0~2 之间"
                data[k] = fv
            else:
                iv = int(v)
                # max_iterations / max_rounds 是「走多远」的预算，至少 1；
                # max_tokens / tool_result_limit 允许 0（0 = 关掉该护栏）。
                lo = 1 if k in ("max_iterations", "max_rounds") else 0
                if iv < lo:
                    return False, f"[错误] {k} 取值非法（{k} ≥ {lo}）"
                data[k] = iv
        for k in ("max_iterations", "max_tokens", "tool_result_limit",
                  "temperature", "max_rounds"):
            data.setdefault(k, None)
        try:
            self.ws.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return False, f"[错误] 无法写入 {path}：{exc}"
        self.apply_limits()
        return True, (f"[ok] 已更新运行边界：步数上限 {self.max_iterations}、"
                      f"max_tokens {self.max_tokens}、"
                      f"工具结果上限 {self.tool_result_limit} 字符、"
                      f"temperature {self.temperature}、最多 {self.max_rounds} 轮")

    # ---------------------------------------------------------------- 用量读数
    def round_stats(self) -> Dict[str, Any]:
        """把「轮」的读数交给模型 —— 看得见「我在第几轮、会不会有下一轮」。

        「步」是一轮的内部计数，「轮」是 Agent 的生命周期计数。模型只有看得见轮，
        才谈得上「这一轮做不完就留着下一轮做」：否则它既不知道 finish 之后还有没有
        人接着跑（`next`），也不知道这颗棋子还能走几格（`remaining`）。

        `max` 是本场**生效轮预算** `max_rounds`（Runtime 真正会执行的轮数），
        与运行边界读数里的 max_rounds 是同一个数 —— 只有一份真相。
        """
        total = int(self.max_rounds)
        remaining = max(0, total - int(self.round_index))
        return {
            "index": self.round_index,
            "max": total,
            "remaining": remaining,
            "autonomous": self.autonomous,
            "next": bool(remaining > 0),
            "step": self.iteration,
            "max_steps": self.max_iterations,
        }

    def next_round(self, summary: str = "") -> None:
        """（工具入口）宣告「这一阶段做完了，开新一轮」——替本条消息收尾，但不算 finish。

        与 finish 的区别：finish 是「整件事做完」，next_round 是「这一轮先到这，
        剩下的下一轮接着做」。只要本场还有轮预算（`next` 为真），运行期就会开下一轮；
        轮预算用尽时它只是把发言权交还，运行期如实告诉你「没有下一轮」。

        `summary` 由 Runtime 带到下一轮、播种在本轮历史开头（见 _seed_history），
        于是「交给下一轮的自己」是真能交接的，而不是只落在日志里。
        """
        self.next_round_summary = (summary or "").strip()
        self.wants_next_round = True
        self.finished = True

    def history_stats(self) -> Dict[str, Any]:
        """把「上下文用量的读数」交给模型 —— 自管的前提是看得见。"""
        used = _estimate_chars(self.history)
        budget = int(self.context_budget_chars)
        steps = _index_steps(self.history)
        foldable = _foldable_steps(steps)
        ratio = (used / budget) if budget > 0 else 0.0
        return {
            "used_chars": used,
            "budget_chars": budget,
            "ratio": round(ratio, 3),
            "steps": len(steps),
            "fold_range": (f"{foldable[0]}-{foldable[-1]}" if foldable else ""),
            "auto_fold": self._auto_fold_enabled,
            "auto_fold_env_locked": (self._env_autofold is not None
                                     and str(self._env_autofold).strip() != ""),
            "compactions": self._compaction_count,
        }

    # ---------------------------------------------------------------- 跨轮记忆
    def _seed_history(self) -> List[Dict[str, Any]]:
        """把本会话「之前几轮」播种成本轮历史的开头。

        多轮会话的「记性」长在这里：每轮都是新 Agent，上一轮的 history 不继承；
        运行期把会话中累积的轮次（seed_turns）显式传进来，这里还原成
        user/assistant 消息，模型就看得到「用户之前说过什么」。

        关键：**Agent 自己绝不读磁盘**。所以单次任务（没有会话）时 seed_turns
        为空，历史就是空的 —— 不会莫名背上上一次对话。会话记忆只活在进程内。
        播种只走消息层，system 依然每轮重组装，演化机制不受影响。
        """
        turns = self.seed_turns
        if not turns:
            return []
        history: List[Dict[str, Any]] = []
        for turn in turns:
            user = (turn.get("user") or "").strip()
            answer = (turn.get("answer") or "").strip()
            imgs = [x for x in (turn.get("images") or []) if x]
            if user or imgs:
                history.append({"role": "user",
                                "content": build_content(user, imgs,
                                                         base_dir=self.ws.root)})
            if answer:
                history.append({"role": "assistant", "content": answer})
        if history:
            self.rec.record("system", summary=f"跨轮记忆：播种会话内 {len(turns)} 轮对话",
                            data={"action": "seed_session_turns",
                                  "turns": len(turns), "messages": len(history)})
            self.progress.note(f"跨轮记忆：播种会话内 {len(turns)} 轮对话")
        return history

    # ---------------------------------------------------------------- 主循环
    def run(self) -> str:
        while not self.finished and self.iteration < self.max_iterations:
            self.iteration += 1
            self.rec.begin_step()
            self.progress.step(self.iteration, self.max_iterations)
            self.tick()                        # 每一步重读「上下文策略 / 运行边界」——改了就生效
            self._apply_pending_compactions()
            self._auto_compact()
            self._inject_images()
            self._inject_next_round_summary()  # 上一轮 next_round 的交接说明（只在首步注入）

            steps = _index_steps(self.history)
            fold_range = f"{min(steps)}-{max(steps)}" if steps else ""
            messages = build_messages(
                self.ws, self.reg, self.history,
                model=self.llm.config.model,
                iteration=self.iteration,
                max_iterations=self.max_iterations,
                last_action=self.last_action,
                fold_range=fold_range,
                context_stats=self.history_stats(),
                limits_stats=self.limits_stats(),
                round_stats=self.round_stats(),
            )
            self.progress.llm_start(self.llm.config.model)
            result = self.llm.chat(messages, tools=self.reg.schemas() or None,
                                   max_tokens=self.max_tokens,
                                   temperature=self.temperature)
            msg = result.get("message") or {}
            self.progress.llm_done(result.get("elapsed_ms", 0.0), result.get("usage"),
                                   result.get("error"), result.get("attempts", 1))
            self.rec.record_llm(result.get("raw_request", {}), result.get("raw_response", {}),
                                result.get("elapsed_ms", 0.0), result.get("usage"),
                                result.get("error"), result.get("attempts", 1))

            if result.get("error"):
                # 被 max_tokens 截断：这是**可恢复**的 —— tool_calls 极可能是半截
                # 非法 JSON，但模型的思路还在。温和地让它重发本条消息（而非杀死
                # 整场任务，那会白烧掉前面所有步的成本）。
                if _is_truncation(result):
                    self.truncations += 1
                    self.rec.record("error", summary="模型输出被截断，请重发",
                                    data={"error": result["error"],
                                          "attempt": self.truncations,
                                          "max": self.max_truncations})
                    if self.truncations <= self.max_truncations:
                        self.last_action = (
                            f"你上一条回复被 max_tokens={self.max_tokens} 截断了"
                            f"（finish_reason=length），tool_calls 可能是半截非法 JSON。"
                            f"请**重发同样的一条消息**，但更精简：只调用最必要的一个工具，"
                            f"或把长内容拆成几步；不要重复已完成的步骤。"
                            f"若确需更长输出，你可以自己用 `set_limits(max_tokens=…)` 调大"
                            f"生成上限（它会写进 .openthree/limits.json，下一步生效）。")
                        self.progress.note(f"输出被截断，已要求重发（{self.truncations}/{self.max_truncations}）")
                        continue
                    self.answer = (f"[未能完成] 模型连续 {self.truncations} 次因 max_tokens 截断输出。"
                                   f"可用 `set_limits(max_tokens=…)` 调大生成上限后重试"
                                   f"（若已被启动参数或环境锁定，则需在 config.yaml 调大）。")
                    break
                self.rec.record("error", summary="LLM 调用失败", data={"error": result["error"]})
                self.answer = f"[未能完成] 模型调用失败：{result['error']}"
                break

            content = content_text(msg.get("content")).strip()
            tool_calls = msg.get("tool_calls") or []

            # 记下模型说的话（reasoning 也是工作记录的一部分）
            if content:
                self.progress.thought(content)
                self.rec.record("note", summary=content[:200],
                                data={"thought": content, "phase": "assistant"})

            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.history.append(assistant_msg)

            if not tool_calls:
                # 没有工具调用 = 模型选择直接说话，视为阶段结论
                self.last_action = f"（无工具调用，直接回复）{content[:300]}"
                self.answer = content or self.answer
                self.rec.record("note", summary="模型未调用工具，视为收尾",
                                data={"answer": content})
                break

            self._run_tool_calls(tool_calls)

        if not self.finished and self.answer is None:
            # 撞到**本轮**的步数上限 —— 这不是整场任务的终点，而是「这一轮没跑完」。
            # 是否续轮由 Runtime 决定（计划还有未完成项 / max_rounds 还有余额）。
            self.step_limit_hit = True
            self.answer = (f"[本轮达到步数上限 {self.max_iterations} 步，未显式 finish] "
                           f"以下是最后一条思考：\n{self.last_action}")
            self.rec.record("error", summary="本轮达到步数上限",
                            data={"max": self.max_iterations,
                                  "round": self.round_index,
                                  "max_rounds": self.max_rounds})

        return self.answer or ""

    # ---------------------------------------------------------------- 执行工具
    def _run_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> None:
        summaries = []
        pending_images: List[Dict[str, Any]] = []
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or call.get("name") or ""
            raw_args = fn.get("arguments") or call.get("arguments") or "{}"
            call_id = call.get("id") or f"call_{self.iteration}_{name}"

            # finish 已提交：同批剩余调用不再执行，但仍补齐 tool 消息保持序列合法
            if self.finished:
                self.history.append({
                    "role": "tool", "tool_call_id": call_id, "name": name,
                    "content": "[跳过：finish 已提交，未执行]",
                })
                self.rec.record("note", summary=f"跳过 {name}（已 finish）",
                                data={"name": name, "call_id": call_id})
                self.progress.note(f"跳过 {name}（已 finish）")
                summaries.append(f"{name} → （跳过：已 finish）")
                continue

            args = _parse_args(raw_args)
            started = time.time()
            self.progress.tool_start(name, args)
            output, error = self._execute(name, args)
            elapsed = (time.time() - started) * 1000

            # 工具返回带图结果：tool 消息只能承载文本，图片另补一条 user 消息。
            # 先归一化成 (文本, 图片清单)，后续一切按文本处理。
            # 先登记，等本批全部 tool 消息补齐后再统一追加（保持 assistant → tool* 的合法序列）。
            call_images: List[str] = []
            if isinstance(output, ImageResult):
                call_images = list(output.images)
                if call_images:
                    pending_images.append({"text": output.brief(), "images": call_images,
                                           "detail": output.detail})
                output = output.brief()
            output = output if isinstance(output, str) else str(output or "")

            self.progress.tool_done(name, elapsed, len(output), error)

            self.rec.record(
                "tool",
                summary=f"{name}({_short_args(args)})",
                data={"name": name, "arguments": args, "output": _clip(output),
                      "error": error, "call_id": call_id, "images": call_images},
                elapsed_ms=elapsed,
            )
            summaries.append(f"{name} → {_clip(output, 160)}")

            body = output if output else (error or "[无输出]")
            self.history.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": self._spill(call_id, name, body),
            })

        self.last_action = "\n".join(summaries)
        for item in pending_images:
            self._append_image_message(item["text"], item["images"], item["detail"])

    def _append_image_message(self, text: str, images: List[str],
                              detail: str = DEFAULT_DETAIL) -> None:
        """把一批图片作为一条 user 消息追加进 history —— 这是模型「看见」的唯一通道。"""
        if not images:
            return
        content = build_content(
            (text or "").strip() or "以下是图片（工具刚生成的）：", images,
            detail=detail or DEFAULT_DETAIL, base_dir=self.ws.root)
        self.history.append({"role": "user", "content": content})
        n = count_images(content)
        self.rec.record("note", summary=f"注入 {n} 张图片供模型查看",
                        data={"action": "inject_images", "count": n,
                              "sources": images, "detail": detail or DEFAULT_DETAIL})
        self.progress.note(f"注入 {n} 张图片供模型查看")

    def _inject_next_round_summary(self) -> None:
        """把上一轮 `next_round(summary=…)` 的交接说明注入 history 开头（只一次）。

        与跨轮播种的对话不同：这是**上一轮的你**留给**这一轮的你**的便条，
        不是用户说的话，所以标成 system，放在最前面 —— 让本轮一开场就知道
        「上一轮到哪了、还剩什么」。
        """
        if self._next_round_injected or not self.next_round_summary:
            return
        self.history.insert(0, {
            "role": "system",
            "content": ("[上一轮交接] " + self.next_round_summary
                        + "\n（这是你主动 next_round 时留给这一轮的便条；接着做。）"),
        })
        self._next_round_injected = True
        self.rec.record("system", summary="注入上一轮 next_round 交接说明",
                        data={"action": "seed_next_round_summary",
                              "chars": len(self.next_round_summary)})

    def _inject_images(self) -> None:
        """把本轮的「任务文本 + 图片」作为一条 user 消息注入 history（只注入一次）。

        刻意**不复用**跨轮播种的 user 消息：那些是历史轮次，本轮任务
        与它们不是同一条。注入放在 history 末尾 —— 它才是「当前这一问」。
        """
        if self._images_injected or not self.current_images:
            return
        text = (self.task_text or "").strip() or "（图片输入）"
        self.history.append({
            "role": "user",
            "content": build_content(text, self.current_images, base_dir=self.ws.root),
        })
        self._images_injected = True
        n = len(self.current_images)
        self.rec.record("note", summary=f"本轮输入含 {n} 张图片",
                        data={"action": "seed_images", "count": n,
                              "sources": self.current_images})
        self.progress.note(f"本轮输入含 {n} 张图片")

    def _spill(self, call_id: str, name: str, text: str) -> str:
        """工具结果进入 history 前统一截断；超长部分落盘，history 只留预览 + 路径。"""
        text = text if isinstance(text, str) else str(text)
        limit = self.tool_result_limit
        if limit <= 0 or len(text) <= limit:
            return text
        out_dir = self.ws.logs_dir / "tool-output"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            fp = out_dir / f"{_safe_name(call_id)}.txt"
            fp.write_text(text, encoding="utf-8")
            note = f"\n... [已截断，完整输出共 {len(text)} 字符，见 {fp}，可用 read_file 读取]"
        except Exception:  # noqa: BLE001
            note = f"\n... [已截断，共 {len(text)} 字符]"
        return text[:limit] + note

    def _execute(self, name: str, args: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        tool = self.reg.get(name)
        if tool is None:
            return "", f"未知工具 {name}。当前可用：" + ", ".join(self.reg.names())
        call_args = dict(args)
        if tool.needs_kernel:
            call_args["kernel"] = KERNEL
        try:
            out = tool.func(**call_args)
        except TypeError as exc:
            # 参数不匹配是最常见的模型失误：**一步**把正确签名回给模型，
            # 别让它再多调一次 list_functions 才知道自己错在哪。
            return "", (f"参数不匹配：{exc}\n正确签名：{tool.signature_hint()}\n"
                        f"（可用参数：{', '.join(tool.parameters) or '无'}；"
                        f"必填：{', '.join(tool.required) or '无'}）")
        except Exception as exc:  # noqa: BLE001
            import traceback
            return "", f"执行异常 {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}"
        if out is None:
            return "[ok]（无返回值）", None
        if isinstance(out, ImageResult):
            return out, None      # 保住类型：上层要拆出图片另发 user 消息
        return str(out), None

    # ---------------------------------------------------------------- 上下文压缩
    def compact(self, start: int, end: int, summary: Optional[str]) -> Tuple[bool, str]:
        """折叠 step [start, end]：`summary` 非空则留摘要，为空则**直接丢弃**。

        登记后**立刻**在现有 history 上落地 —— 于是返回值说的是「真的省了多少」，
        而不是「已登记、留待下一步再看」。
        返回 (是否成功, 面向模型的说明)。
        """
        try:
            start, end = int(start), int(end)
        except (TypeError, ValueError):
            return False, f"[错误] start/end 必须是整数，收到 {start!r}/{end!r}"
        if start > end:
            return False, f"[错误] start({start}) 大于 end({end})"
        steps = _index_steps(self.history)
        if not steps:
            return False, "[错误] 当前还没有可折叠的 step（至少要走完一个工具调用回合）"
        lo, hi = min(steps), max(steps)
        chosen = sorted(s for s in steps if start <= s <= end)
        if not chosen:
            return False, (f"[错误] step 区间 {start}-{end} 与现有 step {lo}-{hi} 不相交，"
                           f"未折叠。请用当前可折叠范围 {lo}-{hi}。")
        # 最后一步不能折：它可能是你「正要走完」的那一步（工具结果还没全部回到
        # history），折掉会让紧随其后的 tool 消息变成孤儿、消息序列非法。
        last = max(steps)
        if last in chosen:
            if len(steps) == 1:
                return False, "[错误] 只有 1 个 step（就是当前这一步），没有更早的历史可折。"
            foldable = _foldable_steps(steps)
            return False, (f"[错误] 最后一步 step {last} 不能折（你正在走它）；"
                           f"请只折到 {last - 1}（当前可折 {foldable[0]}-{foldable[-1]}）。")
        self._compact_marks.append((start, end, summary or ""))
        before = _estimate_chars(self.history)
        self._apply_pending_compactions()
        after = _estimate_chars(self.history)
        verb = "折叠" if (summary and summary.strip()) else "丢弃"
        return True, (f"[ok] 已{verb} step {start}-{end}："
                      f"{before} → {after} 字符（省 {max(0, before - after)}）。")

    def _apply_pending_compactions(self) -> None:
        """把折叠意图落到 history 上（按 step 与 message 对齐）。

        一次性处理本批全部意图，避免逐条折叠后 step 重新编号导致区间错位。
        summary 为空的意图做「纯丢弃」（不留任何占位）—— 这是最省的压缩，
        模型自管时常用。
        """
        if not self._compact_marks:
            return
        marks = sorted(self._compact_marks)
        self._compact_marks = []

        steps = _index_steps(self.history)
        if not steps:
            return
        chosen = sorted({s for s in steps
                         for (st, en, _) in marks if st <= s <= en})
        if not chosen:
            return
        lo, hi = chosen[0], chosen[-1]
        lo_idx = min(steps[s][0] for s in chosen)
        hi_idx = max(steps[s][1] for s in chosen)

        # 有摘要的段落保留文字；空摘要的段落直接丢弃、不留痕迹。
        parts = [f"step {st}-{en}: {sm}" for st, en, sm in marks if sm and sm.strip()]
        head = self.history[:lo_idx]
        tail = self.history[hi_idx + 1:]
        if parts:
            node = {"role": "assistant",
                    "content": f"[已折叠 step {lo}-{hi}] " + "; ".join(parts)}
            tail = [node] + tail
        self.history = head + tail
        self._compaction_count += 1
        self.rec.record("evolution",
                        summary=(f"已{'摘要折叠' if parts else '丢弃'} step {lo}-{hi}"),
                        data={"action": "compact_applied", "start": lo, "end": hi,
                              "summary": "; ".join(parts), "dropped": not parts})

    def _auto_compact(self) -> None:
        """内核对「上下文爆仓」的兜底折叠 —— 但开关与预算都交给模型（见 context_budget）。

        这是「安全网」而不是「主控」：默认开着，好让第一次跑长任务不至于撞上限；
        模型一旦想自己管（把预算调大 / 关掉自动折叠），先写策略文件即可。
        环境变量 OPENTHREE_CONTEXT_AUTOFOLD=0 能从外面彻底冻结它（模型改不了）。
        """
        if not self._auto_fold_enabled:
            return
        if self.context_budget_chars <= 0:
            return
        before = _estimate_chars(self.history)
        if before <= self.context_budget_chars:
            return
        steps = _index_steps(self.history)
        keys = sorted(steps)
        keep = max(2, len(keys) // 4)          # 至少保留最近 2 步
        candidates = keys[:-keep] if len(keys) > keep else []
        if not candidates:
            return

        lo = candidates[0]
        hi = lo
        for s in candidates:
            hi = s
            folded = self.history[:steps[lo][0]] + self.history[steps[s][1] + 1:]
            if _estimate_chars(folded) <= self.context_budget_chars:
                break
        self.rec.record("note",
                        summary=f"上下文超预算（约 {before} 字符），自动折叠 step {lo}-{hi}",
                        data={"action": "auto_compact", "start": lo, "end": hi,
                              "budget": self.context_budget_chars, "before_chars": before})
        self.progress.note(f"上下文超预算（约 {before} 字符），自动折叠 step {lo}-{hi}")
        self.compact(lo, hi, "（自动折叠：早期步骤，完整过程见 logs/ 与 steps/）")

    # ---------------------------------------------------------------- 收尾
    def finish(self, answer: str) -> None:
        self.answer = answer
        self.finished = True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Agent iter={self.iteration} tools={len(self.reg)} events={len(self.rec.events)}>"


# ---------------------------------------------------------------- 工具函数


def _parse_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"value": data}
    except Exception:  # noqa: BLE001
        try:
            from json_repair import repair_json  # type: ignore
            data = json.loads(repair_json(raw))
            return data if isinstance(data, dict) else {"value": data}
        except Exception:  # noqa: BLE001
            return {"value": raw}


def _short_args(args: Dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", "⏎")
        parts.append(f"{k}={s[:60]}")
    return ", ".join(parts)


def _clip(text: str, limit: int = 4000) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [截断，共 {len(text)} 字符]"


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", name or "call")[:120] or "call"


def _as_int(value: Any, default: int, *, lo: Optional[int] = None) -> int:
    """安全转 int：解析不了就回退 `default`，低于 `lo` 就夹到 `lo`。

    `limits.json` 是模型/人能随手改写的磁盘状态，坏值（"abc"、数组、负数）不该
    把 ReAct 循环拖崩 —— 与 `_read_context_policy` 的「坏了也不能拖死」同一契约。
    """
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and out < lo:
        return lo
    return out


def _as_float(value: Any, default: float, *, lo: Optional[float] = None,
              hi: Optional[float] = None) -> float:
    """安全转 float：解析不了回退 `default`，越界夹到 [lo, hi]。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):   # NaN / inf
        return default
    if lo is not None and out < lo:
        return lo
    if hi is not None and out > hi:
        return hi
    return out


def _clamp_int(value: Any, default: int, *, lo: Optional[int] = None) -> int:
    return _as_int(value, default, lo=lo)


def _clamp_float(value: Any, default: float, *, lo: Optional[float] = None,
                 hi: Optional[float] = None) -> float:
    return _as_float(value, default, lo=lo, hi=hi)


def _numeric(value: Any) -> bool:
    """这个值能不能被当成数字？（用于「读数来源」诚实标注，不参与取值）。"""
    if value is None or isinstance(value, bool):
        return False
    try:
        out = float(value)
    except (TypeError, ValueError):
        return False
    return out == out and out not in (float("inf"), float("-inf"))


def _is_truncation(result: Dict[str, Any]) -> bool:
    """判断这次 LLM 结果是不是「被 max_tokens 截断」——这是可恢复的错误。

    优先用 llm 层给的 error_kind（明确信号）；退回看 finish_reason / 错误文本，
    好让 agent 在只带 finish_reason 的结果上也能识别。
    """
    if result.get("error_kind") == "truncated":
        return True
    if result.get("finish_reason") == "length":
        return True
    err = str(result.get("error") or "").lower()
    return "finish_reason=length" in err or "被截断" in err


def _estimate_chars(history: List[Dict[str, Any]]) -> int:
    """粗略估算一条 messages 的字符体量（1 token ≈ 2 字符就够了）。

    多模态 content（list）由 multimodal.estimate_content_chars 折算：
    图片按固定成本计，**不**按 base64 实际长度 —— 否则一张图就撑爆预算。
    """
    total = 0
    for m in history:
        total += estimate_content_chars(m.get("content"))
        for tc in (m.get("tool_calls") or []):
            try:
                total += len(json.dumps(tc, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                total += 50
    return total


def _index_steps(history: List[Dict[str, Any]]) -> Dict[int, Tuple[int, int]]:
    """history 中每个 assistant-with-tool_calls 算一个 step，覆盖到其 tool 结果末尾。"""
    steps: Dict[int, Tuple[int, int]] = {}
    n = 1
    i = 0
    while i < len(history):
        m = history[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                j += 1
            steps[n] = (i, j - 1)
            n += 1
            i = j
        else:
            i += 1
    return steps


def _foldable_steps(steps: Dict[int, Tuple[int, int]]) -> List[int]:
    """可折叠的 step：**排除最后一个**。

    最后一个 step 可能是「模型此刻正在走的这一步」（它的 tool 消息还没补齐），
    折掉它会让紧随其后的 tool 消息变成孤儿、消息序列非法。排除它最省心，
    也符合直觉：别折你正要走的那一步。
    """
    keys = sorted(steps)
    return keys[:-1] if len(keys) > 1 else []
