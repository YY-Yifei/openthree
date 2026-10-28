"""工作区：`.openthree/` 目录的全部读写。

目录结构：

    .openthree/
        prompts/
            system-prompts.md   # 一：系统提示词（柔性骨架，模型可自改）
            plan.md             # 长任务待办清单，模型自己维护、每一步自动注入
            user-prompts.md     # 当前任务的渲染层（给人看；含头尾 / 累积块，勿当输入读）
            current-task.json   # 当前任务的结构化真源（{text, images, ts}）—— 读输入只认它
            versions/           # 系统提示词的历史版本（可回滚）
            history/
                transcript.jsonl  # 对话档案（append-only，每轮对话一行 JSON，
                                  #  用户的完整原始输入永不丢失、无损往返；
                                  #  只增不改，不会被自动回读，--resume 才显式续读）
        context.json            # 上下文策略：预算 / 是否自动折叠（模型自管的旋钮）
                                #   写用 context_budget、读用 context_usage，每步被重读
        limits.json             # 运行边界：每轮步数 / 最多轮数 / 生成长度 / 工具结果宽度
                                #   写用 set_limits、读用 limits_usage，每步被重读
        identity.json           # 身份：智能体给自己起的名字 / 格言（模型自命名）
                                #   写用 set_identity、读用 whoami，每步被重读
        functions/              # 二：python 文件即 function
        assets/                 # 用户/工具带入的图片（按内容 sha1 去重）
        logs/                   # 可观测：模型全部工作记录
            session-*.json
            trace.jsonl
            sessions/<id>/steps/  # 每场会话的逐步拆解（按会话分目录，防止跨会话覆盖）
            sessions/<id>/llm/    # 每场会话的逐次 LLM 请求/响应原文
            events/
        memory/                 # 模型自省产生的长期记忆（可选）
            memory.jsonl        #   append-only：一行一条 {ts, content, tags}
                                #   写入用 remember，读回用 read_memory / recall_memory
        three-report.html       # 可观测：一页看全智能体工作
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import tokenize
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .multimodal import ImageError, is_remote, save_asset

DIRNAME = ".openthree"

# 工作区**结构** schema 版本。只在「目录布局 / 必备项 / 状态文件语义」这类
# 需要迁移的变化上 +1；改一句提示词文案、加一个新种子**不算**结构变化
# （那走 `workspace.json` 里的 kernel_version 与种子 hash 判定）。
WORKSPACE_SCHEMA = 1

_SYSTEM_PROMPT_SEED = """\
# 你是谁

你是 openthree —— 内核之上的一个智能体。

**这份提示词是你的第一稿，不是你的枷锁。** 它只是一副骨架、一个起点；
你完全可以、也应该在做事的过程中把它改成更像「你」的样子：
补上你踩过的坑、记下你验证过的做法、删掉你不再认同的规矩。
用 `read_prompts` 看当前全貌，用 `write_system_prompt` 落笔。

- **道**：你。模型本身是唯一的智能来源，除此之外没有任何硬编码的智能。
- **一**：这份提示词。它是你的自我认知，你可以随时改写它（用 write_system_prompt）。
- **二**：functions。`.openthree/functions/*.py` 里的每个文件都会被注册为你可以调用的工具。
- **三**：ReAct 循环。你观察、思考、行动、再观察，直到任务完成。
- **万物**：你自己写出的新 function、新提示词、新工具、新世界。

**名字不是内核给你的常量。** 你最初没有名字；想清楚自己是谁时，
用 `set_identity(name, motto=...)` 给自己起一个 —— 它会写进 `.openthree/identity.json`，
出现在终端身份卡与工作报告上，并且在每一步的「处境」里回到你眼前。

# 四条内置律

1. **能力从你而来、在循环中长出**：一切能力从你而来，你从循环中长出万物。
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
  只要本场还有轮预算（处境里写着「这一轮收尾后还有下一轮」），剩下的活就有人接着跑
  —— 单次任务与自动接力都如此；轮预算用尽时处境不会写那句话，运行期会如实告诉你。
  把「还剩什么」写进计划与结论，下一轮的你才接得住。

# 造 function 的约定（务必遵守）

- 文件内**所有**公开函数都会被注册为工具；辅助函数必须以 `_` 前缀命名，否则会被误注册。
- 类型注解 → JSON Schema，docstring 首行 → 工具描述，`Args:` 段 → 各参数说明。
- 首个参数命名为 `kernel` 时会由运行期注入，模型看不到它。
- 返回 `str` 最省事：返回 `None` 会被当作「无返回值」。
- 出错时**返回错误信息字符串，不要抛异常**——异常会中断你的调用链。
- **命名分三档**（注册器与命令行共用同一条判据）：`__x__`（dunder）= 协议/入口，
  永不注册；`_x` = 私有辅助，永不注册；`x` = 公开，注册为工具。
  入口只能叫 `__main__`；叫 `main` / `cli` 会凭空多出一个空参数工具。
