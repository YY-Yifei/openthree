"""出站文件的闸门 —— 所有能把本地文件发给对方的频道共用这一份。

**为什么单独一个模块**：A2A 与微信都能把文件送出去，而「哪些文件不许发」这条判定
是安全性质的。两处各写一份，早晚有一处漏掉一条黑名单，而漏掉的那一次看起来完全
正常：文件发出去了，只是它恰好是 `config.yaml`。所以根白名单、密钥黑名单、
体积上限、名字消毒全部只在这里定义，频道只负责「按这些闸门筛一遍，然后发」。

**默认值**：根 = 工作区（`.`，相对项目根），单文件 20 MiB，一条消息合计 24 MiB。
`outbound_roots: []` 是**唯一**的关法 —— 空列表意味着「一个都不发」，而不是
「发整个磁盘」。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

# 单文件 20 MiB：A2A 里它会被 base64 成 ≈26.7 MB（再被复制三次），所以这是
# 「够用」的上限而不是「随便用」；微信侧同一份文件要先经过 iLink 上传通道。
MAX_FILE_BYTES = 20 << 20
MAX_TOTAL_BYTES = 24 << 20          # 一条消息合计：一个 20 MiB 大文件 + 零头
DEFAULT_ROOTS = "."
MIME_FALLBACK = "application/octet-stream"

# 密钥黑名单。按**路径组件**判，不按子串：`report-api_key-summary.pdf` 不该被误杀，
# 而 `.openthree/config.yaml` 无论根放宽到哪儿都该被拦。
DENY_NAMES = frozenset({
    "api_key", "api_key.txt", "id_rsa", "id_ed25519",
})
# `.env` / `.env.local` / `.env.production`：按前缀判，不枚举。
DENY_NAME_PREFIXES = (".env",)
# 工作区状态目录 `.openthree/` 下**唯一**允许外发的子树：收来的图（对方要看的往往
# 就是它）。其余一律不发 —— 这是**反向白名单**而不是枚举：枚举会漏，最现实的例子是
# 升级模式把被覆盖的旧文件 `copy2` 到 `.openthree/upgrade/<ts>/files/config.yaml`，
# 那是 `config.yaml` 的副本、同样带着 llm.api_key，却不在任何一张枚举表里。反向规则
# 让「新增的状态文件」默认是**不可外发**的，而不是悄悄可外发。
ALLOW_PATH = ".openthree/assets"


def project_root(workspace_dir: Any) -> Path:
    """项目根（`.openthree` 的父目录）—— 相对根与黑名单的基准。

    **必须 resolve**：`denied_reason()` 拿它跟一个已经 resolve 过的绝对路径比
    `relative_to()`；基准是相对路径时那个调用抛 ValueError，整个 `.openthree`
    黑名单就被静默跳过 —— 一条安全闸的失效，不该取决于工作区是怎么传进来的。
    """
    root = Path(workspace_dir) if workspace_dir else Path(".openthree")
    return (root.parent if root.name == ".openthree" else root).resolve()


def parse_roots(raw: str, base: Path) -> List[Path]:
    """把配置里的 `outbound_roots` 读成绝对路径列表；空列表 = 关掉出站文件。

    配置层把选项值统一 `str()`（`ChannelConfig.from_dict`），所以 `[]` 会变成
    `"[]"`、`[a, b]` 变成 `"['a', 'b']"`。这里按**分隔符**切而不是按逗号切，
    两种写法都认；切完为空就返回 `[]`。
    """
    text = str(raw if raw is not None else DEFAULT_ROOTS)
    items = [x.strip() for x in re.split(r"[,;\[\]'\"]+", text) if x.strip()]
    roots: List[Path] = []
    for item in items:
        # `expanduser()` 也在 try 里：`~nosuchuser` 抛 RuntimeError，漏掉它就会从
        # 发送路径里冒出去 —— 一条写错的配置不该让整条答复失败。
        try:
            p = Path(item).expanduser()
            if not p.is_absolute():
                p = base / p
            roots.append(p.resolve())
        except (OSError, RuntimeError, ValueError):
            continue        # 单条路径解析不了不该让整个频道起不来
    return roots


def within_roots(path: Path, roots: Sequence[Path]) -> bool:
    """`path` 是否落在某个根之内（`path` 应当已经 resolve 过）。"""
    for root in roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


def denied_reason(path: Path, root: Path) -> str:
    """命中黑名单就返回原因（空串 = 放行）。

    两条规则，管的是两件事：

    * **文件名**：`.env*` / `api_key` / 私钥 —— 按名字判，不管它在哪一层。
      按组件判、不按子串：`report-api_key-summary.pdf` 不该被误杀。
    * **工作区状态目录**：`.openthree/` 下除 `assets/` 外一律不发（反向白名单，
      理由见 `ALLOW_PATH` 上方的注释）。
    """
    name = path.name.lower()
    if name in DENY_NAMES:
        return "命中密钥黑名单"
    for prefix in DENY_NAME_PREFIXES:
        if name == prefix or name.startswith(prefix + "."):
            return "命中密钥黑名单（.env 里通常是密钥）"
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ""
    rel_text = rel.as_posix().lower()
    if rel_text == ".openthree" or rel_text.startswith(".openthree/"):
        if rel_text == ALLOW_PATH or rel_text.startswith(ALLOW_PATH + "/"):
            return ""
        return "命中密钥黑名单（工作区状态不发）"
    return ""


def clean_name(name: str, fallback: str = "download.bin") -> str:
    """对方看到的文件名：只取 basename、滤掉控制字符与路径分隔符、截到 128。

    模型可以为了对方看得懂而改名，但**不采信它**：路径分隔符能伪造目录，控制字符
    能伪造日志行。留一个纯文件名就够。
    """
    base = Path(str(name or "")).name or fallback
    cleaned = "".join(c for c in base if c.isprintable() and c not in '\\/:*?"<>|')
    cleaned = cleaned.strip().strip(".")
    if cleaned:
        return cleaned[:128]
    # 回落值也要消毒：它通常是真实文件名，而 POSIX 的文件名可以只由一个换行或一个
    # 反斜杠组成 —— 那正是这个函数要挡的东西。
    alt = "".join(c for c in str(fallback or "") if c.isprintable() and c not in '\\/:*?"<>|')
    return (alt.strip().strip(".") or "download.bin")[:128]


def screen(raw: str, roots: Sequence[Path], root: Path,
           max_bytes: int = MAX_FILE_BYTES) -> Tuple[Optional[Path], int, str]:
    """把用户给的路径过一遍最便宜的闸门，返回 `(路径, 字节数, 拒绝原因)`。

    顺序即代价（先便宜的）：resolve（顺带拦住软链逃逸）→ 是不是普通文件 →
    密钥黑名单 → 根白名单 → 大小。放行时第三个值是空串，拒绝时第一个值是 `None`
    且第二个是 0。

    `max_bytes` 由调用方给：各渠道的**单文件上限不一样**（A2A 是 base64 进 JSON 的账，
    微信还要在同步发送里做纯 Python 加密），但闸门的**顺序与措辞只有一个**。

    **不抛异常**：路径可能是含 NUL 的字符串（`os.stat` 抛 ValueError 而不是
    OSError）、可能是刚被删掉的文件、家目录也可能解析不出来 —— 任何一种冒出去，
    就会让整条答复失败，而它只是「一个附件没发成」。
    """
    text = str(raw or "").strip()
    if not text:
        return None, 0, "路径是空的"
    try:
        path = Path(text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None, 0, "读不到这个路径"
    try:
        if not path.is_file():
            return None, 0, "不是普通文件"
    except OSError:
        return None, 0, "读不到这个路径"
    denied = denied_reason(path, root)
    if denied:
        return None, 0, denied
    if not within_roots(path, roots):
        return None, 0, "不在允许发送的目录内"
    try:
        size = path.stat().st_size
    except OSError:
        return None, 0, "读不到文件大小"
    if size <= 0:
        return None, 0, "空文件"
    if size > max_bytes:
        return None, 0, f"{size} 字节超过单文件上限 {max_bytes}"
    return path, size, ""


def read_exact(path: Path, size: int) -> Tuple[Optional[bytes], str]:
    """按 `size` 读文件，返回 `(字节, 没读成的原因)`；成功时原因为空串。

    只读 `size + 1` 字节：多读的那 1 字节就是「它在 stat 之后变了」的证据，同时
    保证一次分配不超过单文件上限 —— `read_bytes()` 会按文件**现在**的大小分配，
    stat 与读之间的间隙够别人把它换成 2 GB。

    **两种失败分开报**：读不出来（被删 / 权限变了 / 变成了目录）与「大小变了」是
    两件事，收件人看到的措辞不该是同一条 —— 前者他会去查权限，后者他会去问「谁在
    改这个文件」。
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(size + 1)
    except OSError:
        return None, "读不出来"
    if len(data) != size:
        return None, "文件在发送时被改动"
    return data, ""
