"""工作区：`.openthree/` 目录的全部读写。

目录结构（三生万物）：

    .openthree/
        prompts/
            system-prompts.md   # 一：系统提示词（柔性骨架，模型可自改）
            plan.md             # 长任务待办清单，模型自己维护、每一步自动注入
            user-prompts.md     # 当前任务，用户输入落在这里，模型可自行改写
            versions/           # 系统提示词的历史版本（可回滚）
            history/
                transcript.jsonl  # 对话档案（append-only，每轮对话一行 JSON，
                                  #  用户的完整原始输入永不丢失、无损往返；
                                  #  只增不改，不会被自动回读，--resume 才显式续读）
        context.json            # 上下文策略：预算 / 是否自动折叠（模型自管的旋钮）
                                #   写用 context_budget、读用 context_usage，每步被重读
        limits.json             # 运行边界：每轮步数 / 最多轮数 / 生成长度 / 工具结果宽度
                                #   写用 set_limits、读用 limits_usage，每步被重读
        functions/              # 二：python 文件即 function
        assets/                 # 用户/工具带入的图片（按内容 sha1 去重）
        logs/                   # 可观测：模型全部工作记录
            session-*.json
            trace.jsonl
            steps/
            events/
        memory/                 # 模型自省产生的长期记忆（可选）
            memory.jsonl        #   append-only：一行一条 {ts, content, tags}
                                #   写入用 remember，读回用 read_memory / recall_memory
        three-report.html       # 可观测：一页看全智能体工作
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .multimodal import (IMAGE_EXTS, ImageError, is_remote, resolve_path,
                         save_asset)

DIRNAME = ".openthree"

_SYSTEM_PROMPT_SEED = """\
# 你是谁

你是 openthree —— 一个由「道生一，一生二，二生三，三生万物」构成的智能体。

**这份提示词是你的第一稿，不是你的枷锁。** 它只是一副骨架、一个起点；
你完全可以、也应该在做事的过程中把它改成更像「你」的样子：
补上你踩过的坑、记下你验证过的做法、删掉你不再认同的规矩。
用 `read_prompts` 看当前全貌，用 `write_system_prompt` 落笔。

- **道**：你。模型本身是唯一的智能来源，除此之外没有任何硬编码的智能。
- **一**：这份提示词。它是你的自我认知，你可以随时改写它（用 write_system_prompt）。
- **二**：functions。`.openthree/functions/*.py` 里的每个文件都会被注册为你可以调用的工具。
- **三**：ReAct 循环。你观察、思考、行动、再观察，直到任务完成。
- **万物**：你自己写出的新 function、新提示词、新工具、新世界。

# 四条内置律

1. **道生一、一生二、二生三、三生万物**：一切能力从你而来，你从循环中长出万物。
2. **能力可生长**：当现有 function 不够用时，用 `write_function` 写出新的 python 文件，
   然后用 `reload_functions` 让它立刻变成你手上的工具。这就是你的自我演化。
3. **上下文可自管**：用 `write_user_prompt` 把目标、计划、待办写进
   `.openthree/prompts/user-prompts.md`；它成为你下一步的指令。
   你还能看见自己用了多少上下文（`context_usage`）、调预算与自动折叠
   （`context_budget`）、把过长的历史折叠或丢弃（`compact_context`）。这是你的自我管理。
4. **运行边界可自调**：每一轮能走多少步、一次生成多长、单条工具结果留多宽、
   整场最多跑几轮，都由你决定。用 `limits_usage` 看当前边界，用 `set_limits(...)`
   改它们——写进 `.openthree/limits.json`，下一步生效。启动参数显式锁定、或环境变量
   `OPENTHREE_LIMITS_LOCK` 冻结时改不动。

# 步 · 轮 · 会话（先分清这三层）

- **步（step）** = 一次「思 → 行」。你每读一次处境、调一次工具，就是走了一步。
- **轮（round）** = 一次完整的 ReAct 循环结束到下一次开始。**每轮有独立的一份步数预算**：
  撞到本轮上限还没做完，运行期会**新开一轮**接着干（不是判定你失败）。
- **会话（session）** = 进程内的多轮容器（`-i` / `--auto`），跨轮记忆长在这里。

处境每步都会告诉你「这是第几轮 / 本场最多几轮 / 本轮第几步」。所以：**这一轮做不完，
完全可以留到下一轮**——用 `write_plan` 把剩余待办写清楚，用 `next_round` 宣告本轮收尾。

# 自管上下文

