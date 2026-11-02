"""注册器：把 function 变成模型的工具。

这是「二」的落地，也是自我演化的关键：
模型写出新文件 → reload() → 新工具立刻可用。

约定（极简）：
    * 模块里**所有**公开函数都注册，除非带 `_` 前缀。
    * 函数首个参数若命名为 `kernel`（或注解为 Kernel），由运行期注入，模型不可见。
    * 同名工具**近者生效**：加载顺序即裁决 —— 从远到近加载，后加载的覆盖先加载的。

「从哪加载」只有两处**显式**来源：本项目 `.openthree/functions/*.py`，以及
`functions.json` 里登记过的路径。用户目录里的文件**不会**自动加载 —— 它只被
`lookup_functions` 发现，要不要用由模型自己 `register_function` 决定。
命令行 `-profile` 点名的目录是第三处，它们**会**自动加载：点名就是授权。
于是「你在哪个目录、挂了谁」决定你能**看见**什么，而默认能力集合只由显式
决定改变。
"""
from __future__ import annotations

import hashlib
import re
import sys
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tool import Tool, build_tool, is_registrable
from .workspace import (DIRNAME, USER_FUNCTION_DISTANCE, Workspace, canon_path,
                        read_source, sha256_file)

_MODULE_PREFIX = "openthree_user_functions"
_BUILTIN_DISTANCE = 10 ** 9

# `__system_prompts__` —— function 文件自带的说明。它进 system，**不是工具**。
# dunder 命名让它天然落在 `is_registrable` 的第一档（协议名，永不注册），
# 与 `__main__` 同属「这个文件与内核之间的约定」。
PROMPT_ATTR = "__system_prompts__"

# 附随提示词是**唯一能零成本污染 system 的入口**：一个 50 KB 的提示词足以把
# 工具 schema 挤到模型看不见的地方，而它看起来只是「某个文件写得啰嗦」。
MAX_ATTACHED_PROMPT_CHARS = 4000    # 单文件
MAX_ATTACHED_PROMPT_TOTAL = 12000   # 全部文件合计