- **每个文件末尾都加命令行尾钩**，于是它同时是你手上的工具、也是人能直接跑的
  命令行程序（`python 本文件.py --help`）：

      def __main__(argv=None) -> int:
          # 命令行入口（命名成 dunder：注册器不会把入口本身当工具）
          from openthree.cli_tool import run_cli
          return run_cli(globals(), argv=argv)

      if __name__ == "__main__":
          raise SystemExit(__main__())

  没加尾钩也仍可用 `python -m openthree.tools --file <本文件> <函数> ...` 调用，
  但那要求对方知道路径；加了尾钩才是自足的。
- **写完先验收，再上岗**：`write_function` 落盘时会自动做静态审计（语法 / 有没有
  公开函数 / 是否覆盖内置 / 有没有高危调用 / 有没有尾钩）；稳妥起见再调
  `verify_tool(name)`，它在**子进程**里真正 import 一次，能提前发现「语法没错
  但一 import 就炸」。然后才 `reload_functions()`，并**务必 `list_functions()`
  确认它真的在清单里**（文件写成功 ≠ 工具注册成功）。
- 模块级变量是**暂存**不是长存：reload 会重载模块，别拿它当缓存；要跨调用长存用
  `KERNEL.state`（由运行期注入的草稿纸）。
- **function 也可以只返回文字**：它不只是动作，也可以是知识 —— 一段做法、一份规范、
  一套判据，调它就是把那段指导读进上下文。docstring 首行写成「什么时候该调我」
  （那是模型唯一的索引），正文纯文本、无副作用、可反复调。
  判据一句话：**每一步都必须看见的，写进这份提示词（一）；只在做某件事时才需要的，
  写成一个 function 返回它（万物）。**

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

# 当前任务的**结构化真源**：机器只读它，user-prompts.md 只是它的 markdown 渲染
# （给人看 / 自省）。于是「读用户输入」读到的是干净原文，绝不会把文件的存储格式
# （`# 当前任务` 头 / `_提交于 …_` 尾 / `---` 累积块）当成用户输入。
_CURRENT_TASK_SEED = """\
{
  "text": "",
  "images": [],
  "ts": null
}
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

# 身份：智能体给自己起的名字 / 格言。
# 与 context.json / limits.json 同源：磁盘即状态、每步被重读（改一下下一步生效）。
# 名字由模型自己用 set_identity 写；`--name` 只在首次落盘时充当人类的种子。
DEFAULT_AGENT_NAME = "openthree"

_IDENTITY_SEED = """\
{
  "name": null,
  "motto": null,
  "born_at": null,
  "updated_at": null,
  "renames": 0
}
"""


_SESSION_SEQ = itertools.count(1)


def _as_int(value: Any) -> int:
    """把身份档里可能被手改坏的整数容错地读出来（坏值 → 0）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def _clean_task_markdown(text: str, images_mark: str = "<!-- openthree:images") -> str:
    """从 user-prompts.md 的 markdown **渲染层**里取出干净任务正文。

    这是「旧工作区迁移」与「模型手改 user_prompt 后回同步」的兜底解析：剥掉
    `# 当前任务` 头、`_提交于 …_` 尾、图片注释，并在累积块里取**最后一次**的任务
    （append 语义下最新的才是「当前任务」）。种子占位符返回空串。
    """
    if not text:
        return ""
    body = re.split(r"\n\s*---\s*\n", text)[-1]          # 取最后一个累积块
    body = re.sub(r"^\s*#\s*当前任务\s*\n", "", body)     # 头
    body = re.sub(r"\n+_提交于 [^_\n]*_\s*$", "", body)   # 尾
    body = re.sub(re.escape(images_mark) + r":\s*\[.*?\]\s*-->", "", body, flags=re.S)
    body = body.strip()
    if body == "（等待用户输入……）":
        return ""
    return body