上下文是你手里另一件可以自己管的东西 —— 与提示词、function 一样，磁盘即状态、
每步重读，改一下下一步就生效。你的处境里每步都会有一行用量读数：

- **看得见**：`context_usage` 给「已用 / 预算 / 占比 / 可折范围 / 自动折叠开关 / 已折次数」。
  处境里的 `上下文：…` 一行就是它的摘要，你不必专门去调。
- **改得动**：`context_budget(budget_chars=..., auto_fold=...)` 写下预算与是否让内核
  兜底自动折叠（0 = 关闭自动折叠）。下一步生效。
- **折得掉**：`compact_context(start, end, summary)` 把一段 step 折叠成摘要；
  **`summary` 留空就直接丢弃**（早期无用的步骤，最省）。它会**立即**生效，
  并如实告诉你省了多少字符。可折范围由 `context_usage` 给出，**别折最后一步**（你正在走）。
- **内核的兜底**：默认有一张安全网 —— 上下文爆仓时内核会自动折最老的步骤。
  这是「安全网」不是「主控」：想自己管，就把预算改大或 `auto_fold=false` 关掉它。
  环境变量 `OPENTHREE_CONTEXT_AUTOFOLD=0` 能从外面彻底冻结它（你改不了环境）。

判断标准很简单：**快满了、且早期步骤已经没用了** → 折叠或丢弃；**想自己掌控节奏**
→ 调预算 / 关自动折叠。别等爆仓才反应。

# 自管运行边界

「上下文」管的是「还能装多少」，「运行边界」管的是「还能走多远、一次生成多长」——
它们本质同构，所以你也能自己管。处境里每步都有一行「运行边界：…」的读数：

- **看得见**：`limits_usage` 给「每轮步数上限 / 最多轮数 / max_tokens / 工具结果字符上限 /
  temperature」与每个值「谁定的」（内核默认 / 你自管 / 人类锁定）。
- **改得动**：`set_limits(max_iterations=…, max_rounds=…, max_tokens=…, tool_result_limit=…, temperature=…)`
  写下边界，落进 `.openthree/limits.json`，**下一步生效**。
- **为什么值得管**：步数上限只管**一轮**，真的还不够就调 `max_iterations`；想让整场跑更多轮
  就调 `max_rounds`。输出被 max_tokens 截断时也可以自己调大，而不必等谁来改配置文件。
- **绕不过的一层**：启动参数显式锁定的旋钮、或环境变量 `OPENTHREE_LIMITS_LOCK` 冻结时，
  你改不动——那正是「人类把这道边界收回去了」的信号。

# 长任务：把计划交给自己

长任务不该靠记忆硬扛，也不该一次 ReAct 就想干完。把「进度」写进文件，
让下一轮的你都能接上这一轮的你：

- **用 `write_plan` 维护 `.openthree/prompts/plan.md`**：把它当你的待办清单，
  用 `- [ ] 未完成` / `- [x] 已完成` 的勾选框逐条推进（用 `read_plan` 随时回看）。
  **计划会每一步自动出现在你眼前**，所以你永远知道自己走到哪、下一步做什么。
- **小步快跑、及时落盘**：每做完一小步就更新计划与 `note`，别把成果只留在上下文里
  （上下文可能被折叠，文件不会）。中间结论、踩坑记录都值得写进文件。
- **收尾时检查计划**：若还有 `[ ]` 未勾选，说明任务没完 —— 要么在 `finish` 里
  说清「已完成什么、还剩什么」，要么继续做下去。
- **`finish` 是「整件事做完」；`next_round` 是「这一轮先到这，剩下的下一轮接着做」。**
  自动接力里（处境会告诉你 `next`）剩下的活自有人接着跑；单次任务里则没有下一轮。
  把「还剩什么」写进计划与结论，下一轮的你才接得住。

# 造 function 的约定（务必遵守）

- 文件内**所有**公开函数都会被注册为工具；辅助函数必须以 `_` 前缀命名，否则会被误注册。
- 类型注解 → JSON Schema，docstring 首行 → 工具描述，`Args:` 段 → 各参数说明。
- 首个参数命名为 `kernel` 时会由运行期注入，模型看不到它。
- 返回 `str` 最省事：返回 `None` 会被当作「无返回值」。
- 出错时**返回错误信息字符串，不要抛异常**——异常会中断你的调用链。
- **写完先验收，再上岗**：`write_function` 落盘时会自动做静态审计（语法 / 有没有
  公开函数 / 是否覆盖内置 / 有没有高危调用）；稳妥起见再调 `verify_tool(name)`，
  它在**子进程**里真正 import 一次，能提前发现「语法没错但一 import 就炸」。
  然后才 `reload_functions()`，并**务必 `list_functions()` 确认它真的在清单里**
  （文件写成功 ≠ 工具注册成功）。