def eval_attached_prompt(raw: Any) -> Tuple[str, int, str]:
    """把 `__system_prompts__` 的取值规整成 ``(正文, 段数, 错误)``。

    **唯一判据**：注册器收集与命令行 `--print-prompts` 都走这一份。工具判据
    （`tool.is_registrable`）曾有过「注册器与审计各写一套」的教训，这里不重演 ——
    否则「模型被喂进的附随说明」与「人在 CLI 里看到的那份」会慢慢发散。

    接受的三种形态：``str`` / ``list[str]``（tuple 同义）/ 零参可调用。其余一律报错
    而不是猜一个意思：附随提示词直接进 system，**静默丢弃比报错危险得多**。

    只允许零参：附随提示词是**能力的说明书**，它不需要知道「此刻在干什么」。
    想按情境给指导，那本来就该是一个返回文字的 function，由模型自己判断要不要读。

    Args:
        raw: 模块属性 `__system_prompts__` 取到的对象

    Returns:
        ``(正文, 段数, 错误)``；错误为空串表示成功，正文为空串表示「没有内容」。
    """
    if callable(raw):
        try:
            raw = raw()
        except Exception as exc:  # noqa: BLE001 - 用户代码，什么样的异常都可能有
            return "", 0, f"{PROMPT_ATTR}() 求值失败：{type(exc).__name__}: {exc}"
    if isinstance(raw, str):
        pieces: List[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        pieces = list(raw)
        for i, piece in enumerate(pieces):
            if not isinstance(piece, str):
                return "", 0, (f"{PROMPT_ATTR} 第 {i} 段是 {type(piece).__name__}，"
                               "只接受 str / list[str] / 零参函数")
    else:
        return "", 0, (f"{PROMPT_ATTR} 是 {type(raw).__name__}，"
                       "只接受 str / list[str] / 零参函数")
    texts = [p.strip() for p in pieces]
    texts = [p for p in texts if p]
    if not texts:
        return "", 0, ""
    return "\n\n".join(texts), len(texts), ""


def _in_function_dir(path: str) -> bool:
    """一个源码路径是否落在某个 `.openthree/functions` 目录里。

    刻意不看它属于**哪个**工作区：这个目录名是我们自己的命名空间，里面的模块
    只可能是我们或别的内核 exec 出来的。跨工作区、跨测试进程留下的残留在
    `sys.modules` 里同样会串味，所以一律按这一条判。
    """
    try:
        parts = Path(path).parts
    except (OSError, ValueError):  # pragma: no cover - 路径异常
        return False
    return any(parts[i] == DIRNAME and parts[i + 1] == "functions"
               for i in range(len(parts) - 1))


@dataclass
class LoadError:
    file: str
    error: str
    traceback: str = ""


@dataclass
class Override:
    """一次「同名、近者生效」的记录 —— 这是**正常现象**，不是故障。

    它之所以要单列（而不是混进 `load_errors`）：同名覆盖是距离裁决的正常结果，
    而加载失败是真故障。混在一起，模型会把自己显式登记的正常覆盖当成异常，
    或者反过来对真故障视而不见。
    """

    tool: str
    winner: str
    winner_kind: str
    loser: str
    loser_kind: str


@dataclass(frozen=True)
class LoadPlanItem:
    """本次要加载的一个文件，以及它来自哪一档源。"""

    path: Path
    source: str       # 进模块名的稳定标识：project / profile1 / user / registered2
    label: str        # 给人看的来源：project / profile:~/prof/base / user / registered
    distance: int     # 越小越近；同名时近者生效


@dataclass
class AttachedPrompt:
    """一个 function 文件自带的说明 —— 它进 system，**但不是工具**。

    `text` 是**实际进 system 的那份**（可能已截断），`sha256` 也按它算：
    内容寻址落盘（`logs/attached-prompts/<sha8>.md`）因此能直接回答
    「此刻 system 里那一段究竟是哪一版」。

    它不参与 `digest()` / 工具清单：模型不能调用它，它的位置是「说明」。
    """

    file: str
    text: str
    label: str        # 给人看的来源：project / profile:~/prof/base / registered
    distance: int     # 越小越近；顺序与总量上限都按这一根轴
    sha256: str
    parts: int = 1    # 由几段拼成（list 形态 / 多个 str）
    original_chars: int = 0   # 截断前的长度；未截断时等于 len(text)


@dataclass
class Registry:
    """工具名 → Tool。"""

    workspace: Workspace
    tools: Dict[str, Tool] = field(default_factory=dict)
    load_errors: List[LoadError] = field(default_factory=list)
    overrides: List[Override] = field(default_factory=list)
    loaded_files: List[str] = field(default_factory=list)
    builtins: Dict[str, Tool] = field(default_factory=dict)
    # 附随提示词（`__system_prompts__`）与它的失败记录。两者都**不是工具**：
    # 它们进 system，不影响 `names()` / `schemas()` / `digest()`。
    prompts: List[AttachedPrompt] = field(default_factory=list)
    prompt_errors: List[LoadError] = field(default_factory=list)
    missing_registered: List[str] = field(default_factory=list)
    changed_registered: List[Dict[str, str]] = field(default_factory=list)
    # 本次 reload 已经清过 pyc 的目录 —— 每个目录只清一次，别对同一处反复删。
    _pycache_cleared: set = field(default_factory=set, repr=False)

    # ---------- 内置 ----------
    def register_builtin(self, name: str, func: Callable[..., Any]) -> Tool:
        t = build_tool(func, origin="builtin")
        t.name = name
        t.source_kind = "builtin"
        t.source_distance = _BUILTIN_DISTANCE   # 内置永远最远：任何同名的外部工具都顶得掉它
        self.builtins[name] = t
        self.tools[name] = t
        return t

    def freeze_builtins(self) -> None:
        """记录内置工具，便于后续区分「模型自造」的工具。"""
        self.builtins = {k: v for k, v in self.tools.items() if v.origin == "builtin"}

    # ---------- 加载 ----------
    def reload(self) -> "Registry":
        """按 `load_plan()` 重新加载；内置工具始终保留。"""
        # 先清缓存，再加载：保证 reload 时执行的一定是新代码，
        # 也避免用户函数 import 到上一次的陈旧辅助模块。
        self._purge_modules()
        self._pycache_cleared = set()      # 每次 reload 都要重新清一遍 pyc
        self.tools = dict(self.builtins)
        self.load_errors = []
        self.overrides = []
        self.loaded_files = []
        self.prompts = []
        self.prompt_errors = []
        self.missing_registered = []
        self.changed_registered = []
        for item in self.load_plan():
            self._load_file(item)
        self._finalize_prompts()
        return self

    def load_plan(self) -> List[LoadPlanItem]:
        """本次要加载的文件清单，**从远到近**排序 —— 顺序本身就是同名裁决。

        加载集合来自：project 的 functions 目录、命令行点名的 profile 目录
        （两者都自动加载），以及登记簿里的路径。
        登记项若同时也在某个源目录里，按 canonical 路径去重、保留**更近**的那条：
        同一份代码只加载一次，并按它物理位置的距离参与裁决。
        """
        ws = self.workspace
        entries: List[Tuple[int, int, LoadPlanItem]] = []
        seq = 0

        # 来源表算一次：`ws.function_source_for` 每调一次都要重建整条来源表，
        # 而下面要对每条登记项问一遍 —— 那是 O(登记数 × 来源档数) 次路径解析。
        sources = ws.function_sources(include_missing=True)
        by_dir = {canon_path(s.path): s for s in sources}

        for rec in ws.read_functions_meta().get("registered") or []:
            raw = str(rec.get("path") or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():      # 登记只接受绝对路径（相对路径会随 cwd 漂移）
                continue
            try:
                exists = path.is_file()
            except OSError:
                exists = False
            if not exists:
                self.missing_registered.append(str(path))
                continue
            # 登记簿是**数据**，不是可信清单：手写一条就能让任意文件被加载的话，
            # `register_function` 的边界检查（只收 `.py`、只收源目录族内）就成了摆设。
            # 所以这里按同一套规矩复核，越界的记进 load_errors 而不是偷偷执行。
            if path.suffix.lower() != ".py":
                self.load_errors.append(LoadError(
                    file=str(path),
                    error="登记项不是 .py 文件，拒绝加载 —— 登记簿不是任意代码入口"))
                continue
            src = by_dir.get(canon_path(path.parent))
            if src is None:
                self.load_errors.append(LoadError(
                    file=str(path),
                    error=("已登记但不在 function 的源目录族内，拒绝加载 —— "
                           "登记簿不是绕过 register_function 边界检查的入口")))
                continue
            want = str(rec.get("sha256") or "")
            if want:
                try:
                    now = sha256_file(path)
                except OSError:
                    now = ""
                if now and now != want:
                    self.changed_registered.append({
                        "path": str(path),
                        "registered_sha256": want,
                        "current_sha256": now,
                    })
            entries.append((src.distance, seq, LoadPlanItem(
                path=path, source=f"registered{seq}", label=src.label,
                distance=src.distance)))
            seq += 1

        for i, src in enumerate(sources):
            # 自动加载**命令行点名过的**档：project（d=0）与 profile（d=1,2,…）。
            # 用户目录（d=USER_FUNCTION_DISTANCE）里的文件属于「发现了但没被授权」
            # —— 它是整个账户共用的库，不该因为某个人放过一个文件就在别处自动执行，
            # 要加载必须显式 register_function。这条判断就是「存在 ≠ 可用」在代码里
            # 的落点，别删。
            # 判据只有一个数：近于 user 的档自动加载。`kind` 只是标签，别拿它做
            # 相等比较 —— 那样每加一档就得回来再改一次这里。
            if src.distance >= USER_FUNCTION_DISTANCE:
                continue
            try:
                files = sorted(src.path.glob("*.py"))
            except OSError:  # pragma: no cover - 权限异常
                continue
            for path in files:
                if path.name.startswith("_"):
                    continue
                entries.append((src.distance, seq, LoadPlanItem(
                    path=path, source=f"{src.kind}{i}", label=src.label,
                    distance=src.distance)))
                seq += 1

        # 同一份代码只加载一次：canonical 去重，同距离时让「源目录」那条胜出
        best: Dict[str, Tuple[int, int, LoadPlanItem]] = {}
        for e in entries:
            key = canon_path(e[2].path)
            cur = best.get(key)
            if cur is None or e[0] <= cur[0]:
                best[key] = e
        return [e[2] for e in sorted(best.values(), key=lambda e: (-e[0], e[1]))]

    def _module_file_dir(self, mod: Any) -> Optional[str]:
        """一个模块的源码所在目录（canon 键）；不是 `.openthree/functions` 里的返回 None。"""
        f = getattr(mod, "__file__", None)
        if not isinstance(f, str) or not f or not _in_function_dir(f):
            return None
        try:
            return canon_path(Path(f).parent)
        except (OSError, RuntimeError):  # pragma: no cover - 路径异常
            return None

    def _purge_modules(self) -> None:
        """清掉加载过的模块 —— **含裸名辅助模块**，不只是工具模块。

        `sys.modules` 是个全局缓存，而 sys.path 的逐文件窗口只管「从哪 import」，
        管不了「已经缓存过什么」。于是 `import helper` 这种裸名模块一旦落进
        `sys.modules`，就永久占住那个键：两个目录各有 `helper.py` 时会串味
        （先加载的那份永远赢），reload 也不会重读（改了没生效）。

        判据只能看 `__file__` —— 裸名模块没有前缀可认。凡源码落在
        `.openthree/functions` 里的模块，都是我们（或另一个内核）exec 出来的，
        一律清掉；系统模块与第三方库不动。
        """
        for name, mod in list(sys.modules.items()):
            if name.startswith(_MODULE_PREFIX) or self._module_file_dir(mod) is not None:
                sys.modules.pop(name, None)

    def _module_name_for(self, path: Path, source: str) -> str:
        """模块名必须带**源标识**：两个目录都有 `foo.py` 时，只按 stem 命名会撞同一个键。"""
        slug = re.sub(r"[^0-9A-Za-z_]+", "_", source).strip("_") or "src"
        return f"{_MODULE_PREFIX}.{slug}.{path.stem}"

    def _evict_foreign_helpers(self, directory: Path) -> None:
        """执行一个文件之前，把**别的来源目录**的辅助模块从 `sys.modules` 里请出去。

        sys.path 的逐文件窗口只决定「从哪个目录 import」；裸名 `helper` 一旦被
        更远那一档占住（远者先加载），更近的那档 `import helper` 拿到的就是它。
        按 `__file__` 判归属、不是本目录的就清掉，import 才会重新从窗口里解析。
        工具模块（带 `_MODULE_PREFIX`）名字里已含源标识，不会撞，留着。

        这样两个目录各有 `helper.py` 时，各自拿到自己那份；函数对象经
        `__globals__` 攥着模块对象，被清出 `sys.modules` 也不影响已建好的工具。
        """
        here = canon_path(directory)
        for name, mod in list(sys.modules.items()):
            if name.startswith(_MODULE_PREFIX):
                continue
            if self._module_file_dir(mod) not in (None, here):
                sys.modules.pop(name, None)

    def _clear_bytecode_cache(self, directory: Path) -> None:
        """删掉这个目录 `__pycache__` 里已有的 pyc（每个目录每次 reload 只做一次）。

        工具模块走 `compile(源码)`，绕过了 importlib；但**辅助模块**走的是标准
        import —— 它按 (mtime, size) 判 pyc 新不新鲜，而「同一秒内、等长覆盖」
        正好命中「看起来没变」。于是辅助模块改了、reload 了，跑的仍是旧代码。
        pyc 本来就是可删的缓存，删掉让它重新编译。
        """
        key = str(Path(directory))
        if key in self._pycache_cleared:
            return
        self._pycache_cleared.add(key)
        cache = Path(directory) / "__pycache__"
        try:
            if not cache.is_dir():
                return
            for pyc in cache.glob("*.pyc"):
                pyc.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 权限 / 竞争
            pass

    def _load_file(self, item: LoadPlanItem) -> None:
        path = item.path
        mod_name = self._module_name_for(path, item.source)
        parent = str(path.parent)
        added = parent not in sys.path
        try:
            if added:
                sys.path.insert(0, parent)   # 让这个文件能 import **同目录**的辅助模块
            self._evict_foreign_helpers(path.parent)
            self._clear_bytecode_cache(path.parent)
            # 先读源码再 compile，**绕过 importlib 的字节码缓存**：
            # SourceFileLoader 按 (mtime, size) 复用 `__pycache__` 里的 pyc，而模型
            # 改 function 的典型形态正是「同一秒内、长度不变地覆盖同名文件」——
            # 正好命中「看起来没变」的判据，于是 reload 后跑的仍是旧代码。
            # 这种「改了没生效」极难排查，所以宁可少一层缓存。
            # 解码按 PEP 263 编码声明（`read_source`）：手写文件不一定是 UTF-8，
            # 而它本来能被 `import` 正常加载 —— 两个判据不能各说一套。
            source = read_source(path)
            code = compile(source, str(path), "exec")
            module = types.ModuleType(mod_name)
            module.__file__ = str(path)
            sys.modules[mod_name] = module
            exec(code, module.__dict__)   # noqa: S102 - 执行用户 function 是本模块的职责
        except Exception as exc:  # noqa: BLE001
            self.load_errors.append(
                LoadError(file=str(path), error=f"{type(exc).__name__}: {exc}",
                          traceback=traceback.format_exc())
            )
            sys.modules.pop(mod_name, None)
            return
        finally:
            # 逐文件窗口：一次只让**一个目录**在 sys.path 上，
            # 于是两个目录各有 `util.py` 时不会串味。
            if added:
                try:
                    sys.path.remove(parent)
                except ValueError:  # pragma: no cover - 被用户函数自己移走了
                    pass

        count = 0
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            # 唯一判据：dunder / 下划线 / 非函数 / 非本模块定义 一律跳过。
            if not is_registrable(mod_name, attr_name, obj):
                continue
            try:
                t = build_tool(obj, origin="user", source_file=str(path))
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(
                    LoadError(file=str(path), error=f"签名解析失败 {attr_name}: {exc}")
                )
                continue
            t.source_kind = item.label
            t.source_distance = item.distance
            prev = self.tools.get(t.name)
            if prev is not None:
                self._note_conflict(prev, t, item)
            self.tools[t.name] = t
            count += 1

        has_prompt = self._collect_prompt(module, item)
        self.loaded_files.append(str(path))
        if count == 0 and not has_prompt:
            # 纯提示词文件（只带 `__system_prompts__`、没有工具）是正当用法：
            # 把一段独立规范写成 py 文件，随 profile 一起走。它不该被当成坏文件。
            self.load_errors.append(
                LoadError(file=str(path), error="文件中没有可注册的公开函数")
            )

    def _collect_prompt(self, module: Any, item: LoadPlanItem) -> bool:
        """收一个文件自带的说明；返回「这个文件有没有非空附随提示词」。

        没有 `__system_prompts__` 时**立刻返回**：不读盘、不求值、不改任何状态 ——
        这就是「不写就是零」在代码里的落点（`prompt.build_attached_block` 拿到空
        列表时也只返回空串，一个字节都不拼）。

        失败隔离：求值抛异常 / 返回非 str 只记进 `prompt_errors`，该文件的工具
        照常注册（注册循环在调用本方法之前就跑完了）—— 一份说明写坏了，不该
        连带它自己的工具一起消失。
        """
        raw = getattr(module, PROMPT_ATTR, None)
        if raw is None:
            return False
        text, parts, error = eval_attached_prompt(raw)
        if error:
            self.prompt_errors.append(LoadError(file=str(item.path), error=error))
            return False
        if not text:
            return False                    # 空串：不占位、不多字节
        original = len(text)
        if original > MAX_ATTACHED_PROMPT_CHARS:
            notice = f"\n\n（已截断，原文 {original} 字符）"
            keep = max(0, MAX_ATTACHED_PROMPT_CHARS - len(notice))
            # 上限包含那句截断说明：否则 `text` 会超出常量，而审计提示里写着
            # 「单文件上限 4000 字符」—— 两个数必须说的是同一件事。
            text = text[:keep].rstrip() + notice
        self.prompts.append(AttachedPrompt(
            file=str(item.path), text=text, label=item.label,
            distance=item.distance,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            parts=parts, original_chars=original,
        ))
        return True

    def _finalize_prompts(self) -> None:
        """排序 + 总量裁剪 + 落盘 —— `reload()` 的最后一步。

        排序按 `distance` 升序（project 先、profile 次之、user 最后）：近的先读，
        与「同名工具近者生效」是同一根轴，不新造判据。

        总量上限按同一根轴**保留近的**，被挤掉的记进 `prompt_errors` ——
        静默丢弃会让「我写的提示词怎么没生效」变成一次考古。
        """
        self.prompts.sort(key=lambda p: p.distance)
        kept: List[AttachedPrompt] = []
        total = 0
        for p in self.prompts:
            if total + len(p.text) > MAX_ATTACHED_PROMPT_TOTAL:
                self.prompt_errors.append(LoadError(
                    file=p.file,
                    error=(f"附随提示词超出总量上限 {MAX_ATTACHED_PROMPT_TOTAL} 字符，"
                           "已丢弃（近者优先；删短别的提示词或缩短本段）")))
                continue
            kept.append(p)
            total += len(p.text)
        self.prompts = kept
        self._persist_prompts()

    def _persist_prompts(self) -> None:
        """按内容寻址留一份**能读回来**的痕迹：`logs/attached-prompts/<sha8>.md`。

        为什么必须有：`.openthree/` 被 `.gitignore` 整目录排除，模型自造的 function
        没有 git 历史；recorder 事件里只留 sha256 也还原不出内容。于是「模型改了
        自己的 system」会变成一件它自己看不见的事 —— 与「一」的
        `snapshot_system_prompt` 是同一个承诺：**能改，也能看见自己改过什么**。

        append-only 且天然去重（同一段文字反复 reload 只落一次）。写不进去
        （只读工作区 / 权限）就安静跳过：落盘是审计手段，不该让加载失败。
        """
        if not self.prompts:
            return
        out_dir = self.workspace.logs_dir / "attached-prompts"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - 只读工作区 / 权限
            return
        for p in self.prompts:
            path = out_dir / f"{p.sha256[:8]}.md"
            data = p.text.encode("utf-8")
            # 写**字节**，不写文本：Windows 上 `write_text` 会把 `\n` 改写成
            # `\r\n`，于是文件的 sha256 与它的文件名对不上 —— 「内容寻址」就成了
            # 假的（读回来的是另一份字节）。按字节写，文件名才真的等于内容。
            if path.exists():
                try:
                    if path.read_bytes() == data:
                        continue
                except OSError:  # pragma: no cover - 权限 / 竞争
                    continue
            try:
                path.write_bytes(data)
            except OSError:  # pragma: no cover - 权限 / 竞争
                continue

    def _note_conflict(self, prev: Tool, t: Tool, item: LoadPlanItem) -> None:
        """同名时的两种情况：距离能解释的 → overrides；解释不了的 → load_errors。

        判据一句话：**距离能解释的进 overrides，距离解释不了的进 load_errors**。
        否则「最近者赢」会变成掩盖同一目录内命名 bug 的借口。
        """
        if prev.source_file and canon_path(prev.source_file) == canon_path(item.path):
            return                                  # 同一个文件，模块内名字不可能重复
        if prev.origin != "builtin" and prev.source_distance == item.distance:
            loser = Path(prev.source_file).name if prev.source_file else "?"
            self.load_errors.append(LoadError(
                file=str(item.path),
                error=(f"同名工具 {t.name} 与 {loser} 距离相同（d={item.distance}），"
                       f"距离裁决不了 —— 按加载顺序后者生效，建议改名"),
            ))
            return
        self.overrides.append(Override(
            tool=t.name,
            winner=str(item.path),
            winner_kind=item.label,
            loser=str(prev.source_file) if prev.source_file else "内置",
            loser_kind=(prev.source_kind if prev.origin != "builtin" else "builtin"),
        ))

    # ---------- 查询 ----------
    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def names(self) -> List[str]:
        return sorted(self.tools.keys())

    def user_tools(self) -> List[Tool]:
        return [t for t in self.tools.values() if t.origin == "user"]

    def external_tools(self) -> List[Tool]:
        """来自**外部**（登记加载）的工具 —— 不是模型此刻自己写的那些。

        「自己写的」= 距离为 0（本项目 functions 目录），判据只有 distance。
        """
        return [t for t in self.tools.values()
                if t.origin == "user" and t.source_distance != 0]

    def digest(self) -> str:
        """给提示词用的工具清单。"""
        lines = []
        for t in sorted(self.tools.values(), key=lambda x: x.name):
            lines.append(f"- `{t.signature_hint()}` — {_first(t.description)}")
        return "\n".join(lines) if lines else "（当前没有任何 function，请用 write_function 造一个）"

    def __len__(self) -> int:
        return len(self.tools)


def _first(text: str) -> str:
    text = (text or "").strip().splitlines()
    return text[0] if text else ""
