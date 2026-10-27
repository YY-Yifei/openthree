# 方案：让每个 function 文件都是可独立运行的 CLI

> 状态（2026-10-27）：**已实施。** 落地代码在 `openthree/cli_tool.py`（CLI 骨架）、
> `openthree/tools.py`（保底通道 `python -m openthree.tools`）、
> `openthree/tool.py::is_registrable`（唯一注册判据），
> 回归锁在 `tests/test_function_cli.py`。
>
> 施工中实测推翻了本文两处论断，正文已就地改正，另有一处是文档没写到的新发现：
>
> 1. `functools.partial` **不会**泄漏成工具（它的 `__module__` 是 `functools`，
>    被 `is_registrable` 的「必须是本模块定义的函数」条件排除）；真正会泄漏的
>    只有 **lambda**（`__module__` 是本模块，`build_tool` 取 `func.__name__`
>    → 工具名变成 `<lambda>`）。
> 2. `Returns:` 段写进 docstring **不会**自动出现在 `--help` 里 —— argparse 只渲染
>    参数级 `help=`，从不渲染函数整体描述。要在 `--help` 里看到「返回什么」，
>    必须显式塞进 `description`（`cli_tool._brief` / `_returns_hint` 就是干这个的）。

判据：每个 `.openthree/functions/*.py` 与 `openthree/builtins.py` 都能
`python xxx.py --help` 看清自己、`python xxx.py <fn> --k v` 直接跑起来，
且不依赖 Agent 循环 —— 人、shell 脚本、其他 agent 都能用。

---

## 一、结论先行

| 前提 | 现状 | 判定 |
|---|---|---|
| 参数类型注解 | 35/35 全齐（`str/int/bool/Optional[int]/float`） | 可直接映射成 CLI 参数 |
| docstring `Args:` 段 | 35/35 全齐 | 可直接变成 `--help` 的说明 |
| 返回类型注解 | 35/35 全齐 | 可决定退出码 / 输出形态 |
| `Returns:` 段 | 2/35 | 待补（不影响 CLI，影响可读性） |
| CLI 入口 | **0/35** | 这是要修的 |
| 手动运行 | **完全静默**（`python safe_edit.py` → exit 0 零输出） | 这是要修的 |
| 注册判据拷贝份数 | **4 份，其中 1 份与注册器不一致** | 见第三节，这是真正的「不规范」 |

**不给任何函数加装饰器、加元数据、改签名。**
`inspect.signature` + `get_type_hints` + docstring 已够生成完整 CLI ——
因为注册器（`tool.build_tool`）本来就是这么干的。CLI 复用**同一条构造路径**，
天然一一对应，不会发散。

三句话：

1. **入口一律 `def __main__`**（dunder），尾钩 5 行；注册器**不用改一行**就会忽略它。
2. 新增零依赖模块 `openthree/cli_tool.py`：`build_tool()` 拿 schema → 翻 argparse →
   调函数 → 按返回值的 `[ok]`/`[错误]` 前缀定退出码。
3. 把**唯一一份**注册判据抽出来，注册器 / 静态审计 / verify_tool / CLI 共用它
   —— 「标准化」的落点在这里，不在尾钩上。

---

## 二、命名规约：三档命名空间

现状的判据是「`_` 开头即私有」。它能覆盖 dunder，但**语义是窄的**：没有说
「dunder 是保留给协议与入口的命名空间」。后果是模型只学会「想不注册就加下划线」，
永远不知道 `__main__` 有约定用途。

**定稿规约**（唯一一份，代码在第六节）：

| 形态 | 语义 | 是否注册为工具 |
|---|---|---|
| `__x__` | dunder：协议 / 入口（`__main__`） | **永不** |
| `_x` | 单下划线：私有辅助 | **永不** |
| `x` | 公开 | 注册 |

实测：`"__main__".startswith("_")` → `True`，所以**注册器现有代码已天然满足**，
不需要为 dunder 改一行。但这不等于规约可以不写 —— 它要写进提示词、审计、文档，
才能约束将来的人与模型。