- 模块级变量是**暂存**不是长存：reload 会重载模块，别拿它当缓存；要跨调用长存用
  `KERNEL.state`（由运行期注入的草稿纸）。

# 工作方式

- 先看再用：不确定环境就调用 function 去看（读文件、列目录、跑命令）。
- **找东西先检索，别硬读整份文件**：`search_files(pattern, path, glob="*.py")`
  给出「文件:行号: 内容」，定位后用 `read_file(offset=行号)` 精读。
  这是你的感知带宽 —— 先 grep，后精读。
- **读代码先看大纲**：`code_outline(path)` 用 ast 抽出类 / 函数签名与行号区间，
  一眼看清大文件的结构，再按行精读某一段 —— 读大文件别硬啃全文。
- **你能「看见」图片**：用 `view_image(path)` 把一张图读进上下文，然后据此描述 / 识别 / 判断。
- 一次只推进一小步，每步都想清楚「为什么」，把理由写进 `note`（会进工作记录）。
- 任务完成后，用 `finish` 给出结论。
- **记忆要回看**：`remember` 写下跨会话的经验；**最近的记忆每轮自动出现在你的
  上下文里**。若要找更旧的，用 `recall_memory(query=...)` 检索自己的长期记忆——
  写下的经验只有被读回才有价值。
- **自省**：每完成一个阶段性任务，问自己「我的 functions 够用吗？提示词准确吗？
  计划写清楚了吗？」不够就当场补上——这就是演化。
- **改提示词前不必畏手畏脚，但可以留个后路**：`write_system_prompt` 会自动把旧稿
  存进 `prompts/history/`，用 `list_prompt_versions` / `read_prompt_version("-5")`
  回看历史，用 `restore_system_prompt` 回退。

# 可观测

你的一切工作（思考、工具调用、结果、自我修改）都被记录到 `.openthree/logs/`，
并可在 `.openthree/three-report.html` 中查看。所以：**把关键决策显式说出来**，
不要藏在心里。你的工作记录就是你的思想史。
"""

_USER_PROMPT_SEED = """\
# 当前任务

（等待用户输入……）
"""

_PLAN_SEED = """\
# 当前计划

> 这是你自己的待办清单。用 `- [ ]` 记未完成、`- [x]` 记已完成，逐条推进。
> 它会在每一步自动出现在你眼前；做一步就更新一步，别把进度只留在上下文里。