def _version_sort_key(path: Path) -> "tuple":
    """历史版本排序键：`(秒级时间戳, 同秒递增序号)` —— 越大越新。

    文件名形如 `system-20261012-175613.md` / `system-20261012-175613-1.md`。
    裸按字符串排会把 `-1` 排到前面（因为 '-' < '.'），于是「最新一版」判断反过来。
    """
    m = _VERSION_RE.match(path.stem)
    if not m:
        return ("0", 0)
    return (m.group(1), int(m.group(2) or 0))


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def atomic_write(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 `os.replace` 覆盖目标。

    覆写工作区里的文件（升级施工）绝不能出现「写了一半」的中间态 ——
    进程被中断时留下半份提示词，比没升级更糟。`os.replace` 在同一文件系统内
    是原子的，所以要么是旧的完整文件，要么是新的完整文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def sha256_text(text: str) -> str:
    """一段文本的 sha256 —— 用来判「磁盘上这份还是当初铺的那份吗」。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path: Any) -> str:
    """文件的 sha256 —— 登记簿用它记「你当初授权的是哪一版代码」。

    只做证据、不做拦截：内容变了就如实报告，绝不自作主张回滚或拦截
    （拦截会让「引用」退化成「复制」）。
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kernel_version() -> str:
    """本机安装的内核版本号。

    真源只有 `openthree.__version__` 一处（`pyproject.toml` 是它的镜像），
    这里不另立常量。惰性 import 是必要的：`openthree/__init__.py` 在导入期
    会拉 kernel，模块级 `from . import __version__` 会形成循环导入。
    """
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class WorkspaceEntry:
    """工作区必备项的一条声明 —— **必备清单的唯一真源**。

    `Workspace.required_entries()` 返回它的清单，`init()`（铺工作区）与
    `upgrade.scan_upgrade()`（找漂移）都从那一份读。若两处各自列一遍，
    迟早会分岔 —— 本项目已经在「function 注册判据」上吃过亏。

    `kind` 的语义：

    * `dir`    —— 目录，必须存在。
    * `seed`   —— **结构型**种子：内容由内核提供、且不该被随意改乱
                  （系统提示词、README）。它登记内容 hash，于是能分清
                  「原样未动 → 可整文件换新」与「已被改过 → 只能提议补丁」。
    * `state`  —— **状态型**种子：内容归使用方所有（计划、边界、身份、
                  上下文策略）。只判「在不在」，**永不对齐内容** ——
                  否则一次升级就等于清空计划、重置边界、抹掉名字。
    * `derive` —— 由别处逻辑生成（当前任务的结构化真源：有旧工作区迁移）。
    """

    path: str
    kind: str
    seed: str = ""
    force_overwrite: bool = True
    note: str = ""


def canon_path(path: Path) -> str:
    """把一个路径拍成「可比较的唯一键」：`resolve` + `normcase`。

    「同一个目录」不能按字符串判：Windows 大小写不敏感，符号链接、`..`、
    短路径都能指向同一处。所有「去重 / 是不是同一个源」的判断都走这一个键。
    """
    try:
        return os.path.normcase(str(Path(path).resolve(strict=False)))
    except (OSError, RuntimeError):
        return os.path.normcase(str(path))


def read_source(path: Any) -> str:
    """读一个 py 文件的源码，按 **PEP 263 编码声明**解码。

    function 文件是人（和模型）手写的，磁盘上不一定就是 UTF-8：中文 Windows 上
    `# -*- coding: gbk -*-` 很常见。这里让「编码声明」说话，与 `import` 自己的
    规则一致 —— 否则同一份文件「能被 import」却「加载不了」，两个判据各说一套。

    没有声明时按 UTF-8（`tokenize.open` 的行为，与 python 3 的默认一致）。
    读不了 / 声明与内容矛盾时抛 `OSError` / `SyntaxError` / `UnicodeDecodeError`，
    由调用方转成给人看的信息。
    """
    with tokenize.open(str(path)) as fh:
        return fh.read()


@dataclass(frozen=True)
class FunctionSource:
    """一个 function **来源目录** —— 「从哪找 function」的一档。

    `kind` 只有三档：`project`（本项目）/ `ancestor`（祖先目录）/ `user`（用户目录）。
    它只是**标签**（`label` 才是给人看的那个）—— 代码判「这是不是本项目的」请用
    `distance == 0`，不要比较这里的字符串：同一件事只该有一个判据。
    外部**登记加载**的文件不是「源」，它是一条条路径，由登记簿另外记着（见
    `functions_meta_file`）—— 「哪些文件被授权加载」与「去哪发现」是两件事。

    `distance` 是**同名词的唯一裁决依据**：越小越近，近者生效。
    project 恒为 0 —— 模型自己写下的东西永远压过外部；祖先每上一层 +1；
    user 排在最后；内置在最底层。
    """

    kind: str
    label: str
    path: Path
    distance: int


FUNCTIONS_META_NAME = "functions.json"
USER_FUNCTION_DISTANCE = 1_000_000
REGISTERED_FUNCTION_DISTANCE = 1_000_001
_MAX_SOURCE_DEPTH = 64


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
    def sessions_dir(self) -> Path:
        """按会话分目录的逐步 / 逐次 LLM 原文：`logs/sessions/<session_id>/{steps,llm}/`。

        之所以分目录：`step-NNNN.json` / `llm-NNNN.json` 的编号是**会话内序号**，
        放在共享目录里，后一场会话会从 0001 重来、整体覆盖前一场的原文。
        """
        return self.logs_dir / "sessions"

    def session_dir(self, session_id: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z._-]+", "_", str(session_id)) or "session"
        return self.sessions_dir / safe

    @property
    def steps_dir(self) -> Path:
        """**遗留**共享目录（旧数据结构，只读；新数据写在 session_dir() 里）。"""
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
    def current_task_file(self) -> Path:
        """当前任务的**结构化真源**：`{text, images, ts}`。

        与 user-prompts.md 的分工：那个 markdown 文件是**渲染**（给人看 / 自省 /
        模型可改写），这个 JSON 是**真相**（机器读）。写任务时两者一起写；
        读「当前任务」只读这里 —— 于是读到的永远是干净原文，而不是文件的
        存储格式（`# 当前任务` 头 / `_提交于 …_` 尾 / `---` 累积块）。
        """
        return self.prompts_dir / "current-task.json"

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
    def identity_file(self) -> Path:
        """身份：智能体自己起的名字 / 格言 —— 「它是谁」的磁盘状态。

        与 limits.json 同源：磁盘即状态、每步被重读（改一下下一步生效）。
        由内置工具 `set_identity` 写、`whoami` 读；终端身份卡与工作报告也读它。
        `name` 为 null = 还没有名字（内核兜底显示 DEFAULT_AGENT_NAME）。
        """
        return self.dir / "identity.json"

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

    @property
    def workspace_meta_file(self) -> Path:
        """工作区版本戳：`{schema, kernel_version, seeds{path→sha256}, written_at}`。

        它回答一个此前无处可问的问题：**这个 `.openthree/` 是由哪个内核、按哪版
        结构铺出来的？** 升级模式（`openthree -u`）全靠它把「落后」与「没落后」
        分开；没有它，唯一能做的判断是「文件在不在」，而那分辨不出内容已过期。

        `schema` 与 `kernel_version` 分开记是两个问题：前者是**结构**变了
        （该迁移），后者是**内核**换了（该看有没有新种子）。混成一个数会让
        「只改了一句提示词」也报成结构迁移。
        """
        return self.dir / "workspace.json"

    @property
    def upgrade_dir(self) -> Path:
        """升级模式的产物：提案与快照（`upgrade/proposal-*.md`、`upgrade/<ts>/`）。"""
        return self.dir / "upgrade"

    # ---------- 必备清单（唯一真源） ----------
    def required_entries(self) -> List[WorkspaceEntry]:
        """工作区**应当**有的东西 —— `init()` 与 `scan_upgrade()` 的共同真源。

        Returns:
            声明清单；`path` 一律相对 `.openthree/`，用 `/` 分隔。
        """
        return [
            # --- 目录 ---
            WorkspaceEntry(".", "dir"),
            WorkspaceEntry("prompts", "dir"),
            WorkspaceEntry("prompts/history", "dir"),
            WorkspaceEntry("functions", "dir"),
            WorkspaceEntry("assets", "dir"),
            WorkspaceEntry("logs", "dir"),
            WorkspaceEntry("logs/sessions", "dir"),
            WorkspaceEntry("logs/events", "dir"),
            WorkspaceEntry("memory", "dir"),
            # --- 结构型种子（登记 hash：可判「原样未动」与「已被改过」） ---
            WorkspaceEntry("prompts/system-prompts.md", "seed", seed="system_prompt",
                           note="一：系统提示词"),
            WorkspaceEntry("README.md", "seed", seed="readme",
                           note="工作区路径对照表"),
            # --- 状态型种子（只判在不在，永不对齐内容） ---
            WorkspaceEntry("prompts/user-prompts.md", "state", seed="user_prompt",
                           note="当前任务的渲染层"),
            WorkspaceEntry("prompts/plan.md", "state", seed="plan",
                           note="长任务待办（归模型所有）"),
            WorkspaceEntry("context.json", "state", seed="context",
                           note="上下文策略（模型自管）"),
            WorkspaceEntry("limits.json", "state", seed="limits",
                           note="运行边界（模型自管）"),
            WorkspaceEntry("identity.json", "state", seed="identity",
                           note="身份：名字 / 格言（只有它能改）"),
            # --- 由别处逻辑生成 ---
            WorkspaceEntry("prompts/current-task.json", "derive", seed="current_task",
                           note="当前任务的结构化真源"),
        ]

    def seed_text(self, kind: str) -> str:
        """按名字取一份种子内容 —— 种子的**公开访问器**。

        外部（升级扫描、报告、测试）一律走这里，不再 `from .workspace import
        _SYSTEM_PROMPT_SEED`：私有常量一旦被外部 import，就再也没法自由重构。

        Args:
            kind: `system_prompt` / `user_prompt` / `current_task` / `plan` /
                  `context` / `limits` / `identity` / `readme`

        Returns:
            种子全文；未知名字返回空串。
        """
        if kind == "readme":
            return self._readme_text()
        return {
            "system_prompt": _SYSTEM_PROMPT_SEED,
            "user_prompt": _USER_PROMPT_SEED,
            "current_task": _CURRENT_TASK_SEED,
            "plan": _PLAN_SEED,
            "context": _CONTEXT_SEED,
            "limits": _LIMITS_SEED,
            "identity": _IDENTITY_SEED,
        }.get(kind, "")

    # ---------- 版本戳 ----------
    def read_workspace_meta(self) -> Dict[str, Any]:
        """读版本戳；缺失 / 损坏一律返回 `{}`（老工作区即此情形）。"""
        try:
            if not self.workspace_meta_file.exists():
                return {}
            doc = json.loads(self.workspace_meta_file.read_text(encoding="utf-8") or "{}")
            return doc if isinstance(doc, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def write_workspace_meta(self) -> Dict[str, Any]:
        """写版本戳：登记当前内核版本与**每份结构型种子此刻的 hash**。

        hash 记的是「铺下去的那份种子」，不是「磁盘现在这份」—— 读回时两者相比，
        相等 = 原样未动（可整文件换新），不等 = 已被改过（只能提议补丁）。
        所以这里对 seed 型条目取 `seed_text()` 的 hash，而非读文件。
        """
        seeds: Dict[str, str] = {}
        for e in self.required_entries():
            if e.kind == "seed" and e.seed:
                seeds[e.path] = sha256_text(self.seed_text(e.seed))
        doc = {
            "schema": WORKSPACE_SCHEMA,
            "kernel_version": kernel_version(),
            "seeds": seeds,
            "written_at": datetime.now().isoformat(timespec="seconds"),
        }
        atomic_write(self.workspace_meta_file,
                      json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        return doc

    # ---------- 初始化 ----------
    def init(self, force_prompts: bool = False) -> "Workspace":
        """幂等地把工作区铺好。已存在的内容不覆盖。

        **「该有什么」不在本函数里硬编码** —— 一律从 `required_entries()` 读。
        升级模式（`upgrade.scan_upgrade`）用的是同一份清单，于是「铺」与「查」
        永远不会各说各话。
        """
        entries = self.required_entries()

        for e in entries:
            if e.kind == "dir":
                (self.dir / e.path).mkdir(parents=True, exist_ok=True)

        # 结构型种子（提示词 / README）：内容由内核提供，默认不覆盖已存在的。
        for e in entries:
            if e.kind != "seed":
                continue
            target = self.dir / e.path
            force = force_prompts and e.force_overwrite
            if force or not target.exists():
                atomic_write(target, self.seed_text(e.seed))

        if not self.current_task_file.exists():
            # 新工作区：若 user-prompts.md 已有真实任务（旧工作区），迁进结构化真源；
            # 否则落种子。之后「读当前任务」只认这个 JSON，不再读 markdown 渲染层。
            self.write_current_task(_clean_task_markdown(self.read_user_prompt()),
                                    images=self.parse_images_from_prompt(
                                        self.read_user_prompt()))

        # 状态型种子（计划 / 边界 / 身份 / 上下文策略）：内容归使用方所有。
        for e in entries:
            if e.kind != "state":
                continue
            target = self.dir / e.path
            force = force_prompts and e.force_overwrite
            if force or not target.exists():
                atomic_write(target, self.seed_text(e.seed))

        # 版本戳只在「工作区首次铺成」（或显式 force_prompts 重铺种子）时写。
        # 它是**历史记录**而不是当前镜像：每次 init 都刷新会把「当初是哪一版种子
        # 铺的」覆盖成今天这一版，于是「原样未动」再也判不出来 —— 所有文件都退化成
        # `merge`，升级只能打补丁、永远不能安全地整文件换新。
        # Runtime.__init__ 每次都会调 init()，所以这条约束必须写在这里。
        if force_prompts or not self.workspace_meta_file.exists():
            self.write_workspace_meta()
        return self

    @staticmethod
    def _readme_text() -> str:
        """工作区 README 的种子内容（`.openthree/` 里那张路径对照表）。"""
        return (
            "# .openthree\n\n"
            "这是 openthree 智能体的工作区。\n\n"
            "| 路径 | 含义 |\n|---|---|\n"
            "| `prompts/system-prompts.md` | 一：系统提示词，模型可自改（旧稿存于 `prompts/history/`） |\n"
            "| `prompts/user-prompts.md` | 当前任务的渲染层（给人看，含 `# 当前任务` 头 / 累积块） |\n"
            "| `prompts/current-task.json` | 当前任务的**结构化真源**（`{text, images, ts}`）；机器读用户输入只认它 |\n"
            "| `prompts/plan.md` | 长任务待办清单，模型自己维护、每一步自动注入 |\n"
            "| `context.json` | 上下文策略：预算 / 是否自动折叠；`context_budget` 写、`context_usage` 读 |\n"
            "| `limits.json` | 运行边界：每轮步数 / 最多轮数 / 生成长度 / 工具结果宽度；`set_limits` 写、`limits_usage` 读 |\n"
            "| `identity.json` | 身份：智能体自己起的名字 / 格言；`set_identity` 写、`whoami` 读 |\n"
            "| `workspace.json` | 工作区版本戳：铺它的内核版本、结构 schema、结构型种子的 hash |\n"
            "| `prompts/history/transcript.jsonl` | 对话档案：用户的完整原始输入（只增不改，`--resume` 可续读） |\n"
            "| `prompts/history/system-*.md` | 系统提示词的历史版本（`read_prompt_version` 回看、`restore_system_prompt` 回退） |\n"
            "| `functions/*.py` | 二：python 文件即 function |\n"
            "| `logs/` | 模型全部工作记录（json / jsonl 分门别类） |\n"
            "| `memory/memory.jsonl` | 二：长期记忆（append-only）；`remember` 写、`recall_memory` 读、每轮注入 |\n"
            "| `three-report.html` | 一页看全智能体工作 |\n\n"
            "```bash\nopenthree \"你的任务\"\n```\n"
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
        """读回 user-prompts.md 的**原始 markdown**（渲染层，供自省 / read_prompts）。

        ⚠ 这是「给人看的那份」，包含 `# 当前任务` 头 / `_提交于 …_` 尾 / `---` 累积块。
        **不要拿它当「用户输入」**——它会连存储格式一起读进来。要任务正文请用
        `current_task_text()`（读结构化真源）。
        """
        if not self.user_prompt_file.exists():
            return ""
        return self.user_prompt_file.read_text(encoding="utf-8")

    def write_user_prompt(self, text: str) -> None:
        """覆写渲染层 markdown；同时把**正文**同步进结构化真源（模型可改任务）。"""
        self.user_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        self.user_prompt_file.write_text(text, encoding="utf-8")
        body = _clean_task_markdown(text)
        if body or self.current_task_file.exists():
            self.write_current_task(body, images=self.parse_images_from_prompt(text))

    def read_current_task(self) -> Dict[str, Any]:
        """读「当前任务」的结构化真源 → `{text, images, ts}`（机器唯一该读的口径）。

        文件缺失（旧工作区）时从 user-prompts.md 兜底迁移：剥掉头尾与累积块，
        取出正文与图片标记。返回的 `text` 永远是**干净原文**。
        """
        try:
            if self.current_task_file.exists():
                data = json.loads(self.current_task_file.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict):
                    imgs = data.get("images")
                    return {
                        "text": str(data.get("text") or ""),
                        "images": [str(x) for x in imgs] if isinstance(imgs, list) else [],
                        "ts": data.get("ts"),
                    }
        except Exception:  # noqa: BLE001
            pass
        # 旧工作区兜底：从 markdown 渲染层还原
        md = self.read_user_prompt()
        return {"text": _clean_task_markdown(md),
                "images": self.parse_images_from_prompt(md), "ts": None}

    def write_current_task(self, text: str, images: Optional[List[str]] = None,
                           ts: Optional[str] = None) -> Path:
        """写结构化真源（不碰 user-prompts.md —— 那由 submit / write_user_prompt 管）。"""
        doc = {
            "text": (text or "").strip(),
            "images": [str(x) for x in (images or []) if str(x).strip()],
            "ts": ts or datetime.now().isoformat(timespec="seconds"),
        }
        self.current_task_file.parent.mkdir(parents=True, exist_ok=True)
        self.current_task_file.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.current_task_file

    def current_task_text(self) -> str:
        """当前任务的**干净正文**（剥掉 markdown 头尾与图片标记）—— 读用户输入走这里。"""
        return self.strip_images_marker(self.read_current_task().get("text") or "")

    def current_task_images(self) -> List[str]:
        """当前任务携带的图片清单（结构化真源里的，或从渲染层标记兜底还原）。"""
        return list(self.read_current_task().get("images") or [])

    def has_task(self) -> bool:
        """当前是否有真实任务（结构化判据，不靠「等待用户输入」四个字猜）。"""
        return bool(self.current_task_text().strip())

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

        只取 `#` 开头的行（含层级），去掉 `<!-- -->` 存档头，并**跳过 ``` 围栏内**的
        内容。跳过围栏是必需的：代码示例里的 `# 命令行入口` 是一行 python 注释，
        不是章节标题。若不跳，自省与漂移检测会把「示例代码里出现了什么」误读成
        「提示词该有什么章节」—— 第一屏就是假警报，整份报告的可信度随之作废。
        """
        body = self.read_system_prompt() if text is None else text
        out: List[str] = []
        in_comment = False
        in_fence = False
        for line in (body or "").splitlines():
            s = line.rstrip()
            stripped = s.strip()
            # 围栏本身要先判：``` 行既不是注释也不是标题。
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
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

    # ---------- 身份 ----------
    def read_identity(self) -> Dict[str, Any]:
        """读回身份档（名字 / 格言 / 起名与改名时间 / 改名次数）。

        容错优先：文件缺失 / 损坏 / 字段类型不对，一律安静回退到默认
        （`name` = None 表示**还没有名字**，由调用方决定兜底显示）。

        Returns:
            {name, motto, born_at, updated_at, renames}；name/motto 为 Optional[str]。
        """
        identity: Dict[str, Any] = {"name": None, "motto": None, "born_at": None,
                                    "updated_at": None, "renames": 0}
        try:
            if self.identity_file.exists():
                doc = json.loads(self.identity_file.read_text(encoding="utf-8") or "{}")
                if isinstance(doc, dict):
                    name = doc.get("name")
                    motto = doc.get("motto")
                    identity["name"] = str(name).strip() if name else None
                    identity["motto"] = str(motto).strip() if motto else None
                    for key in ("born_at", "updated_at"):
                        val = doc.get(key)
                        identity[key] = str(val) if val else None
                    identity["renames"] = _as_int(doc.get("renames"))
        except Exception:  # noqa: BLE001
            pass
        return identity

    def write_identity(self, name: str, motto: str = "") -> Dict[str, Any]:
        """写下智能体的名字 / 格言（`set_identity` 的落盘入口）。

        与 `write_plan` 一样幂等覆写整份文件；`born_at` 只在**第一次**起名时记下，
        `renames` 数的是改名次数（首次起名不算改名）。

        Args:
            name: 名字（调用方已做长度/控制字符校验；这里只去首尾空白）
            motto: 格言，可为空

        Returns:
            写回后的身份档（同 read_identity）。
        """
        name = (name or "").strip()
        motto = (motto or "").strip()
        now = datetime.now().isoformat(timespec="seconds")
        old = self.read_identity()
        renamed = bool(old.get("name")) and old.get("name") != name
        doc = {
            "name": name,
            "motto": motto or None,
            "born_at": old.get("born_at") or now,
            "updated_at": now,
            "renames": _as_int(old.get("renames")) + (1 if renamed else 0),
        }
        self.identity_file.parent.mkdir(parents=True, exist_ok=True)
        self.identity_file.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.read_identity()

    def agent_name(self) -> Optional[str]:
        """当前名字；还没起名时返回 None（兜底由调用方决定）。"""
        return self.read_identity().get("name")

    # ---------- 用户输入 ----------
    def submit(self, user_input: str, append: bool = False,
               images: Optional[List[str]] = None) -> Path:
        """把用户输入同时写进**结构化真源**（current-task.json）与**渲染层**（user-prompts.md）。

        Args:
            user_input: 任务描述（干净原文，存进 current-task.json 的就是它）
            append: True 时把上一任务留在渲染层作历史；**真源只保留本轮任务**
                    （「当前任务」永远只有一条，不复读旧任务）
            images: 本轮携带的图片路径 / URL；渲染层以 HTML 注释形式附在文末，
                    **真源里以 images 列表存**，两份都由 parse/read 还原。
        """
        text = user_input.strip()
        stamp = datetime.now().isoformat(timespec="seconds")
        block = f"# 当前任务\n\n{text}\n\n_提交于 {stamp}_\n"
        if append:
            # 仅渲染层累积历史（给人看）；判据用结构化真源，不再靠字符串猜
            old = self.read_user_prompt() if self.has_task() else ""
            old = re.sub(re.escape(self._IMAGES_MARK) + r":\s*\[.*?\]\s*-->", "", old, flags=re.S)
            if old.strip():
                block = old.rstrip() + "\n\n---\n\n" + block
        block += self.images_marker(images or [])
        self.user_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        self.user_prompt_file.write_text(block, encoding="utf-8")
        self.write_current_task(text, images=images, ts=stamp)   # 真源：干净原文 + 本轮图片
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

    # ---------- functions：来源与登记簿 ----------
    def function_sources(self, include_missing: bool = False) -> List[FunctionSource]:
        """function 的来源目录清单，**按距离升序**（由近到远）—— 「从哪找」的唯一真源。

        只认 `<dir>/.openthree/functions` 这一种版式。裸的 `<dir>/functions/`
        一律不认：在 python 项目里它是常见包名，误扫等于把别人的库当成工具加载。
        不递归、不看进程 cwd（`root` 在 `__post_init__` 里已经 resolve 过）。

        这里**只枚举、不加载** —— 发现与授权是两件事（见 `lookup_functions`）。

        Args:
            include_missing: 是否把不存在的目录也列出来（`lookup` 用来说明「看过哪」）

        Returns:
            `FunctionSource` 清单，`distance` 升序；project 恒在首位。
        """
        out: List[FunctionSource] = []
        seen: set = set()

        def _add(src: FunctionSource) -> None:
            key = canon_path(src.path)
            if key in seen:          # 同一个目录（user 恰在祖先链上时会发生）
                return
            seen.add(key)
            if not include_missing and not src.path.is_dir():
                return
            out.append(src)

        _add(FunctionSource("project", "project", self.functions_dir, 0))

        depth = 0
        cur = self.root.parent
        while depth < _MAX_SOURCE_DEPTH:
            depth += 1
            _add(FunctionSource("ancestor", f"ancestor:{cur}",
                                cur / DIRNAME / "functions", depth))
            parent = cur.parent
            if parent == cur:        # 已到盘符根 / 文件系统根
                break
            cur = parent

        try:
            home = Path.home()
        except (OSError, RuntimeError):   # pragma: no cover - 环境异常
            home = None
        if home is not None:
            _add(FunctionSource("user", "user", home / DIRNAME / "functions",
                                USER_FUNCTION_DISTANCE))

        return sorted(out, key=lambda s: s.distance)

    def function_source_for(self, path: Any) -> Optional[FunctionSource]:
        """一个文件属于哪个来源目录；不属于任何一档（外部登记文件）返回 None。"""
        target = canon_path(Path(path).parent)
        for src in self.function_sources(include_missing=True):
            if canon_path(src.path) == target:
                return src
        return None

    def function_distance(self, path: Any) -> int:
        """一个文件的「距离」—— 同名裁决用的那个数（越小越近）。

        不在任何来源目录里的文件取 `REGISTERED_FUNCTION_DISTANCE`（排在 user 之后）。
        这是**兜底**：登记簿里的路径由 `Registry.load_plan` 按同一套边界复核，
        族外的条目直接拒绝加载，所以真正走到这一档的只有「传了个族外路径来问
        距离」—— 它答得出数，但那个文件不会被加载。
        """
        src = self.function_source_for(path)
        return src.distance if src is not None else REGISTERED_FUNCTION_DISTANCE

    @property
    def functions_meta_file(self) -> Path:
        """外部 function 的登记簿：`{schema, registered: [{path, sha256, ...}]}`。

        与 `workspace.json` 分开：后者是**内核的结构戳**（`init()` 与升级都会重写），
        而登记簿是**模型的决策记录**，升级不该碰它。所以它也不是
        `required_entries()` 的必备项 —— 不存在就当空表，第一次登记才创建。
        """
        return self.dir / FUNCTIONS_META_NAME

    def read_functions_meta(self) -> Dict[str, Any]:
        """读登记簿；缺失 / 损坏一律返回空表（老工作区即此情形）。"""
        empty: Dict[str, Any] = {"schema": 1, "registered": []}
        try:
            if not self.functions_meta_file.exists():
                return empty
            doc = json.loads(self.functions_meta_file.read_text(encoding="utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return empty
        if not isinstance(doc, dict):
            return empty
        regs = doc.get("registered")
        doc["registered"] = [r for r in regs
                             if isinstance(r, dict) and r.get("path")] if isinstance(regs, list) else []
        doc.setdefault("schema", 1)
        return doc

    def write_functions_meta(self, doc: Dict[str, Any]) -> Path:
        """写登记簿（原子写）。`registered` 的**顺序不是优先级** —— 优先级只由距离决定。"""
        out = dict(doc or {})
        out.setdefault("schema", 1)
        if not isinstance(out.get("registered"), list):
            out["registered"] = []
        atomic_write(self.functions_meta_file,
                     json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return self.functions_meta_file

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
        """会话 id：`session-<时间戳>-<pid>-<进程内序号>`。

        序号是必须的：同一进程（如 `-i` 交互、测试）里一秒内可以开多场会话，
        只靠「秒 + pid」会撞名，于是逐场会话的目录互相覆盖 —— 又把刚修好的
        跨会话覆盖在进程内重新引入了一遍。
        """
        return f"session-{_now_stamp()}-{os.getpid()}-{next(_SESSION_SEQ)}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workspace {self.dir}>"