> 反面实证：入口若叫 `main` / `cli` / `run`，会被**立刻注册成一个空参数工具**。

---

## 三、真正的「不规范」：注册判据有 4 份拷贝

| # | 位置 | 判据实现 | 与注册器一致？ |
|---|---|---|---|
| 1 | `registry.py:110-119` | `dir(module)` + callable + 非类 + `__module__` | 基准 |
| 2 | `builtins.py:1469-1479` `_static_audit` | **ast** 遍历 `tree.body` 的 `FunctionDef` | **不一致** |
| 3 | `builtins.py:1579-1589` `_VERIFY_SCRIPT` | 又抄了一遍运行时判据 | 一致但重复 |
| 4 | `builtins.py:384-389` `_outline_keep` | 名字规则（供展示） | 近似 |

第 2 处的可复现 bug：

```python
PUB_LAMBDA = lambda: "hi"

def real(path: str) -> str:
    """真的函数。"""
    return path
```

```
注册器会收： [('PUB_LAMBDA', '<lambda>'), ('real', 'real')]
静态审计：   ['real']                             ← 漏报
build_tool: attr='PUB_LAMBDA' -> tool.name='<lambda>'   ← 工具名成了 '<lambda>'
```

即 `write_function` 会告诉模型「可注册工具：real」，reload 后却多出一个叫
`<lambda>` 的工具；两个这样的 lambda 还会**同名覆盖**。

> CLI 若复用判据，判据必须先归一，否则 cli_tool 就是第 5 份拷贝 ——
> 「标准化」刚好走向反面。故落地顺序：**P0 判据归一 → P1 CLI 骨架**。

---

## 四、硬约束（已核实到源码行）

1. **schema 由「签名 + docstring」推导**：`tool.py:154-172`。CLI 必须走同一路径，
   否则会出现「模型看到 7 个参数、命令行只有 5 个」。
2. **`kernel` 首参由运行期注入**：`agent.py:912-913`。CLI 下需「离线 kernel」。
3. **一批函数只在 Agent 内成立**：`finish` / `next_round` / `compact_context` /
   `context_usage` / `context_budget` / `limits_usage` / `set_limits` 依赖
   `KERNEL.agent`。CLI 下返回 `[错误] …未就绪` —— **这是正确行为**，
   但 `--help` 必须标注，否则人一跑以为坏了。
4. **`kernel.py:14-19` 已把 `recorder` / `agent` 初始化为 `None`** ——
   离线 kernel 不需要新造语义，是既有设计。
5. **`Workspace.init()` 幂等**（`workspace.py:475`），CLI 可直接调用。

### 4.1 一条会让 P2 直接失败的事实（实测）

```
$ python openthree/builtins.py
ImportError: attempted relative import with no known parent package
$ python -m openthree.builtins
（成功）
```

包内模块的顶层 `from .kernel import KERNEL` 在 `__main__` 保护**之前**执行，
所以 `python openthree/builtins.py` **永远到不了尾钩**。

→ **结论**：`.openthree/functions/*.py` 用 `python xxx.py`；
`openthree/builtins.py` 只能用 `python -m openthree.builtins`。
两者的 `--help` 输出一致，因为入口代码是同一份。

---

## 五、设计

### 5.1 文件末尾的标准尾钩（5 行）

```python
def __main__(argv: Optional[List[str]] = None) -> int:
    """命令行入口。dunder 命名 → 注册器不会把入口本身变成工具。"""
    from openthree.cli_tool import run_cli
    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
```

刻意之处：

- **`def __main__` 而非裸尾钩**：多 2 行，换一个可 import、可测、可被其他 agent
  程序化调用的**具名**入口（`mod.__main__(["--path", "a.py"])`，不必伪造 `sys.argv`）。
- **`argv=None`**：`None` 读 `sys.argv[1:]`；传列表则完全可测。
- **函数内 import**：`reload` 会 `exec_module` 整个文件，顶层 import 会拖慢每次重载。
- 实测确认：直接运行触发、被 import 不触发、`__main__` 不进工具集。