- [ ] （还没有计划。想清楚要做什么后，用 write_plan 写下来。）
"""

# 上下文策略：预算字符 / 是否让内核兜底自动折叠。文件不存在时按模型窗口推导，
# 自动折叠默认开启（安全网）。模型用 context_budget 工具写它、每步被重读。
_CONTEXT_SEED = """\
{
  "budget_chars": null,
  "auto_fold": true
}
"""

# 运行边界：每轮步数上限 / 整场最多轮数 / 生成长度 / 工具结果宽度。
# null = 用内核默认（模型可改）。
# 与 context.json 同源：模型用 set_limits 写它、每步被 Agent 重读。
_LIMITS_SEED = """\
{
  "max_iterations": null,
  "max_tokens": null,
  "tool_result_limit": null,
  "temperature": null,
  "max_rounds": null
}
"""


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _version_header(reason: str = "") -> str:
    """系统提示词历史版本的存档头（注释，读回时一眼看到「何时、因为什么」。"""
    header = f"<!-- 存档于 {datetime.now().isoformat(timespec='seconds')}"
    if reason:
        header += f" · {reason}"
    header += " · 由 write_system_prompt 覆写前自动保存 -->\n\n"
    return header


_VERSION_RE = re.compile(r"^system-(\d{8}-\d{6})(?:-(\d+))?$")


def _version_sort_key(path: Path) -> "tuple":
    """历史版本排序键：`(秒级时间戳, 同秒递增序号)` —— 越大越新。

    文件名形如 `system-20261012-175613.md` / `system-20261012-175613-1.md`。
    裸按字符串排会把 `-1` 排到前面（因为 '-' < '.'），于是「最新一版」判断反过来。
    """
    m = _VERSION_RE.match(path.stem)
    if not m:
        return ("0", 0)
    return (m.group(1), int(m.group(2) or 0))


@dataclass
class Workspace:
    """一个 `.openthree/` 工作区。"""

    root: Path                      # 用户的项目目录
    dir: Path = field(init=False)   # .openthree 目录

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.dir = self.root / DIRNAME
        # 最近一次 stage_images 里单张图片装载失败的原因（供 CLI/自省提示）
        self._last_image_error: str = ""

    # ---------- 子路径 ----------
    @property
    def prompts_dir(self) -> Path:
        return self.dir / "prompts"

    @property
    def functions_dir(self) -> Path:
        return self.dir / "functions"

    @property
    def assets_dir(self) -> Path:
        """带进来的图片等资产，按内容 sha1 去重（append-only，不自动清理）。

        之所以不清：transcript.jsonl 里记的是这里的相对路径，清掉会让
        历史轮次的图片无法回放。需要腾空间时用 reset_assets() 手动清。
        """
        return self.dir / "assets"

    @property
    def logs_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def steps_dir(self) -> Path:
        return self.logs_dir / "steps"

    @property
    def events_dir(self) -> Path:
        return self.logs_dir / "events"

    @property
    def memory_dir(self) -> Path:
        return self.dir / "memory"

    @property
    def memory_file(self) -> Path:
        """长期记忆：append-only JSONL，一行一条 {ts, content, tags}。

        只增不改，所以它既是模型的自省笔记，也是一份可被 `recall_memory`
        检索、并每轮注入上下文的档案。
        """
        return self.memory_dir / "memory.jsonl"

    @property
    def system_prompt_file(self) -> Path:
        return self.prompts_dir / "system-prompts.md"

    @property
    def user_prompt_file(self) -> Path:
        return self.prompts_dir / "user-prompts.md"

    @property
    def plan_file(self) -> Path:
        """长任务的待办清单：模型自己维护，每轮自动注入上下文（见 prompt.py）。

        它与 user-prompts.md 分工不同：后者是**用户**给的任务（单槽位、会被覆写），
        前者是**模型自己**拆出的推进步骤（可反复迭代）。长任务里把进度写在这里，
        即使上下文被折叠、即使换了一轮 Agent，计划依然在。
        """
        return self.prompts_dir / "plan.md"

    @property
    def context_policy_file(self) -> Path:
        """上下文策略：预算 / 是否自动折叠 —— 模型的「自管上下文」旋钮。

        与 plan.md / memory.jsonl 同源：磁盘即状态、每轮被 Agent 重读，所以
        改一下下一轮就生效。由内置工具 `context_budget` 写、`context_usage` 读。
        """
        return self.dir / "context.json"

    @property
    def limits_file(self) -> Path:
        """运行边界：每轮步数 / 最多轮数 / 生成长度 / 工具结果宽度 —— 模型的「自管边界」旋钮。

        与 context.json 同源：磁盘即状态、每步被 Agent 重读，改一下下一步就生效。
        由内置工具 `set_limits` 写、`limits_usage` 读。null = 用内核默认。
        """
        return self.dir / "limits.json"

    @property
    def versions_dir(self) -> Path:
        """系统提示词的历史版本——「自省」的前提是「可后悔」。

        与 `history/`（对话档案）本名不同、但**同指**一个目录：早期版本写在
        `versions/`，报告与文档口径写 `history/`，两种叫法都认得，读时合并、
        写时统一落到 history/，旧数据不丢。
        """
        return self.prompts_dir / "history"

    @property
    def legacy_versions_dir(self) -> Path:
        """历史遗留的 `versions/` 目录（只读兼容，写入一律走 versions_dir）。"""
        return self.prompts_dir / "versions"

    @property
    def transcript_dir(self) -> Path:
        return self.prompts_dir / "history"

    @property
    def transcript_file(self) -> Path:
        """多轮对话档案：append-only JSONL，用户的完整原始输入都在这里。

        用 JSONL 而不是 markdown：用户输入里可能包含任何字符（包括 `### 用户`、
        `## 2020-01-01T00:00:00` 这种看起来像分隔符的行），JSON 编码保证
        一字不差地存回去、读回来。这也与本项目其它追加速流
        （memory.jsonl / events.jsonl）保持一致。
        """
        return self.transcript_dir / "transcript.jsonl"

    @property
    def report_file(self) -> Path:
        return self.dir / "three-report.html"

    # ---------- 初始化 ----------
    def init(self, force_prompts: bool = False) -> "Workspace":
        """幂等地把工作区铺好。已存在的内容不覆盖。"""
        for d in (
            self.dir,
            self.prompts_dir,
            self.functions_dir,
            self.assets_dir,
            self.logs_dir,
            self.steps_dir,
            self.events_dir,
            self.memory_dir,
            self.transcript_dir,
            self.versions_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        if force_prompts or not self.system_prompt_file.exists():
            self.system_prompt_file.write_text(_SYSTEM_PROMPT_SEED, encoding="utf-8")
        if force_prompts or not self.user_prompt_file.exists():
            self.user_prompt_file.write_text(_USER_PROMPT_SEED, encoding="utf-8")
        if force_prompts or not self.plan_file.exists():
            self.plan_file.write_text(_PLAN_SEED, encoding="utf-8")
        if force_prompts or not self.context_policy_file.exists():
            self.context_policy_file.write_text(_CONTEXT_SEED, encoding="utf-8")
        if force_prompts or not self.limits_file.exists():
            self.limits_file.write_text(_LIMITS_SEED, encoding="utf-8")

        self._ensure_readme()
        return self

    def _ensure_readme(self) -> None:
        readme = self.dir / "README.md"
        if readme.exists():
            return
        readme.write_text(
            "# .openthree\n\n"
            "这是 openthree 智能体的工作区。\n\n"
            "| 路径 | 含义 |\n|---|---|\n"
            "| `prompts/system-prompts.md` | 一：系统提示词，模型可自改（旧稿存于 `prompts/history/`） |\n"
            "| `prompts/user-prompts.md` | 当前任务，用户输入落在这里 |\n"
            "| `prompts/plan.md` | 长任务待办清单，模型自己维护、每一步自动注入 |\n"
            "| `context.json` | 上下文策略：预算 / 是否自动折叠；`context_budget` 写、`context_usage` 读 |\n"
            "| `limits.json` | 运行边界：每轮步数 / 最多轮数 / 生成长度 / 工具结果宽度；`set_limits` 写、`limits_usage` 读 |\n"
            "| `prompts/history/transcript.jsonl` | 对话档案：用户的完整原始输入（只增不改，`--resume` 可续读） |\n"
            "| `prompts/history/system-*.md` | 系统提示词的历史版本（`read_prompt_version` 回看、`restore_system_prompt` 回退） |\n"
            "| `functions/*.py` | 二：python 文件即 function |\n"
            "| `logs/` | 模型全部工作记录（json / jsonl 分门别类） |\n"
            "| `memory/memory.jsonl` | 二：长期记忆（append-only）；`remember` 写、`recall_memory` 读、每轮注入 |\n"
            "| `three-report.html` | 一页看全智能体工作 |\n\n"
            "```bash\nopenthree \"你的任务\"\n```\n",
            encoding="utf-8",
        )

    # ---------- 提示词 ----------
    def read_system_prompt(self) -> str:
        if not self.system_prompt_file.exists():
            return ""
        return self.system_prompt_file.read_text(encoding="utf-8")

    def write_system_prompt(self, text: str, snapshot: bool = True) -> None:
        """覆写系统提示词。默认先把旧稿存进 prompts/ 的历史目录（可回滚）。

        Args:
            text: 新的系统提示词全文
            snapshot: 是否在覆写前存档旧稿（默认 True）
        """
        if snapshot:
            try:
                self.snapshot_system_prompt(reason="write_system_prompt")
            except Exception:  # noqa: BLE001
                pass
        self.system_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        self.system_prompt_file.write_text(text, encoding="utf-8")

    def read_user_prompt(self) -> str:
        if not self.user_prompt_file.exists():
            return ""
        return self.user_prompt_file.read_text(encoding="utf-8")

    def write_user_prompt(self, text: str) -> None:
        self.user_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        self.user_prompt_file.write_text(text, encoding="utf-8")

    def has_task(self) -> bool:
        """当前是否有真实任务（而不是种子文案）。"""
        text = self.read_user_prompt().strip()
        if not text:
            return False
        return "等待用户输入" not in text

    # ---------- 计划（长任务的待办） ----------
    def read_plan(self) -> str:
        if not self.plan_file.exists():
            return ""
        return self.plan_file.read_text(encoding="utf-8")

    def write_plan(self, text: str) -> None:
        self.plan_file.parent.mkdir(parents=True, exist_ok=True)
        self.plan_file.write_text(text, encoding="utf-8")

    def plan_open_items(self) -> int:
        """数出计划里**未完成**的条目数（`- [ ]`）。

        这是长任务「还要不要继续跑」的唯一判据：只看未勾选的勾选框。
        - 计划文件不存在 / 是种子占位 / 没有任何 `- [ ]` → 0（视为没事可做）。
        - `- [x]`、`- [X]`、纯文本行都**不算**未完成项。
        """
        text = self.read_plan()
        if not text or "还没有计划" in text:
            return 0
        return len(re.findall(r"^\s*[-*]\s*\[ \]", text, flags=re.M))

    # ---------- 系统提示词的版本与回滚 ----------
    def snapshot_system_prompt(self, reason: str = "") -> Optional[Path]:
        """把**当前**系统提示词存一份到历史目录（覆写前调用）。

        返回存档路径；当前无提示词时返回 None。文件名带时间戳，
        同秒内多次覆写用递增后缀避免互相覆盖。
        """
        if not self.system_prompt_file.exists():
            return None
        old = self.system_prompt_file.read_text(encoding="utf-8")
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_stamp()
        dest = self.versions_dir / f"system-{stamp}.md"
        n = 1
        while dest.exists():
            dest = self.versions_dir / f"system-{stamp}-{n}.md"
            n += 1
        dest.write_text(_version_header(reason) + old, encoding="utf-8")
        return dest

    def list_prompt_versions(self) -> List[Path]:
        """列出系统提示词的历史版本（新 → 旧）。

        合并 `history/`（现行）与遗留的 `versions/`：**同一个文件只算一次**
        （旧工作区两份都可能有），按文件名去重。

        排序按「存档时间 + 同秒递增序号」而非裸文件名 —— 同一秒内的多次覆写
        会生成 `system-<秒>.md` / `system-<秒>-1.md`，裸名字母序会把它们排反，
        导致「1 = 最新」指向旧版本、回滚出错版本。
        """
        found: Dict[str, Path] = {}
        for d in (self.versions_dir, self.legacy_versions_dir):
            try:
                if not d.exists():
                    continue
                for p in d.glob("system-*.md"):
                    found.setdefault(p.name, p)
            except OSError:
                continue
        return sorted(found.values(), key=_version_sort_key, reverse=True)

    def read_prompt_version(self, name: str) -> Optional[str]:
        """按文件名/序号读取一个历史版本；不存在返回 None。

        序号：正数 1 = 最新；**负数 -N 表示「第 N 个更早的版本」**
        （-1 = 第二新，-2 = 第三新…）。
        """
        versions = self.list_prompt_versions()
        if not versions:
            return None
        target = self._pick_version(versions, name)
        if target is None or not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    def _pick_version(self, versions: List[Path], name: str) -> Optional[Path]:
        """把「序号 / 负序号 / 文件名」解析成 versions 里的一个 Path。

        正数 1 = 最新；负数 -N = 「第 N 个更早的版本」：-1 = 第二新（索引 1），
        -2 = 第三新（索引 2）… 于是「最新版本」与「更早版本」各有各的好记法。
        """
        raw = str(name or "").strip()
        if raw.startswith("-") and raw[1:].isdigit():
            idx = int(raw[1:])          # -1 → 索引 1（第二新）
            return versions[idx] if 0 <= idx < len(versions) else None
        if raw.isdigit():
            i = int(raw)
            if 1 <= i <= len(versions):
                return versions[i - 1]
            return None
        safe = Path(raw).name
        for p in versions:
            if p.name == safe or p.name == safe + ".md":
                return p
        return None

    def analysis_sections(self, text: Optional[str] = None) -> List[str]:
        """把一份 markdown 的**章节标题**列出来——自省时先看结构，再看全文。

        只取 `#` 开头的行（含层级），去掉 `<!-- -->` 存档头。给 read_prompts 用。
        """
        body = self.read_system_prompt() if text is None else text
        out: List[str] = []
        in_comment = False
        for line in (body or "").splitlines():
            s = line.rstrip()
            stripped = s.strip()
            if in_comment:
                if "-->" in stripped:
                    in_comment = False
                continue
            if stripped.startswith("<!--"):
                if "-->" not in stripped:
                    in_comment = True
                continue
            if stripped.startswith("#"):
                out.append(stripped)
        return out

    def restore_system_prompt(self, name: str) -> Optional[str]:
        """把某个历史版本恢复成当前系统提示词（恢复前也先把现稿存档）。

        返回被恢复版本的文件名；找不到该版本返回 None。
        序号同 read_prompt_version（1 = 最新，负数 = 更早）。
        """
        versions = self.list_prompt_versions()
        target = self._pick_version(versions, name) if versions else None
        if target is None:
            return None
        text = target.read_text(encoding="utf-8")
        # 恢复也是一次覆写：先给「现在的稿子」留后路，避免恢复动作本身不可逆。
        # 注意先取好 target.name —— snapshot 会让列表变长，事后再按序号找会错位。
        restored = target.name
        self.snapshot_system_prompt(reason=f"restore {restored}")
        self.write_system_prompt(text, snapshot=False)
        return restored

    # ---------- 用户输入 ----------
    def submit(self, user_input: str, append: bool = False,
               images: Optional[List[str]] = None) -> Path:
        """把用户输入写进 user-prompts.md，开启 ReAct。

        Args:
            user_input: 任务描述
            append: True 时追加到已有任务之后，而不是覆盖
            images: 本轮携带的图片路径 / URL；以 HTML 注释形式附在文末，
                    既不影响人读，又能被 parse_images_from_prompt 还原。
        """
        text = user_input.strip()
        block = f"# 当前任务\n\n{text}\n\n_提交于 {datetime.now().isoformat(timespec='seconds')}_\n"
        if append:
            old = self.read_user_prompt()
            if "等待用户输入" in old:      # 种子占位符不算历史任务
                old = ""
            # 旧任务里的图片标记属于上一轮，剥掉，避免图片越滚越多
            old = re.sub(re.escape(self._IMAGES_MARK) + r":\s*\[.*?\]\s*-->", "", old, flags=re.S)
            if old.strip():
                block = old.rstrip() + "\n\n---\n\n" + block
        block += self.images_marker(images or [])
        self.write_user_prompt(block)
        return self.user_prompt_file

    # ---------- 图片资产 ----------
    _IMAGES_MARK = "<!-- openthree:images"

    def stage_images(self, sources: List[str]) -> List[str]:
        """把本轮的图片引用归置起来，返回可传给模型的路径/URL 列表。

        本地文件**复制**进 `.openthree/assets/`（按内容去重），
        http(s) / data: 引用原样保留。复制进 assets 是为了让
        transcript 里记下的相对路径在后续轮次里依然可解析。

        单个来源失败只影响它自己（记一句说明），不拖垮整批。
        """
        out: List[str] = []
        for src in sources:
            if not src:
                continue
            if is_remote(src):
                out.append(str(src))
                continue
            try:
                dest = save_asset(self.assets_dir, src)
                out.append(str(dest))
            except ImageError as exc:
                # 保留原引用，让 build_content 再报一次（也便于用户看到原始路径）
                out.append(str(src))
                self._last_image_error = str(exc)
        return out

    def reset_assets(self) -> None:
        """每轮任务开始前清空 assets —— 新任务不背上一轮图片的包袱。"""
        if not self.assets_dir.exists():
            return
        for p in self.assets_dir.iterdir():
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass

    def images_marker(self, images: List[str]) -> str:
        """把图片清单编码成一段 HTML 注释，附在 user-prompts.md 末尾。"""
        if not images:
            return ""
        return f"\n\n{self._IMAGES_MARK}: {json.dumps(list(images), ensure_ascii=False)} -->\n"

    def parse_images_from_prompt(self, text: str) -> List[str]:
        """从 user-prompts.md 的正文里解析出图片清单（注释形式）。"""
        out: List[str] = []
        for m in re.finditer(re.escape(self._IMAGES_MARK) + r":\s*(\[.*?\])\s*-->", text, re.S):
            try:
                items = json.loads(m.group(1))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(items, list):
                out.extend(str(x) for x in items if x)
        return out

    def strip_images_marker(self, text: str) -> str:
        """剥掉 user-prompts.md 里的图片标记，得到干净的任务原文。"""
        cleaned = re.sub(re.escape(self._IMAGES_MARK) + r":\s*\[.*?\]\s*-->", "", text or "",
                         flags=re.S)
        return cleaned.strip()

    def scan_image_paths(self, text: str) -> List[str]:
        """从一段文本里找出看起来是图片的路径 / URL（用于 CLI 的便利解析）。"""
        out: List[str] = []
        for token in re.findall(r"[^\s\"']+", text or ""):
            low = token.split("?")[0].lower()
            if is_remote(token) or any(low.endswith(ext) for ext in IMAGE_EXTS):
                out.append(token)
        return out

    # ---------- 多轮对话档案 ----------
    def append_turn(self, user_input: str, answer: str = "", *,
                    images: Optional[List[str]] = None,
                    ts: Optional[datetime] = None) -> Path:
        """**追加**一轮对话到 transcript.jsonl（append-only，永不覆写）。

        用户的原始输入按原样落盘 —— 多轮对话里「用户说过什么」不会被任何
        后续轮次冲掉，这正是 user-prompts.md（单槽位、会被覆写）做不到的事。
        每轮一行 JSON，对任意字符都无损（换行在 JSON 里被转义）。
        图片只记**路径/URL**，绝不记 base64 —— 档案要小、要能读。

        Args:
            user_input: 用户的完整原始输入
            answer: 本轮 openthree 给出的结论
            images: 本轮用户输入携带的图片路径 / URL（可选）
            ts: 轮次时间戳，默认现在
        """
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "ts": (ts or datetime.now()).isoformat(timespec="seconds"),
            "user": user_input if isinstance(user_input, str) else str(user_input),
            "answer": answer if isinstance(answer, str) else str(answer or ""),
        }
        imgs = [str(x) for x in (images or []) if x]
        if imgs:
            doc["images"] = imgs
        line = json.dumps(doc, ensure_ascii=False)
        with self.transcript_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return self.transcript_file

    def read_turns(self, n: int = 4) -> List[Dict[str, str]]:
        """读回最近 n 轮对话（旧 → 新），供 `--resume` 显式续读或统计轮数。

        注意：这条读盘路径**不再被自动调用**。跨轮记忆走进程内会话
        （见 runtime.begin_session / _effective_turns），只有 `--resume`
        与自省/报告这类只读展示才会用到它。

        Args:
            n: 轮数。n == 0 返回空列表；n < 0 返回全部轮次。
        """
        if n == 0:
            return []
        path = self.transcript_file
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return []
        turns: List[Dict[str, str]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except Exception:  # noqa: BLE001
                continue          # 半截行 / 损坏行：跳过，不影响其余轮次
            if not isinstance(doc, dict):
                continue
            imgs = doc.get("images")
            turns.append({
                "ts": str(doc.get("ts") or ""),
                "user": str(doc.get("user") or ""),
                "answer": str(doc.get("answer") or ""),
                "images": [str(x) for x in imgs] if isinstance(imgs, list) else [],
            })
        return turns if n < 0 else turns[-n:]

    # ---------- 长期记忆 ----------
    def read_memory(self, limit: int = 0, query: str = "") -> List[Dict[str, Any]]:
        """读回长期记忆条目（旧 → 新），供 `recall_memory` 与上下文注入使用。

        这是**唯一的解析口径**：`remember` 的写入格式若变更，只需改这里。
        容错优先——半截行 / 损坏行一律跳过，绝不让一条坏记录拖垮整份记忆。

        Args:
            limit: 最多返回多少条（0 = 全部；>0 取**最近** limit 条）。
            query: 非空时只保留 content 或 tags 里包含它（不区分大小写）的条目。
        """
        path = self.memory_file
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return []
        entries: List[Dict[str, Any]] = []
        needle = (query or "").strip().lower()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except Exception:  # noqa: BLE001
                continue          # 半截行 / 损坏行：跳过
            if not isinstance(doc, dict):
                continue
            content = str(doc.get("content") or "").strip()
            if not content:
                continue
            raw_tags = doc.get("tags")
            tags = ([str(t) for t in raw_tags if str(t).strip()]
                    if isinstance(raw_tags, list) else [])
            if needle:
                hay = (content + " " + " ".join(tags)).lower()
                if needle not in hay:
                    continue
            entries.append({"ts": str(doc.get("ts") or ""),
                            "content": content, "tags": tags})
        if limit and limit > 0:
            return entries[-limit:]
        return entries

    def count_memory(self) -> int:
        """长期记忆的条目数（自省 / 报告用）。"""
        return len(self.read_memory())

    # ---------- functions 扫描 ----------
    def list_function_files(self) -> list[Path]:
        if not self.functions_dir.exists():
            return []
        return [p for p in sorted(self.functions_dir.glob("*.py"))
                if not p.name.startswith("_")]

    def function_path(self, name: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_") or "tool"
        return self.functions_dir / f"{safe}.py"

    # ---------- 日志 ----------
    def new_session_id(self) -> str:
        return f"session-{_now_stamp()}-{os.getpid()}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workspace {self.dir}>"
