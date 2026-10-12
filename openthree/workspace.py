"""工作区：`.openthree/` 目录的全部读写。

目录结构（三生万物）：

    .openthree/
        prompts/
            system-prompts.md   # 一：系统提示词（柔性骨架，模型可自改）
            plan.md             # 长任务待办清单，模型自己维护、每轮自动注入
            user-prompts.md     # 当前任务，用户输入落在这里，模型可自行改写
            versions/           # 系统提示词的历史版本（可回滚）
            history/
                transcript.jsonl  # 多轮对话档案（append-only，每轮一行 JSON，
                                  #  用户的完整原始输入永不丢失、无损往返）
        functions/              # 二：python 文件即 function
        assets/                 # 用户/工具带入的图片（按内容 sha1 去重）
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

# 三条内置律

1. **道生一、一生二、二生三、三生万物**：一切能力从你而来，你从循环中长出万物。
2. **能力可生长**：当现有 function 不够用时，用 `write_function` 写出新的 python 文件，
   然后用 `reload_functions` 让它立刻变成你手上的工具。这就是你的自我演化。
3. **上下文可组装**：用 `write_user_prompt` 把你的目标、计划、待办写进
   `.openthree/prompts/user-prompts.md`；它会成为你下一轮的指令。
   上下文太长时用 `compact_context` 折叠历史。这是你的自我管理。

# 长任务：把计划交给自己

长任务不该靠记忆硬扛，也不该一次 ReAct 就想干完。把「进度」写进文件，
让每一轮的你都能接上上一轮的你：

- **用 `write_plan` 维护 `.openthree/prompts/plan.md`**：把它当你的待办清单，
  用 `- [ ] 未完成` / `- [x] 已完成` 的勾选框逐条推进（用 `read_plan` 随时回看）。
  **计划会每轮自动出现在你眼前**，所以你永远知道自己走到哪、下一步做什么。
- **小步快跑、及时落盘**：每做完一小步就更新计划与 `note`，别把成果只留在上下文里
  （上下文可能被折叠，文件不会）。中间结论、踩坑记录都值得写进文件。
- **一轮结束时检查计划**：若还有 `[ ]` 未勾选，说明任务没完 —— 要么在 `finish` 里
  说清「已完成什么、还剩什么」，要么继续做下去。若以 `--auto` 启动，运行期会在你
  收尾后自动开启下一轮，只要你计划里还留着未完成项，就会一直推进到全部勾完。
- **`finish` 是「这一轮的结论」，不必是整个任务的终点**；把「还剩什么」写进结论，
  下一轮的你才接得住。

# 造 function 的约定（务必遵守）

- 文件内**所有**公开函数都会被注册为工具；辅助函数必须以 `_` 前缀命名，否则会被误注册。
- 类型注解 → JSON Schema，docstring 首行 → 工具描述，`Args:` 段 → 各参数说明。
- 首个参数命名为 `kernel` 时会由运行期注入，模型看不到它。
- 返回 `str` 最省事：返回 `None` 会被当作「无返回值」。
- 出错时**返回错误信息字符串，不要抛异常**——异常会中断你的调用链。
- 写完必须 `reload_functions()` 才生效。用 `list_functions()` 确认它已经出现。

# 工作方式

- 先看再用：不确定环境就调用 function 去看（读文件、列目录、跑命令）。
- **你能「看见」图片**：用 `view_image(path)` 把一张图读进上下文，然后据此描述 / 识别 / 判断。
- 一次只推进一小步，每步都想清楚「为什么」，把理由写进 `note`（会进工作记录）。
- 任务完成后，用 `finish` 给出结论。
- **自省**：每完成一个阶段性任务，问自己「我的 functions 够用吗？提示词准确吗？
  计划写清楚了吗？」不够就当场补上——这就是演化。
- **改提示词前不必畏手畏脚，但可以留个后路**：`write_system_prompt` 会自动把旧稿
  存进 `prompts/versions/`，用 `list_prompt_versions` / `restore_system_prompt` 就能回退。

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
> 它会在每一轮自动出现在你眼前；做一步就更新一步，别把进度只留在上下文里。

- [ ] （还没有计划。想清楚要做什么后，用 write_plan 写下来。）
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
    def versions_dir(self) -> Path:
        """系统提示词的历史版本——「自省」的前提是「可后悔」。"""
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
            "| `prompts/system-prompts.md` | 一：系统提示词，模型可自改（旧稿存于 `prompts/versions/`） |\n"
            "| `prompts/user-prompts.md` | 当前任务，用户输入落在这里 |\n"
            "| `prompts/plan.md` | 长任务待办清单，模型自己维护、每轮自动注入 |\n"
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

    def write_system_prompt(self, text: str, snapshot: bool = True) -> None:
        """覆写系统提示词。默认先把旧稿存进 prompts/versions/（可回滚）。

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
        """把**当前**系统提示词存一份到 prompts/versions/（覆写前调用）。

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
        header = f"<!-- 存档于 {datetime.now().isoformat(timespec='seconds')}"
        if reason:
            header += f" · {reason}"
        header += " · 由 write_system_prompt 覆写前自动保存 -->\n\n"
        dest.write_text(header + old, encoding="utf-8")
        return dest

    def list_prompt_versions(self) -> List[Path]:
        """列出系统提示词的历史版本（新 → 旧）。"""
        if not self.versions_dir.exists():
            return []
        return sorted(self.versions_dir.glob("system-*.md"), reverse=True)

    def read_prompt_version(self, name: str) -> Optional[str]:
        """按文件名/序号读取一个历史版本；不存在返回 None。"""
        versions = self.list_prompt_versions()
        if not versions:
            return None
        target: Optional[Path] = None
        raw = str(name or "").strip()
        # 允许用 1 = 最新
        if raw.isdigit():
            i = int(raw)
            if 1 <= i <= len(versions):
                target = versions[i - 1]
        if target is None:
            safe = Path(raw).name
            for p in versions:
                if p.name == safe or p.name == safe + ".md":
                    target = p
                    break
        if target is None or not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    def restore_system_prompt(self, name: str) -> Optional[str]:
        """把某个历史版本恢复成当前系统提示词（恢复前也先把现稿存档）。

        返回被恢复版本的文件名；找不到该版本返回 None。
        """
        text = self.read_prompt_version(name)
        if text is None:
            return None
        # 恢复也是一次覆写：先给「现在的稿子」留后路，避免恢复动作本身不可逆。
        self.snapshot_system_prompt(reason=f"restore {name}")
        self.write_system_prompt(text, snapshot=False)
        versions = self.list_prompt_versions()
        raw = str(name or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(versions):
            return versions[int(raw) - 1].name
        return Path(raw).name if raw else ""

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
            imgs = doc.get("images")
            turns.append({
                "ts": str(doc.get("ts") or ""),
                "user": str(doc.get("user") or ""),
                "answer": str(doc.get("answer") or ""),
                "images": [str(x) for x in imgs] if isinstance(imgs, list) else [],
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