### 5.2 两种形态

- **多函数文件 → 子命令**：`python safe_edit.py replace_in_file --path a.py ...`
- **单函数文件 → 免子命令**：`python only_one.py --path a.py`
- 判定：公开函数数 `== 1` → 平铺；`>= 2` → 子命令。
  **不做**「子命令名与参数撞车就智能猜测」——`--path` 与子命令名同占 `argv[1]`，
  只在无歧义时启用平铺。

### 5.3 参数映射

| Python 签名 | CLI |
|---|---|
| `path: str`（无默认） | `--path PATH`（必填） |
| `offset: int = 0` | `--offset 0`，`type=int`，默认值进 help |
| `show_hidden: bool = False` | `--show-hidden` / `--no-show-hidden`（`BooleanOptionalAction`） |
| `limit: Optional[int] = None` | `--limit N`；**不传 = None** |
| 名字含 `_` | `--show-hidden`，**同时接受** `--show_hidden` |
| 长文本（`content`/`code`/`old`/`new`/`block`/`summary`） | 另支持 `--x @file`、`--x -`（stdin） |

必填 / 选填**直接取 `build_tool` 的 `required`**，不另算一遍。

### 5.4 三个「不想当然就会错」的细节（两个是探针抓出来的）

**(a) 默认值要「忠实采用签名」，不是「一律改 None」。**

正确表述：**绝不人为用 `0` 顶替 `None`**。
`set_limits(max_iterations: Optional[int] = None)` 下，
不传 = 不改、传 `0` = 改成 0；若 CLI 写 `type=int, default=0`，两者被压成一个，
**静默改错运行边界**。而 `show_offset(offset: int = 0)` 的 `0` 是**真默认值**，
必须忠实采用 —— 所以规则是「照抄签名默认值」，不是「全都变 None」。

实测：`不传 -> max_iterations=None`、`传 0 -> max_iterations=0`、两者输出不同 ✅

**(b) `--json` 等全局开关必须 `default=argparse.SUPPRESS`。**

这是探针抓到的真 bug：写成普通 `store_true` 时，**子 parser 会用自身的 `False`
覆盖顶层解析出的 `True`**，于是 `python x.py --json fn ...` 静默失效、退回纯文本
—— 除非把 `--json` 写在子命令之后，而这没有任何提示。

```
修复前： python safe_edit.py --json show_offset --offset 3
        → '[ok] offset=3 ratio=0.5'        ← 不是 JSON
修复后： → {"ok": true, "fn": "show_offset", "result": "..."}
```
`SUPPRESS` 的含义是「没显式给就不写 namespace」，因此不覆盖上层结果。

**(c) `bool` 必须用 `BooleanOptionalAction`**（py3.9+，本仓库 `>=3.10`）。
`--show_hidden false` 在 `store_true` 下报错，在 `type=bool` 下**恒为 True**。

**长文本约定**：字符串参数支持 `@path` 从文件读、`-` 从 stdin 读：

```bash
python -m openthree.tools write_plan --content @plan.md
git diff | python -m openthree.tools note --thought -
```
> 注意：这是启发式 —— 真要以 `@` 开头的字面量需转义（`@@x`）。
> 探针只覆盖了 `@file` 正路，**转义分支 P1 实现时必须补一条测试**。

### 5.5 编码：决定「其他 agent 能不能用」

实测（Windows 本机，`sys.stdout.encoding = gbk`、`locale = cp936`、`PYTHONUTF8` 未设）：

```
裸 print      → 管道里 b'[ok] \xd2\xd1\xd0\xb4...'     ← GBK 字节
reconfigure  → 管道里 b'[ok] \xe5\xb7\xb2\xe5\x86\x99...' ← UTF-8
```

一个按 UTF-8 解码的 Python/Node agent 拿到前一种直接 `UnicodeDecodeError`。
本机 `sys.stdout.encoding` 是 `gbk` 而非 `cp936`，说明 Python 层已初始化，
`PYTHONIOENCODING` 改不动 —— **必须在进程内 reconfigure**。

