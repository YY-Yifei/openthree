"""入站图片的闸门 —— 所有会把收到的图片落进 `assets/` 的频道共用这一份。

**为什么单独一个模块**：A2A 与微信都能收图，而「这张图落不落得下」是三条**安全**
判定的合成 —— 单张上限（`OPENTHREE_MAX_IMAGE_BYTES`）、一条消息最多几张、
`assets/` 总量预算。两处各写一份，早晚有一处漏掉一条；而漏掉的那一次看起来完全
正常：图收下了，只是目录被慢慢填满、或者一条消息塞进了几千张。

落盘本身走 `multimodal.save_image_bytes()` —— 命名规则（内容 sha1 + magic bytes
决定的后缀）在那一处，与 CLI `--image` 同一条路。同一个内容因此只落一次盘。

`assets/` **只增不减**：模型改任务时会重读图片文件，删旧文件等于抽掉 history 脚下
的真源。所以预算是硬闸而不是水位线 —— 到顶之后只拒新图，腾空间要手动清理。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..multimodal import TMP_MARK, ImageError, asset_dest, save_image_bytes

# 一条消息最多收几张图。没有上限时，一条请求可以塞几万张「合法」小图（每张只需几十
# 字节的真 magic bytes），而每张都会永久落 `assets/` 并进模型的 content。
MAX_IMAGES_PER_MESSAGE = 8

# `assets/` 的总字节预算。收图是外部输入入口（A2A 那条甚至没有鉴权），不设上限
# 等于把磁盘交给对方：内容改一个字节就绕开 sha1 去重。
ASSETS_BUDGET_BYTES = 512 << 20

# 落盘锁按目录分（`_lock_for()`）：见那里的说明 —— 预算检查是 check-then-act，
# 而两个频道写的是同一个目录，各持一把锁等于没锁。
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def assets_dir(workspace_dir: Any) -> Path:
    """收来的图落在哪儿：`<工作区>/assets/`（`workspace_dir` 一般是 `.openthree`）。

    与 `Workspace.assets_dir` 是同一个目录，所以 `stage_images()` 看到同名同内容时
    直接跳过 —— 同一张图不会在目录里出现两份。
    """
    root = Path(workspace_dir) if workspace_dir else Path(".openthree")
    return root / "assets"


def usage(directory: Path) -> int:
    """这个目录现在占了多少字节（不存在 / 读不动都算 0）。

    每次收图现扫一遍：收图是低频操作（一条消息最多几张），而缓存会与 CLI `--image`
    的写入不同步 —— 一个对不上的数字比一个慢一点的数字更糟。
    """
    if not directory.exists():
        return 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        # 目录在 exists() 与 iterdir() 之间被删、或没有读权限：如实当 0。
        # 这里不是给运维报警的地方 —— 抛出去会把一条本来能跑的消息整个变成失败。
        return 0
    total = 0
    for path in entries:
        try:
            # 跳过原子写的碎片（`<digest>.<ext>.tmp-xxxx`）：它只在写盘中途存在，
            # 崩溃留下的残留不该永久吃掉预算 —— 那是碎片，不是一张图。
            if path.is_file() and TMP_MARK not in path.name:
                total += path.stat().st_size
        except OSError:
            pass
    return total


def _lock_for(directory: Path) -> threading.Lock:
    """按**落盘目录**取一把全进程共用的锁。

    为什么不能由调用方各传各的：预算检查是 check-then-act，而 A2A 与微信写的是
    **同一个** `.openthree/assets/`，各自有一条收图路径（manager 给每个频道起一条
    线程）。两把锁 = 没锁：两边同时收图时各自读到同一个用量、各自通过检查、再各写
    几 MB，那条上限就被绕过去了。目录是唯一身份，所以锁按（resolve 过的）目录分。
    """
    try:
        key = str(Path(directory).resolve())
    except (OSError, RuntimeError, ValueError):
        key = str(directory)        # 解析不出来也得有锁，宁可粗一点
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def append_notes(text: str, notes: List[str]) -> str:
    """把几句说明拼到消息正文后面（空的那几段直接不占位，不留空行）。

    收图失败的原因必须回到发消息的人眼前 —— 静默少一张图与「机器人没看懂」
    是同一种感受。拼法（两个换行、丢掉空段）只许有这一处：微信与钉钉都靠它，
    两边措辞不同步的时候，没人分得清是哪个频道丢的图。
    """
    parts = [str(text or "").strip()] + [str(n or "").strip() for n in notes]
    return "\n\n".join(p for p in parts if p).strip()


def collect_images(directory: Path, specs: List[Any],
                   fetch: Callable[[Any, float], Tuple[Optional[bytes], str]],
                   *, deadline: float, timeout: float,
                   log: Any = None) -> Tuple[List[str], List[str]]:
    """按序收下 `specs` 里的每一张图，返回 `(本地路径表, 说明)`。

    闸门按**代价从便宜到贵**排：条数 → 单张大小（由 `fetch` 里的
    `get_bytes(max_bytes=…)` 在下载时就挡住）→ `assets/` 预算。被拒的每一张各留
    一句原因，那些原因会拼进正文发回去 —— 对方不知道少了什么，只会以为机器人
    没看懂。

    时间也有上界（`deadline` 秒）：这段代码跑在频道自己的收图线程上，而 8 张图 ×
    单张超时足以把下一条消息的交付推迟几分钟；超了就把剩下的如实记为没收到。

    `fetch` 由频道提供（条目, 本次超时 → `(字节, 原因)`）：钉钉要先拿 `downloadCode`
    换地址，微信的凭证本身就是地址参数，两边不同的只是这一步。循环、条数、预算
    与措辞只该有这一份 —— 两个频道各写一遍，迟早一边多收一张、或者一边把「超时」
    说成「失败」。**这个超时是整张图的预算，不是每一跳一份**：本函数只在两张之间
    检查 `deadline`，`fetch` 内部若多跳各拿一份，上界就变成了 deadline + timeout。
    """
    accepted = specs[:MAX_IMAGES_PER_MESSAGE]
    paths: List[str] = []
    notes: List[str] = []
    expire = time.monotonic() + deadline
    for index, spec in enumerate(accepted):
        left = expire - time.monotonic()
        if left <= 0:
            notes.append(f"（另有 {len(accepted) - index} 张图片未接收：下载超时）")
            break
        data, why = fetch(spec, min(timeout, left))
        if data is None:
            notes.append(why)      # why 本身就是一句完整的话（不再套一层括号）
            continue
        path, why = save_within_budget(directory, data)
        if path:
            paths.append(path)
        elif log is not None:
            # 原因会进正文（对方看得见），日志里再留一份带上下文的 —— 排查时
            # 「第几张、为什么」比正文里那句无主语的话有用得多。
            log.warning("收图落盘失败（第 %d/%d 张）：%s", index + 1, len(accepted), why)
        if not path:
            notes.append(why)
    extra = len(specs) - len(accepted)
    if extra > 0:
        notes.append(f"（另有 {extra} 张图片未接收：一条消息最多收 "
                     f"{MAX_IMAGES_PER_MESSAGE} 张）")
    return paths, notes


def save_within_budget(directory: Path, data: bytes,
                       budget: Optional[int] = None) -> Tuple[str, str]:
    """在锁里检查预算并落盘，返回 `(路径, 未接收的原因)`。

    两件事必须闭在一起：**预算检查是 check-then-act**（读用量 → 写盘），而收图可能
    跑在并发线程上（A2A 每条连接一个线程，微信一条轮询线程 + A2A 一条）—— 拆开的话
    N 个请求读到同一个用量、全部通过检查、再各写几 MB，唯一那条上限就被绕过去了。
    锁由 `_lock_for(directory)` 提供，**不是**调用方传进来的（理由见那个函数）。

    去重命中的内容不该再占一次预算：`asset_dest()` 只算名字不写盘，命名规则与
    `save_image_bytes` 是同一条（同一张图在目录里只有一个文件）。

    `budget` 留空就用模块常量（**调用这一刻**读，所以改得动）。**不抛异常**：
    一张图存不下来不该让整条消息失败。
    """
    if budget is None:
        budget = ASSETS_BUDGET_BYTES
    with _lock_for(directory):
        try:
            dest = asset_dest(directory, data)
        except ImageError as exc:
            return "", f"1 张图片未接收（{exc}）"
        if budget and not dest.exists():
            used = usage(directory)
            if used + len(data) > budget:
                return "", (f"1 张图片未接收（图片目录已用 {used} 字节，上限 "
                            f"{budget}；要腾空间请手动清理 assets/）")
        try:
            return str(save_image_bytes(directory, data)), ""
        except (ImageError, OSError) as exc:
            # ImageError 覆盖「认不出 / 太大 / 写不进去」，OSError 是兜底：这个函数
            # 承诺「不抛异常」。
            #
            # 原因会进正文 → 模型上下文，所以本机绝对路径（`图片写入失败 D:\…\assets`，
            # 常含用户名）不能跟着走：细节留给日志，对外只把目录换成 `<assets>`。
            reason = str(exc).replace(str(directory), "<assets>")
            return "", f"1 张图片未接收（{reason}）"
