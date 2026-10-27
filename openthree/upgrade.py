"""升级模式：把当前 `.openthree/` 与本机内核的差距算清楚。

`openthree -u` 的全部判断都在这里，而且**不调模型**：`scan_upgrade()` 是纯函数
（只读工作区、只读种子，不写任何文件），所以它能进 CI（`-u --check`），也能被
测试逐项断言。模型的职责是「拿着这份清单给建议」，不是「自己算差了什麼」。

## 判据（全模块唯一）

| 类 | 判据 | 例 |
|---|---|---|
| `missing` | 内核声明必备，工作区没有 | 没有 `memory/`；提示词缺「命令行入口」整节 |
| `outdated` | 有，但落后于本机内核 | `workspace.json.kernel_version` 更旧；种子内容已更新 |
| `nonconforming` | 有、也不算旧，但不符合当前规范 | `functions/x.py` 缺模块 docstring / 无命令行尾钩 |
| **`extra`** | 工作区有、内核种子里没有 | **永不产生动作。** 那是个体长的东西 |

最后一行是这套设计的地基。本仓库自己就是病例：种子里有「命令行入口」整节而
磁盘没有；磁盘有「写给长存的读者」整节而种子没有 —— 同一个 diff 里，一条该抄
回去、一条该留着。任何硬编码的升级器都判不了这一刀，所以机械层只报「缺 / 旧 /
不合规」，`extra` 只作为信息列出、绝不进 `items`。

## 动作与风险

`create`（建缺失文件）/ `fill`（补缺失段落）/ `replace`（整文件换新）/
`merge`（只能打补丁）/ `advise`（只建议，不动作）/ `locked`（只报告，内核不擅自动手）。

`replace` 与 `merge` 的分界线是**版本戳里记的种子 hash**：磁盘当前 hash 等于
「当初铺的那份」，说明人没动过 → 可以整文件换新；不等，说明已被改过 → 只能
提议补丁，绝不整文件覆盖。老工作区没有版本戳时一律按 `merge` 处理 —— 恰好是
正确且保守的默认。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .builtins import function_conformance
from .workspace import (WORKSPACE_SCHEMA, Workspace, atomic_write, kernel_version,
                        sha256_text)

# 风险从低到高 —— 用于排序与「哪些可以不问人」
RISK_ORDER = {"low": 0, "mid": 1, "high": 2}

# 动作的中文标签（报告与提案共用，避免两处各写一份措辞）
ACTION_LABEL = {
    "create": "新建",
    "fill": "补段落",
    "replace": "整文件换新",
    "merge": "打补丁（需人确认）",
    "advise": "只建议",
    "locked": "只报告",
}

KIND_LABEL = {
    "missing": "缺",
    "outdated": "旧",
    "nonconforming": "不合规",
}

# 升级判据的「人话版」。`build_upgrade_task()`（交给模型）与 `upgrade_guide()`
# （模型中途查阅）都从这里取，不各写一份 —— 否则同一件事会有两处措辞、
# 迟早一处改了一处没改。
RULES: tuple = (
    "- **只补「缺」，不删「多」**：盘上有、种子没有的内容是个体的自我演化，删了就是毁掉它。",
    "- 同一次升级里，一条该抄回去、一条该留着，是正常的 —— 用判断，别用规则。",
    "- 状态文件（`plan.md` / `limits.json` / `identity.json` / `context.json`）"
    "**永不对齐内容**：它们归使用者所有。",
)


@dataclass
class DriftItem:
    """一条漂移：一个问题 + 一个建议动作 + 一个风险等级。

    `details` 是这条问题的逐项明细（缺哪些章节、哪几处不合规）。报告与提案都
    直接引用它，于是「扫出来的」与「给人看的」永远同源。
    """

    kind: str                 # missing / outdated / nonconforming
    target: str               # 相对 .openthree/ 的路径（或 workspace.json 这类状态文件）
    action: str               # create / fill / replace / merge / advise / locked
    risk: str                 # low / mid / high
    detail: str               # 一句话说清「差在哪」
    details: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "action": self.action,
                "risk": self.risk, "detail": self.detail, "details": list(self.details)}


@dataclass
class UpgradeReport:
    """一次扫描的完整结果 —— 结构化的真源，文本报告只是它的渲染。"""

    workspace_dir: str = ""
    kernel_version: str = ""            # 本机安装的内核
    workspace_kernel_version: str = ""  # 铺出这个工作区的内核（可能为空 = 无版本戳）
    schema: int = WORKSPACE_SCHEMA
    workspace_schema: int = 0
    items: List[DriftItem] = field(default_factory=list)
    extras: List[str] = field(default_factory=list)
    scanned_at: str = ""

    @property
    def has_drift(self) -> bool:
        """有没有**需要处理**的项（`advise` / `locked` 也算，因为人要看到）。"""
        return bool(self.items)

    def needing_consent(self) -> List[DriftItem]:
        """必须人确认才能落盘的项（`replace` / `merge`）。"""
        return [i for i in self.items if i.action in ("replace", "merge")]

    def auto_safe(self) -> List[DriftItem]:
        """可以自动做的项 —— 只有「新建缺失」与「只建议」。"""
        return [i for i in self.items if i.action in ("create", "advise")]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for i in self.items:
            out[i.kind] = out.get(i.kind, 0) + 1
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "workspace_dir": self.workspace_dir,
            "kernel_version": self.kernel_version,
            "workspace_kernel_version": self.workspace_kernel_version,
            "schema": self.schema,
            "workspace_schema": self.workspace_schema,
            "counts": self.counts(),
            "items": [i.as_dict() for i in self.items],
            "extras": list(self.extras),
            "scanned_at": self.scanned_at,
        }


def _ver_tuple(text: str) -> tuple:
    """把 `0.1.7` 拆成可比较的元组；解析不出的段一律按 0。"""
    parts: List[int] = []
    for chunk in str(text or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _seed_drift(ws: Workspace, entry: Any, meta: Dict[str, Any],
                extras: List[str]) -> List[DriftItem]:
    """对一份**结构型种子**（提示词 / README）判漂移。

    三条判据按优先级：

    1. 磁盘 hash == 当前种子 hash → 完全一致，无动作。
    2. 磁盘 hash == 版本戳里记的种子 hash → 人没动过，内核种子变新了 → `replace`。
    3. 否则 → 已被改过（或无版本戳）→ `merge`，绝不整文件覆盖。
    """
    out: List[DriftItem] = []
    path = ws.dir / entry.path
    try:
        disk = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [DriftItem("outdated", entry.path, "merge", "high",
                          f"文件存在但读不出来：{exc}")]

    disk_hash = sha256_text(disk)
    seed_text = ws.seed_text(entry.seed)
    seed_hash = sha256_text(seed_text)
    recorded = (meta.get("seeds") or {}).get(entry.path)

    if disk_hash == seed_hash:
        return out                      # 与当前内核一字不差

    if recorded and disk_hash == recorded:
        out.append(DriftItem(
            "outdated", entry.path, "replace", "mid",
            "内核种子已更新，而这份文件仍是当初原样 —— 可整文件换成新版"))
    elif recorded:
        out.append(DriftItem(
            "outdated", entry.path, "merge", "high",
            "内核种子已更新，但这份文件已被改过 —— 只能打补丁，不能整文件覆盖"))
    else:
        out.append(DriftItem(
            "outdated", entry.path, "merge", "high",
            "工作区没有版本戳（老工作区），无法判断这份文件是否被改过 —— 按保守方式只打补丁"))

    # 章节级明细：只有提示词有「章节」这个概念，README 只做整文件判定。
    if entry.seed == "system_prompt":
        seed_sections = ws.analysis_sections(seed_text)
        disk_sections = ws.analysis_sections(disk)
        missing = [s for s in seed_sections if s not in disk_sections]
        surplus = [s for s in disk_sections if s not in seed_sections]
        if missing:
            out[-1].details.extend(f"缺章节：{s}" for s in missing)
        if surplus:
            # 多出来的章节**不进 items**：它是个体长的东西，动了就是毁掉它的自我演化。
            extras.extend(f"{entry.path}：多出章节「{s}」—— 个体所有，不产生动作"
                          for s in surplus)
    return out


def scan_upgrade(ws: Workspace, *, include_functions: bool = True) -> UpgradeReport:
    """扫一遍工作区，出结构化漂移清单。**纯函数：不写盘、不调模型。**

    Args:
        ws: 目标工作区
        include_functions: 是否体检 `functions/*.py` 的规范化（需要解析源码，
            量大时可在 CI 里关掉）

    Returns:
        `UpgradeReport`；`items` 只含「缺 / 旧 / 不合规」，`extras` 只含信息。
    """
    meta = ws.read_workspace_meta()
    report = UpgradeReport(
        workspace_dir=str(ws.dir),
        kernel_version=kernel_version(),
        workspace_kernel_version=str(meta.get("kernel_version") or ""),
        schema=WORKSPACE_SCHEMA,
        workspace_schema=int(meta.get("schema") or 0) if meta else 0,
        scanned_at=datetime.now().isoformat(timespec="seconds"),
    )

    # --- 1. 必备项：目录 / 种子 / 状态 / 派生 ---
    for entry in ws.required_entries():
        target = ws.dir / entry.path
        exists = target.exists()
        if not exists:
            risk = "low" if entry.kind in ("dir", "seed", "state", "derive") else "mid"
            report.items.append(DriftItem(
                "missing", entry.path, "create", risk,
                f"{entry.note or entry.path} 不存在",
                [f"内核声明它必备（kind={entry.kind}）"]))
            continue
        if entry.kind == "seed":
            report.items.extend(_seed_drift(ws, entry, meta, report.extras))
        # state / derive：存在即合规。内容归使用方所有，升级绝不对齐它 ——
        # 否则一次升级就等于清空计划、重置边界、抹掉名字。

    # --- 2. 版本戳本身 ---
    if not meta:
        report.items.append(DriftItem(
            "outdated", "workspace.json", "create", "low",
            "工作区没有版本戳：无法判断它由哪个内核、按哪版结构铺的",
            ["补写版本戳不改变任何既有内容，只是让以后的比对有据可依"]))
    else:
        wschema = report.workspace_schema
        if wschema < WORKSPACE_SCHEMA:
            report.items.append(DriftItem(
                "outdated", "workspace.json", "locked", "high",
                f"结构 schema {wschema} < 内核 {WORKSPACE_SCHEMA}：需要结构迁移",
                ["结构迁移会动到既有内容的语义，-u 只报告、不擅自动手"]))
        elif wschema > WORKSPACE_SCHEMA:
            report.items.append(DriftItem(
                "outdated", "workspace.json", "locked", "high",
                f"工作区 schema {wschema} 比本机内核 {WORKSPACE_SCHEMA} 新："
                "这个工作区是本机内核之后的版本铺的",
                ["先升级内核（pip install -U openthree），-u 不降级"]))
        wkv = report.workspace_kernel_version
        kv = report.kernel_version
        if wkv and kv and _ver_tuple(wkv) != _ver_tuple(kv):
            newer = _ver_tuple(wkv) > _ver_tuple(kv)
            report.items.append(DriftItem(
                "outdated", "workspace.json", "advise", "low",
                f"工作区由内核 {wkv} 铺出，本机内核是 {kv}"
                + ("（工作区更新：先升级内核）" if newer else "（内核更新：可考虑吸收新种子）")))

    # --- 3. 自造 function 的规范化 ---
    if include_functions:
        for path in ws.list_function_files():
            rep = function_conformance(path)
            issues = rep.get("issues") or []
            if not issues:
                continue
            report.items.append(DriftItem(
                "nonconforming", f"functions/{path.name}", "advise", "mid",
                f"{len(issues)} 项不符合当前 function 规范", list(issues)))

    # 稳定的阅读顺序：风险高的在前，同类按路径排。
    report.items.sort(key=lambda i: (-RISK_ORDER.get(i.risk, 0), i.kind, i.target))
    return report


def render_report(report: UpgradeReport, *, as_json: bool = False) -> str:
    """把扫描结果渲染成人能读的文本（或 JSON）。

    Args:
        report: `scan_upgrade()` 的结果
        as_json: 出单行 JSON（给脚本 / 其他 agent 用）

    Returns:
        报告文本。
    """
    if as_json:
        return json.dumps(report.as_dict(), ensure_ascii=False)

    lines: List[str] = ["# openthree 升级检查", ""]
    lines.append(f"- 工作区：{report.workspace_dir}")
    wkv = report.workspace_kernel_version or "（无版本戳）"
    lines.append(f"- 内核版本：工作区 {wkv} → 本机 {report.kernel_version}")
    lines.append(f"- 结构 schema：工作区 {report.workspace_schema or '（无）'}"
                 f" → 内核 {report.schema}")
    lines.append("")
    lines.append("> 语义边界：`-u` **不会**让你的 openthree 变新 —— 那是 "
                 "`pip install -U openthree`。它只让**这个工作区**追平本机已经装着的内核。")
    lines.append("")

    if not report.items and not report.extras:
        lines.append("## ✅ 没有发现漂移")
        return "\n".join(lines)

    counts = report.counts()
    if report.items:
        summary = "，".join(f"{KIND_LABEL.get(k, k)} {v}" for k, v in
                            sorted(counts.items(), key=lambda kv: kv[0]))
        lines.append(f"## 发现 {len(report.items)} 项漂移（{summary}）")
        lines.append("")
        for item in report.items:
            lines.append(f"### [{KIND_LABEL.get(item.kind, item.kind)}] {item.target}")
            lines.append(f"- 建议：**{ACTION_LABEL.get(item.action, item.action)}**"
                         f"（风险 {item.risk}）")
            lines.append(f"- 说明：{item.detail}")
            for d in item.details:
                lines.append(f"  - {d}")
            lines.append("")
        need = report.needing_consent()
        safe = report.auto_safe()
        lines.append(f"- 需人确认后才能落盘：{len(need)} 项")
        lines.append(f"- 可自动处理（新建 / 只建议）：{len(safe)} 项")
    else:
        lines.append("## ✅ 没有需要处理的漂移")

    if report.extras:
        lines.append("")
        lines.append(f"## 多出来的内容（{len(report.extras)} 项，**不产生动作**）")
        lines.append("")
        lines.append("> 这些是个体长的东西 —— 升级只报「缺」，从不删「多」。")
        lines.append("")
        for x in report.extras:
            lines.append(f"- {x}")
    return "\n".join(lines)


def upgrade_playbook(workspace_dir: str) -> List[str]:
    """升级规程正文 —— 交给模型的首次任务与 `upgrade_guide` 工具**共用这一份**。

    分成两层是这道闸门的关键：提案给人读，待应用区给内核搬。模型的每一次
    「写文件」都停在待应用区，真正落到既有路径上必须由人敲 `:apply`。

    Args:
        workspace_dir: `.openthree/` 的绝对路径

    Returns:
        规程的行清单（markdown）。
    """
    base = Path(workspace_dir)
    return [
        "## 两层产物：提案给人看，待应用区给内核搬",
        "",
        f"工作区目录：`{base}`",
        "",
        "**第一层 · 提案（给人读）** —— 每项需要确认的改动写一份到 "
        f"`{base / 'upgrade' / 'proposal-<时间戳>.md'}`：",
        "",
        "```markdown",
        "## <目标路径>",
        "",
        "**现状**：（现在是什么样，关键片段引用原文）",
        "**建议**：（改成什么样，说清保留了什么、吸收了什么）",
        "**影响**：（会改变什么行为；人需要知道的风险）",
        "**怎么确认**：（人该怎么核对这一条）",
        "```",
        "",
        "**第二层 · 待应用区（内核据此落盘）** —— 拿到人的同意后，用 `write_file` 把",
        "**完整的最终内容**写到：",
        "",
        "```",
        f"{base / 'upgrade' / 'stage' / '<相对路径>'}",
        "```",
        "",
        "其中 `<相对路径>` 是相对 `.openthree/` 的路径，例如"
        " `prompts/system-prompts.md` 或 `README.md`。",
        "",
        "**为什么分两层**：内核只有在人敲 `:apply` 时才把待应用区搬到真实位置 ——",
        "模型动手与文件变化之间始终隔着一道人确认。",
        "",
        "- **不要**直接用 `write_system_prompt` / `write_user_prompt` / `write_plan`",
        "  这类工具去改既有文件：那会绕过确认闸门，等于没人确认就改了他的工作区。",
        "- 待应用区里放的是**完整新内容**（不是 diff）—— 内核整文件原子替换。",
        "- 受保护路径不许进待应用区：`logs/`、`memory/`、`assets/`、`prompts/history/`、",
        "  `three-report.html`、`upgrade/` 自身（内核会拒绝）。",
        "- 人可以逐条回「同意 / 不同意 / 改成…」。一次只处理一两项，别一口气全写完。",
        "",
        "## 判据提醒",
        "",
        *RULES,
        "",
    ]


def write_playbook(ws: Workspace) -> Path:
    """把升级规程写成 `upgrade/PLAYBOOK.md` —— 可随时重读的**参考资料**。

    不做成内置工具是刻意的：内置工具的 schema 会出现在**每一次**请求里，
    而这条规程只在升级模式用得上。写成文件则平时零成本 —— 模型需要时
    `read_file` 一次即可（上下文被折叠后也能重读回来）。

    Args:
        ws: 工作区

    Returns:
        写好的文件路径。
    """
    path = ws.upgrade_dir / "PLAYBOOK.md"
    body = "\n".join([
        "# 升级规程（upgrade 模式）",
        "",
        "> 这份文件由 `openthree -u` 写下。上下文被折叠、或你忘了规矩时，",
        "> 用 `read_file` 重读它。它是参考资料，不是新指令。",
        "",
        *upgrade_playbook(str(ws.dir)),
        "## 命令（由内核处理，不是你的事）",
        "",
        "- `:staged` —— 列出待应用区的改动（人看的）",
        "- `:apply` —— 人确认后落盘（先打快照，再逐个原子写）",
        "- `:undo` —— 按快照把上一次升级整体还原",
        "",
    ])
    atomic_write(path, body)
    return path


def build_upgrade_task(report: UpgradeReport, *, dry_run: bool = False) -> str:
    """把扫描结果变成**交给模型的升级任务正文**（`-u` 进交互时的首条任务）。

    内核只描述「差了什么、界在哪」，不替模型拟结论：具体怎么补、哪些该吸收
    哪些该保留，由模型结合项目实际给建议，人点头才落盘。

    Args:
        report: 扫描结果
        dry_run: 只出提案、不落盘

    Returns:
        任务正文（markdown）。
    """
    lines: List[str] = [
        "# 升级任务：把本工作区追平本机内核",
        "",
        "你正在 upgrade 模式。内核已经算好了一份**确定性清单**（下面就是），"
        "你的职责不是重新扫描，而是**拿着它做判断与沟通**。",
        "",
    ]
    if not report.items:
        lines.extend([
            "## 结论：没有发现漂移",
            "",
            "工作区已经与本机内核一致。请复核一遍 `extras`（多出来的内容）"
            "——那是它自己长出来的东西，**不要删**。然后简要汇报即可。",
            "",
        ])
    else:
        lines.extend([
            "## 内核扫出的漂移清单",
            "",
            render_report(report),
            "",
        ])

    lines.extend([
        "## 你的职责",
        "",
        "1. **逐条解释**：这一项差在哪、为什么值得改、不改会怎样。别念清单，要说人话。",
        "2. **给具体建议**：缺的段落该补在什么位置？已改过的文件该保留哪些、吸收哪些？"
        "参考项目实际情况（`read_file` / `search_files` 去看）。",
        "3. **征求补充**：人对自己的项目知道的比你多。明确问「这条要不要做 / 有没有我漏掉的」。",
        "4. **不要擅自落盘**：",
        "   - `advise`（只建议）：解释清楚即可，要不要动手由人定。",
        "   - `replace` / `merge`：**必须先拿到人的明确同意**。同意后按下面的规程写提案。",
        "   - `create`（新建缺失）：可做，但要说一声你建了什么。",
        "   - `locked`：只报告，不要试图绕过（结构迁移 / 内核版本落后不归本模式处理）。",
        "",
    ])
    lines.extend(upgrade_playbook(report.workspace_dir))
    if dry_run:
        lines.extend(["## 本次是 --dry-run", "",
                      "只出提案，不要落盘。说完方案就收尾。", ""])
    lines.extend(["准备好了就开始：先用人话汇报一边全局，再逐条推进。"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 待应用区（stage）
# 「人确认后才落盘」的物理实现：模型把**拟好的完整新内容**写进
# `upgrade/stage/<相对路径>`，由人敲 `:apply` 才由内核搬运到真实位置。
# 若允许模型直接用 `write_system_prompt` 之类的工具落盘，这道闸门就不存在了 ——
# 而那正是本模式唯一不能省的东西：模型负责提议，人负责批准，内核负责搬运。

# 内核绝不代劳的路径：可观测档案（只增不改）、资产、待应用区自身、历史版本、报告。
# 它们要么是 append-only 的审计痕迹，要么是升级动作自己的容器 —— 让一次升级去改
# 审计痕迹，等于让被审计者改写审计日志。
_PROTECTED_PREFIXES = ("logs/", "memory/", "assets/", "upgrade/",
                       "prompts/history/", "prompts/versions/")
_PROTECTED_FILES = ("three-report.html",)


def stage_dir(ws: Workspace) -> Path:
    """待应用区：模型在这里写出拟好的新内容，人 `:apply` 之后才生效。"""
    return ws.upgrade_dir / "stage"


def is_protected(rel: str) -> bool:
    """这条路径是否被升级模式禁止改动。

    Args:
        rel: 相对 `.openthree/` 的路径

    Returns:
        True = 受保护，不许升级动作碰它。
    """
    rel = str(rel).replace("\\", "/").strip().lstrip("/")
    if rel in _PROTECTED_FILES:
        return True
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in _PROTECTED_PREFIXES)


def safe_rel(rel: str) -> Optional[str]:
    """把一条相对路径规范化；越界（`..` / 绝对路径）或受保护则返回 None。

    Args:
        rel: 待应用的相对路径

    Returns:
        规范化后的路径，或 None（非法 / 受保护）。
    """
    rel = str(rel).replace("\\", "/").strip()
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    if is_protected(rel):
        return None
    return rel


def list_staged(ws: Workspace) -> List[str]:
    """列出待应用区里已拟好的文件（相对 `.openthree/` 的路径，已排序）。

    Args:
        ws: 工作区

    Returns:
        相对路径清单；没有则为空。
    """
    root = stage_dir(ws)
    if not root.exists():
        return []
    out: List[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if safe_rel(rel):
            out.append(rel)
    return out


@dataclass
class ApplyResult:
    """一次 `:apply` / `:undo` 的结果。"""

    snapshot_dir: str = ""
    applied: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines: List[str] = []
        if self.applied:
            lines.append(f"已处理 {len(self.applied)} 个文件：")
            lines.extend(f"- {p}" for p in self.applied)
        if self.errors:
            lines.append(f"失败 {len(self.errors)} 项：")
            lines.extend(f"- {e}" for e in self.errors)
        if self.snapshot_dir:
            lines.append(f"快照：{self.snapshot_dir}")
        return "\n".join(lines) if lines else "没有需要处理的文件。"


def apply_staged(ws: Workspace, *, dry_run: bool = False) -> ApplyResult:
    """把待应用区里的文件搬到真实位置：先打快照，再逐个原子写。

    两阶段是刻意的：**先备份、后覆写**。若中途失败，已成功的项保持、失败项记录，
    下次可以从断点续 —— 不做「全有或全无」，那会在失败时把可用的中间态一起丢掉。

    Args:
        ws: 工作区
        dry_run: 只演练、不落盘（不备份、不写、不清待应用区）

    Returns:
        `ApplyResult`；`applied` 是成功搬运的路径，`errors` 是逐项失败原因。
    """
    res = ApplyResult()
    staged = list_staged(ws)
    if not staged:
        return res

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = ws.upgrade_dir / ts
    entries: List[Dict[str, Any]] = []

    for rel in staged:
        src = stage_dir(ws) / rel
        dst = ws.dir / rel
        try:
            content = src.read_text(encoding="utf-8")
        except OSError as exc:
            res.errors.append(f"{rel}：读不了待应用文件（{exc}）")
            continue

        existed = dst.exists()
        backup_rel = ""
        if existed:
            backup_rel = f"files/{rel}"
            backup = snap / backup_rel
            try:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup)
            except OSError as exc:
                # 备份失败就**不许**覆写 —— 没有退路的改动不该发生。
                res.errors.append(f"{rel}：备份失败，已跳过（{exc}）")
                continue

        entries.append({"path": rel, "existed": existed, "backup": backup_rel})

        if dry_run:
            res.applied.append(rel)
            continue
        try:
            atomic_write(dst, content)
            res.applied.append(rel)
        except OSError as exc:
            res.errors.append(f"{rel}：写入失败（{exc}）")

    if dry_run:
        return res

    if entries:
        manifest = {
            "ts": ts,
            "kernel_version": kernel_version(),
            "written_at": datetime.now().isoformat(timespec="seconds"),
            "entries": entries,
        }
        atomic_write(snap / "manifest.json",
                     json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        res.snapshot_dir = str(snap)

    # 搬走成功的那些，待应用区只留下失败的（便于重试）。
    for rel in res.applied:
        try:
            (stage_dir(ws) / rel).unlink()
        except OSError:
            pass
    return res


def list_snapshots(ws: Workspace) -> List[Path]:
    """列出升级快照目录（新 → 旧），只含带 manifest 的那些。

    Args:
        ws: 工作区

    Returns:
        快照目录清单。
    """
    if not ws.upgrade_dir.exists():
        return []
    snaps = [p for p in ws.upgrade_dir.iterdir()
             if p.is_dir() and (p / "manifest.json").exists()]
    return sorted(snaps, key=lambda p: p.name, reverse=True)


def undo_last(ws: Workspace) -> ApplyResult:
    """按最近一份 manifest 把升级整体还原。

    有备份的还原原文件；当初新建的（`existed=false`）直接删掉 —— 于是「升级」
    与「撤销升级」互为逆操作，工作区回到升级前那一刻。

    Args:
        ws: 工作区

    Returns:
        `ApplyResult`；没有快照时 `errors` 里带一条说明。
    """
    res = ApplyResult()
    snaps = list_snapshots(ws)
    if not snaps:
        res.errors.append("没有可撤销的快照。")
        return res
    snap = snaps[0]
    try:
        manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        res.errors.append(f"读不了快照清单 {snap.name}（{exc}）")
        return res

    for entry in manifest.get("entries") or []:
        rel = safe_rel(str(entry.get("path") or ""))
        if not rel:
            res.errors.append(f"跳过非法路径：{entry.get('path')}")
            continue
        dst = ws.dir / rel
        try:
            if entry.get("existed"):
                backup = snap / str(entry.get("backup") or f"files/{rel}")
                atomic_write(dst, backup.read_text(encoding="utf-8"))
            elif dst.exists():
                dst.unlink()
            res.applied.append(rel)
        except OSError as exc:
            res.errors.append(f"{rel}：还原失败（{exc}）")

    res.snapshot_dir = str(snap)
    return res


def render_staged(ws: Workspace) -> str:
    """待应用区的可读清单（`:apply` 之前给人过目）。

    Args:
        ws: 工作区

    Returns:
        清单文本；空时说明「没有待应用改动」。
    """
    staged = list_staged(ws)
    if not staged:
        return "待应用区是空的 —— 模型还没有拟好任何改动。"
    lines = [f"待应用 {len(staged)} 个文件（人确认后用 :apply 落盘）："]
    for rel in staged:
        target = ws.dir / rel
        state = "覆写" if target.exists() else "新建"
        lines.append(f"- [{state}] {rel}")
    return "\n".join(lines)