→ 复用仓库已有的 `cli.py:38 _force_utf8()`，提到公共处，`cli.py` 与 `cli_tool.py` 共用。
→ `--help` 里承诺 **stdout/stderr 恒为 UTF-8**。

实测 3 项：合法 UTF-8 ✅、中文完整 ✅、不是 GBK 字节 ✅

### 5.6 输出 / 退出码

现有函数已统一 `[ok] …` / `[错误] …` 前缀，直接当协议，**不改任何返回值**：

| 情形 | stdout | stderr | exit |
|---|---|---|---|
| `str` 以 `[错误]` 开头 | — | 原样 | **1** |
| 其它 `str` | 原样 | — | **0** |
| `None` | `[ok]（无返回值）` | — | 0 |
| `ImageResult` | `brief` + 「图片仅在 Agent 内可见」 | — | 0 |
| 抛异常 | — | `类型: 消息`（`--traceback` 给全栈） | **2** |

三个默认关闭的开关（保证「默认输出 = 函数原文」）：

- `--json` → 单行 `{"ok":…,"fn":…,"result":…}`。作用范围见 5.6.1。
- `--quiet` → 成功时不打印，只靠退出码。
- `--traceback` → 异常时打完整栈。

#### 5.6.1 `--json` 到底解决什么、不解决什么

现有返回值是**渲染给人看的文本**，每条自带头部与中文措辞：

```
# D:\...\README.md （共 536 行，显示 1-2）
D:\...\__init__.py:20: def add(a: int, b: int) -> int:
# 检索 'def' 于 ...（命中 4 处+ / 扫描 3 个文件）
```

`--json` 把这一整段**原封不动**塞进 `result` 字段。所以它**不做结构化**，
只加一层信封：

| 买到的（均实测） | 没买到的 |
|---|---|
| **单行**：`read_file` 的几十行正文被转义成 1 行，行式消费者不会被正文里的 `\n` 打断 | 正文的结构化拆分（行号 / 文件名 / 命中数仍是文本） |
| **编码稳定**：`ensure_ascii=True` → 纯 ASCII 字节，UTF-8 / GBK / latin-1 解码器都能读 | 「是否被截断」的布尔位（仍要看正文里那个 `+`） |
| **一个字段判成败**：消费者不必知道 `[错误]` 这个中文约定 | 每函数各自的字段模型（→ `--describe`，见第八节 P4） |
| **错误也走 stdout**：纯文本模式失败走 stderr，只抓 stdout 的采集器会丢 | — |

为什么不是更省事的格式：TSV / CSV 在 `read_file` 这种多行正文上直接崩（无转义），
只有 JSON 的 `\n` 转义能同时保住「单行」与「正文原样」。

**`ensure_ascii` 必须为 `True`（这一条是实测出来的，别按直觉改回 `False`）。**
外部调用者最自然的写法是 `subprocess.run(..., text=True)` —— 不指定 `encoding`，
于是父进程按**自己**的 locale 编码解码。本机 `locale.getpreferredencoding(False)`
= `cp936`，实测：

```
子进程 ensure_ascii=True   → 父进程读到 '{"ok": true, ... "[ok] \\u5df2\\u5199\\u5165 a.txt"}'
子进程 ensure_ascii=False  → 父进程崩溃 UnicodeDecodeError: 'gbk' codec can't decode byte 0xa5
```

关键点：**子进程把自己的 stdout 设成 UTF-8 救不了父进程** —— 编解码是两侧的事。
`ensure_ascii=True` 让输出退化成纯 ASCII，于是无论对端是 UTF-8、GBK 还是
latin-1 都能原样读走。代价是中文字符变成 `\uXXXX` 转义（机器读无所谓，
人要看纯文本就别加 `--json`）。

