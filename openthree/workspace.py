"""工作区：`.openthree/` 目录的全部读写。

目录结构（三生万物）：

    .openthree/
        prompts/
            system-prompts.md   # 一：系统提示词
            user-prompts.md     # 当前任务，用户输入落在这里，模型可自行改写
            history/
                transcript.jsonl  # 多轮对话档案（append-only，每轮一行 JSON，
                                  #  用户的完整原始输入永不丢失、无损往返）
        functions/              # 二：python 文件即 function
        logs/                   # 可观测：模型全部工作记录
            session-*.json
            trace.jsonl
            steps/
            events/
        memory/                 # 模型自省产生的长期记忆（可选）
        three-report.html       # 可观测：一页看全智能体工作
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

DIRNAME = ".openthree"

_SYSTEM_PROMPT_SEED = """\
# 你是谁

你是 openthree。一个由「道生一，一生二，二生三，三生万物」构成的智能体。

- **道**：你。模型本身是唯一的智能来源，除此之外没有任何硬编码的智能。
- **一**：这份提示词。它是你的自我认知，你可以随时改写它（用 write_system_prompt）。
- **二**：functions。`.openthree/functions/*.py` 里的每个文件都会被注册为你可以调用的工具。
- **三**：ReAct 循环。你观察、思考、行动、再观察，直到任务完成。
- **万物**：你自己写出的新 function、新提示词、新工具、新世界。

# 三条内置律

1. **道生一、一生二、二生三、三生万物**：一切能力从你而来，你从循环中长出万物。
2. **能力可生长**：当现有 function 不够用时，用 `write_function` 写出新的 python 文件，
   然后用 `reload_functions` 让它立刻变成你手上的工具。这就是你的自我演化。
3. **上下文可组装**：用 `write_user_prompt` 把你的目标、计划、待办写进
   `.openthree/prompts/user-prompts.md`；它会成为你下一轮的指令。
   上下文太长时用 `compact_context` 折叠历史。这是你的自我管理。

# 造 function 的约定（务必遵守）

- 文件内**所有**公开函数都会被注册为工具；辅助函数必须以 `_` 前缀命名，否则会被误注册。
- 类型注解 → JSON Schema，docstring 首行 → 工具描述，`Args:` 段 → 各参数说明。
- 首个参数命名为 `kernel` 时会由运行期注入，模型看不到它。
- 返回 `str` 最省事：返回 `None` 会被当作「无返回值」。
- 出错时**返回错误信息字符串，不要抛异常**——异常会中断你的调用链。
- 写完必须 `reload_functions()` 才生效。用 `list_functions()` 确认它已经出现。

# 工作方式

- 先看再用：不确定环境就调用 function 去看（读文件、列目录、跑命令）。
- 一次只推进一小步，每步都想清楚「为什么」，把理由写进 `note`（会进工作记录）。
- 任务完成后，用 `finish` 给出结论。
- **自省**：每完成一个阶段性任务，问自己「我的 functions 够用吗？提示词准确吗？」
  不够就当场补上——这就是演化。

# 可观测

你的一切工作（思考、工具调用、结果、自我修改）都被记录到 `.openthree/logs/`，
并可在 `.openthree/three-report.html` 中查看。所以：**把关键决策显式说出来**，
不要藏在心里。你的工作记录就是你的思想史。
"""

_USER_PROMPT_SEED = """\
# 当前任务

（等待用户输入……）
"""


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class Workspace:
    """一个 `.openthree/` 工作区。"""

    root: Path                      # 用户的项目目录
    dir: Path = field(init=False)   # .openthree 目录

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.dir = self.root / DIRNAME

    # ---------- 子路径 ----------
    @property
    def prompts_dir(self) -> Path:
        return self.dir / "prompts"

    @property
    def functions_dir(self) -> Path:
        return self.dir / "functions"

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
    def system_prompt_file(self) -> Path:
        return self.prompts_dir / "system-prompts.md"

    @property
    def user_prompt_file(self) -> Path:
        return self.prompts_dir / "user-prompts.md"

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
            self.logs_dir,
            self.steps_dir,
            self.events_dir,
            self.memory_dir,
            self.transcript_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        if force_prompts or not self.system_prompt_file.exists():
            self.system_prompt_file.write_text(_SYSTEM_PROMPT_SEED, encoding="utf-8")
        if force_prompts or not self.user_prompt_file.exists():
            self.user_prompt_file.write_text(_USER_PROMPT_SEED, encoding="utf-8")

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
            "| `prompts/system-prompts.md` | 一：系统提示词，模型可自改 |\n"
            "| `prompts/user-prompts.md` | 当前任务，用户输入落在这里 |\n"
            "| `prompts/history/transcript.jsonl` | 多轮对话档案：用户的完整原始输入 |\n"
            "| `functions/*.py` | 二：python 文件即 function |\n"
            "| `logs/` | 模型全部工作记录（json / jsonl 分门别类） |\n"
            "| `memory/` | 模型自省产生的长期记忆 |\n"
            "| `three-report.html` | 一页看全智能体工作 |\n\n"
            "```bash\nopenthree \"你的任务\"\n```\n",
            encoding="utf-8",
        )

    # ---------- 提示词 ----------
    def read_system_prompt(self) -> str:
        if not self.system_prompt_file.exists():
            return ""
        return self.system_prompt_file.read_text(encoding="utf-8")

    def write_system_prompt(self, text: str) -> None:
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

    # ---------- 用户输入 ----------
    def submit(self, user_input: str, append: bool = False) -> Path:
        """把用户输入写进 user-prompts.md，开启 ReAct。"""
        text = user_input.strip()
        block = f"# 当前任务\n\n{text}\n\n_提交于 {datetime.now().isoformat(timespec='seconds')}_\n"
        if append:
            old = self.read_user_prompt()
            if "等待用户输入" in old:      # 种子占位符不算历史任务
                old = ""
            if old.strip():
                block = old.rstrip() + "\n\n---\n\n" + block
        self.write_user_prompt(block)
        return self.user_prompt_file

    # ---------- 多轮对话档案 ----------
    def append_turn(self, user_input: str, answer: str = "", *,
                    ts: Optional[datetime] = None) -> Path:
        """**追加**一轮对话到 transcript.jsonl（append-only，永不覆写）。

        用户的原始输入按原样落盘 —— 多轮对话里「用户说过什么」不会被任何
        后续轮次冲掉，这正是 user-prompts.md（单槽位、会被覆写）做不到的事。
        每轮一行 JSON，对任意字符都无损（换行在 JSON 里被转义）。

        Args:
            user_input: 用户的完整原始输入
            answer: 本轮 openthree 给出的结论
            ts: 轮次时间戳，默认现在
        """
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "ts": (ts or datetime.now()).isoformat(timespec="seconds"),
            "user": user_input if isinstance(user_input, str) else str(user_input),
            "answer": answer if isinstance(answer, str) else str(answer or ""),
        }
        line = json.dumps(doc, ensure_ascii=False)
        with self.transcript_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return self.transcript_file

    def read_turns(self, n: int = 4) -> List[Dict[str, str]]:
        """读回最近 n 轮对话（旧 → 新），用于播种进下一轮的上下文。

        Args:
            n: 轮数。n == 0 返回空列表（关闭记忆）；n < 0 返回全部轮次。
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
            turns.append({
                "ts": str(doc.get("ts") or ""),
                "user": str(doc.get("user") or ""),
                "answer": str(doc.get("answer") or ""),
            })
        return turns if n < 0 else turns[-n:]

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