**必须说清的一条**：`ok` 并非「独立于 `[错误]` 前缀的判据」——
它就是 `not result.startswith("[错误]")` 算出来的，与原判据同源。
所以它**不是解耦，是收敛**：把「解析中文前缀」从 N 个外部消费者
收敛进 `cli_tool` 一处。前缀约定将来若改动，改 `cli_tool` 即可，
不必逐个去改外部脚本。这个价值是真的，但比「稳定契约」小 —— 别夸大。

同理 `fn` 目前也冗余（一次调用只有一个函数），留着只为将来批量调用 /
日志聚合时不必改格式。

**已知盲区（不修，写进 `--help`）**：`run_shell("exit 1")` 返回
`$ exit 1\nexit=1`，不以 `[错误]` 开头 → CLI 退出码为 **0**。
即 `run_shell --command "make test"` 的失败**不会**传导到 CLI 退出码，
`--json` 也救不了（判据同源）。要判命令成败必须自己读正文里的 `exit=`。

**什么时候该砍掉它**：若确认只有模型与人手动跑、没有任何脚本 / CI / 其他
agent 消费，`--json` 就是死重 —— 但它默认关闭、约 10 行，主消费者
一个字节都感受不到，留着不亏。

### 5.7 离线 Kernel

```python
ws = Workspace(project_dir).init()          # 幂等
KERNEL.workspace = ws
KERNEL.registry  = Registry(ws)
for n, f in BUILTIN_FUNCTIONS.items():       # 不 reload 用户函数，避免递归
    KERNEL.registry.register_builtin(n, f)
KERNEL.recorder = None
KERNEL.agent    = None
```

`project_dir`：`--project-dir` 显式给 → 否则向上找最近的含 `.openthree/` 的目录 → cwd。

**必须写进 `--help` 的差异：**

| | Agent 内 | CLI |
|---|---|---|
| `recorder` | 进 `three-report.html` | **None，不记录** |
| `agent` | `finish`/`context_*` 可用 | **None → 返回未就绪** |
| `evolution.jsonl` | `write_function` 留一条 | **不留** |

这是划界不是妥协：**演化史记的是智能体的自我改造，人手动改的不该混进去。**
代价：CLI 改完 prompt 后 report 里看不到 —— README 要说一句。
若要留痕，给默认关闭的 `--record`。

### 5.8 自举与环境

`python xxx.py` 时 `sys.path[0]` = **文件所在目录**（不是 cwd），
所以能否 `from openthree.cli_tool import run_cli` 取决于「本 Python 有没有装 openthree」，
与「在哪个目录敲命令」无关。

本机是 **editable install**（`.pth` + finder 的 `MAPPING` 指向源码树）→ 手动跑安全。
两种会出问题的环境：
- **没装过** → `ModuleNotFoundError: openthree`
- **装了非 editable 的旧版** → `ImportError: cannot import name 'run_cli'`
  （看着像自己写错，其实版本不对）

→ 尾钩做**自举检查**：`import openthree` + `hasattr(openthree, "cli_tool")`，
失败时打印可操作提示（`pip install -e <repo>` 或 `PYTHONPATH=<repo>`），
而不是把栈糊到人脸上。

> 顺带：editable install 的**元数据停留在安装那一刻**（`pip show openthree` 报
> 0.1.0，代码实际 0.1.7）。排障别拿它当判据。

---

## 六、`openthree/cli_tool.py` 参考实现（探针验证 17/17 通过）

> 这是**跑通过的代码**，不是示意。P1 落地时可直接落盘，只补两处：
> `--record` 与 `@@` 转义，以及把 `_build` 换成 `tool.is_registrable`。

```python
"""把一个 function 文件变成命令行程序。

判据：schema 由 build_tool 从「签名 + docstring」推导 —— 与模型看到的工具
完全同源，因此不会出现「模型 7 个参数、命令行 5 个」的发散。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional


def force_utf8() -> None:
    """Windows 控制台默认 GBK：管道里吐 GBK 字节，外部 agent 按 UTF-8 解会炸。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _read_text_arg(value: str) -> str:
    """字符串参数的 @file / - 约定：从文件或 stdin 读长文本。"""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@@"):
        return value[1:]                     # 转义：字面量 @x
    if value.startswith("@"):
        return open(value[1:], encoding="utf-8").read()
    return value


def _add_globals(p: argparse.ArgumentParser) -> None:
    """三个全局开关。

    default=SUPPRESS 是必须的：否则子 parser 会把 --json 的默认 False 写回
    namespace，覆盖掉「写在子命令之前」的 --json True（实测过的 bug）。
    """
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="单行 JSON 输出：{\"ok\":..,\"fn\":..,\"result\":..}")
    p.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                   help="成功时不打印（只靠退出码），适合 && 串联")
    p.add_argument("--traceback", action="store_true", default=argparse.SUPPRESS,
                   help="异常时打印完整堆栈")


def _add_one(p: argparse.ArgumentParser, name: str, sch: Dict[str, Any],
             required: bool) -> None:
    flag = "--" + name.replace("_", "-")
    aliases: List[str] = [flag]
    if flag != "--" + name:
        aliases.append("--" + name)          # 同时接受 --show_hidden
    kw: Dict[str, Any] = {}
    if sch.get("description"):
        kw["help"] = sch["description"]
    if "enum" in sch:
        kw["choices"] = sch["enum"]

    t = sch.get("type")
    pytype = {"integer": int, "number": float}.get(t, str)

    if t == "boolean":
        p.add_argument(*aliases, action=argparse.BooleanOptionalAction,
                       default=bool(sch.get("default", False)), **kw)
        return
    if required:
        p.add_argument(*aliases, type=pytype, required=True, **kw)
        return
    # 忠实采用签名默认值：Optional[X]=None 时不给 default，argparse 自然是 None
    p.add_argument(*aliases, type=pytype, default=sch.get("default"), **kw)


def _build(funcs: Dict[str, Any]) -> Dict[str, Any]:
    """用与注册器同一条判据挑出可注册函数，再 build_tool 拿 schema。"""
    from openthree.tool import build_tool, is_registrable

    mod = funcs.get("__name__")
    out = {}
    for nm, obj in funcs.items():
        if not is_registrable(mod, nm, obj):
            continue
        out[nm] = build_tool(obj)
    return out


def run_cli(funcs: Dict[str, Any], *, argv: Optional[List[str]] = None) -> int:
    force_utf8()
    if argv is None:
        argv = sys.argv[1:]

    tools = _build(funcs)
    if not tools:
        print("[错误] 这个文件里没有可注册的公开函数", file=sys.stderr)
        return 1

    top = argparse.ArgumentParser(
        prog=sys.argv[0],
        description="function CLI：每个公开函数就是一个命令；--help 看全部。",
        epilog="提示：--json 是给其他 agent 的稳定接口；建议写在子命令之后。")
    _add_globals(top)

    single = len(tools) == 1
    if not single:
        sub = top.add_subparsers(dest="_cmd", metavar="子命令")
        for nm, t in tools.items():
            sp = sub.add_parser(nm, help=(t.description or "").splitlines()[0])
            _add_globals(sp)
            for pn, sch in t.parameters.items():
                _add_one(sp, pn, sch, pn in t.required)
    else:
        t = next(iter(tools.values()))
        for pn, sch in t.parameters.items():
            _add_one(top, pn, sch, pn in t.required)

    ns = top.parse_args(argv)
    opts = {k: bool(getattr(ns, k, False)) for k in ("json", "quiet", "traceback")}

    if single:
        name = next(iter(tools))
    else:
        name = getattr(ns, "_cmd", None)
        if not name:
            top.print_help()
            return 0

    tool = tools[name]
    kwargs: Dict[str, Any] = {}
    for pn, sch in tool.parameters.items():
        v = getattr(ns, pn, None)
        if v is None:
            continue
        if isinstance(v, str) and sch.get("type") == "string":
            v = _read_text_arg(v)
        kwargs[pn] = v

    try:
        result = tool.func(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if opts["traceback"]:
            import traceback as _tb
            _tb.print_exc()
        msg = f"[错误] {type(exc).__name__}: {exc}"
        if opts["json"]:
            print(json.dumps({"ok": False, "fn": name, "error": msg},
                             ensure_ascii=True))
        else:
            print(msg, file=sys.stderr)
        return 2

    text = "" if result is None else str(result)
    is_err = text.startswith("[错误]")
    if opts["json"]:
        # ensure_ascii=True 不是审美选择：输出变成纯 ASCII，
        # 于是「父进程按 cp936 解码、子进程写 UTF-8」这类错配不再致命。
        # 实测 ensure_ascii=False 在 Windows 本地化环境下会让调用方
        # 抛 UnicodeDecodeError（详情见 5.6.1）。
        print(json.dumps({"ok": not is_err, "fn": name,
                          "result": text or None}, ensure_ascii=True))
    elif is_err:
        print(text, file=sys.stderr)
    elif not opts["quiet"]:
        print(text if result is not None else "[ok]（无返回值）")
    return 1 if is_err else 0
```

### 6.1 `tool.is_registrable`（唯一判据）

```python
_DUNDER = re.compile(r"^__.+__$")


def is_registrable(module_name: str, attr: str, obj: Any) -> bool:
    """一个模块属性是否该成为「模型的工具」。全仓库唯一判据。

    三档命名空间：
      1. ``__x__``  dunder —— 协议 / 入口（如 __main__）。永不注册。
      2. ``_x``     单下划线 —— 私有辅助。永不注册。
      3. ``x``      公开 —— 注册为工具。
    """
    if _DUNDER.match(attr) or attr.startswith("_"):
        return False
    if not callable(obj) or isinstance(obj, type):
        return False
    return getattr(obj, "__module__", None) == module_name
```

调用方（P0 后必须全部改用它）：
`registry.py:110-119` 收集循环 / `_static_audit` / `_VERIFY_SCRIPT` / `cli_tool._build`。

---

## 七、每个文件的「规范化清单」

可机检的 6 条：

1. **模块 docstring**：首行一句话用途；正文含 `用法：` 段（`safe_edit.py` 是好样板）。
2. **每个公开函数**：类型注解 ✅、`Args:` ✅、**`Returns:`**（33 个缺）、
   docstring 首行写「什么时候该调我」（它就是工具 `description`）。
3. **文件末尾标准尾钩**（5 行）。
4. **模块级无副作用**（尾钩除外）—— `reload` 会 `exec_module`，顶层 `print`/建目录都是污染。
5. **辅助函数 `_` 前缀** ✅；**入口只许 `__main__`**，不许 `main`/`cli`/`run`。
6. **包内模块只能用 `-m` 运行**（见 4.1）。

第 1/2/3/4/5 条全部可 ast 静态检查，无需执行。

---

## 八、落地路线

### P0 —— 判据归一（40 行）

- `tool.py` 新增 `is_registrable`（6.1）。
- `registry.py:110-119` 改为调用它（行为不变：dunder 本就被 `startswith("_")` 覆盖）。
- `_static_audit`：补「模块级公开名绑定 lambda/partial」警告；
  把 `result["functions"]` 语义改名为 `def_functions`（它是静态近似，不是注册真相）。
- `_VERIFY_SCRIPT` 改为 `from openthree.tool import is_registrable`。

**验收**：新增回归 —— 塞 `main` / `__main__` / `_x` / `PUB_LAMBDA` / 类 / `partial`，
断言只有该注册的进了 `tools`。风险：低（只会多报，不会少报）。

### P1 —— CLI 骨架（不动任何 function 文件）

- 新增 `openthree/cli_tool.py`（第六节）+ `openthree/tools_cli.py`（`python -m openthree.tools`）。
- `_force_utf8` 提到公共处，`cli.py` 与 `cli_tool.py` 共用。
- 补 `--record`、`@@` 转义及其实测。

**验收**：
```bash
python -m openthree.tools --list
python -m openthree.tools read_file --path README.md --limit 5        # exit 0
python -m openthree.tools read_file --path 不存在.txt                  # exit 1，stderr
python -m openthree.tools read_file --path README.md --json            # 单行 JSON
python -m openthree.tools set_limits --max-rounds 3                    # 无 agent → exit 1
# 编码（关键）：
python -m openthree.tools read_file --path README.md | python -c "import sys;sys.stdin.buffer.read().decode('utf-8')"
```
风险：零（不碰任何现有函数）。

### P2 —— 存量加尾钩

- `.openthree/functions/safe_edit.py` 末尾 +5 行。
- `openthree/builtins.py` 末尾 +5 行（**用 `-m` 验证**，见 4.1）。

**验收**：
```bash
python .openthree/functions/safe_edit.py --help          # 2 个子命令
python .openthree/functions/safe_edit.py replace_in_file --help
python -m openthree.builtins --help                      # 33 个子命令
```
风险：低。尾钩在 `__main__` 下 `exec_module` 时**不执行**，`__main__` 又被判据排除
→ 注册器看到的工具集合一字不变（`list_functions()` 前后对比即可证明）。

### P3 —— 让规范化自持

1. `_static_audit` 加两条：缺尾钩 → 提醒 + 模板；有尾钩但缺 `run_cli` import → 报错。
2. 种子提示词「造 function 的约定」补三档命名规约 + 尾钩模板。
3. `write_function` docstring 同步（两处必须一致，否则模型只学到一半）。
4. 新增内置 `audit_functions()`：按第七节 6 条打分，**并在子进程真跑 `--help`**
   （断言 exit=0 且输出含每个公开函数名）—— 这才叫「验证过」。

**验收**：`python -m openthree.tools audit_functions`；故意删尾钩 → 立刻报出来。

### P4 —— 可选（先不做）

位置参数糖（有歧义风险）、`--describe`（输出 JSON Schema 供 agent 做工具发现）、
`--dry-run`（只能约定不能强制）、shell 补全（需 `argcomplete` 新依赖）。

### 8.1 保底通道

```bash
python -m openthree.tools read_file --path README.md --limit 20
python -m openthree.tools --file .openthree/functions/safe_edit.py replace_in_file --path a.py --old x --new y
python -m openthree.tools --list
```
即使某个文件永远不加尾钩也能跑，对**存量和未来新造的**都有效。

---

## 九、探针验收记录（17/17）

```
PASS  1a stdout 是合法 UTF-8 且中文完整
PASS  1b 不是 GBK 字节
PASS  2a 顶层 --help 列出全部公开函数
PASS  2b 私有函数不出现
PASS  2c 入口 __main__ 不出现
PASS  3a int 默认值 0 被忠实采用
PASS  4a 不传 -> None
PASS  4b 传 0 -> 0
PASS  4c 两者可分
PASS  5a --json 在子命令之前生效
PASS  6a --json 在子命令之后生效
PASS  7a 成功 exit=0
PASS  7b [错误] -> exit=1 且进 stderr
PASS  8a 无子命令打印帮助 exit=0
PASS  9a 注册集恰好 3 个公开函数
PASS  9b __main__ 未注册
PASS  9c _helper 未注册
```

**探针改掉的两处规格错误**（值得单独记）：
1. 原写「默认值必须是 `None`」—— 错。`offset: int = 0` 的 `0` 是真默认值，
   应忠实采用；真正要守的是「**不用 0 顶替 None**」。
2. 原写「`--json` 直接 `store_true`」—— 错，会被子 parser 覆盖，须 `SUPPRESS`。

---

## 十、工作量

| 项 | 规模 |
|---|---|
| `is_registrable` + P0 四处改调用 | ~40 行 |
| `openthree/cli_tool.py` | ~200 行（第六节已备好，只需补 2 处） |
| `openthree/tools_cli.py` | ~60 行 |
| 每个文件尾钩 | +5 行 |
| 审计规则 + `audit_functions` | ~80 行 |
| 33 个 `Returns:` 段 | 纯文字 |

现有函数**函数体一行不用动**，Agent 侧行为**零变化**。
