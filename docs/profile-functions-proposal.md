# `-profile`：更多的 function 来源（收敛稿）

> **一句话**：`-profile` 只回答一个问题 —— **除了本项目的 `.openthree/functions/`，还从哪里找 function？**
> 答案是多档目录。除此之外，profile 不提供任何东西。

本文取代 `profile-overlay-proposal.md`（v1/v2：三层叠加 + memory/config/prompt/knowledge 合并）与
`profile-overlay-self-review.md`（对 v2 的评审）。被砍掉的部分见 §5，砍掉的理由见 §1。

---

## 1. 划界：profile 装能力，不装身份

一个「共性」要不要进 profile，判据只有一条：

> **换到另一个项目里，它还成立吗？** 成立 → 能力，可以放；不成立 → 状态，不能放。

| 想共享的东西 | 判定 | 理由 |
|---|---|---|
| 一个 function 文件 | ✅ | 它是**能力**：一份实现，或一段「返回纯文本」的知识/规范 |
| `system-prompts.md` | ❌ | 它写的是**处境**：「你的工作目录是 X」「可用工具 38 个」「你的计划还有 2 项」—— 逐条都对，只是对另一具躯体成立。拼两份出来的不是共性，是幻觉 |
| `memory.jsonl` | ❌ | 这是**这具躯体的经历**。别人的记忆进了你的上下文，你不知道它从哪来 |
| `config.yaml` / `limits.json` / `context.json` | ❌ | 这是**这具躯体的设置**。多键文件的合并读会引出「值来自哪层」的溯源问题，收益远小于复杂度 |
| `plan.md` / `identity.json` / `logs/` | ❌ | 它此刻在干什么、它叫什么 —— 不可共享 |

所以「共性」的落点只有 functions。这也正好是本框架最自然的单位：**一个文件 = 一段可执行能力**，
外加「function 也可以只返回文字」这条既有约定 —— 共享规范不必是 prompt，可以是一个按需读进上下文的知识 function。

---

## 2. 语义

### 2.1 一档 = 一个目录

`-profile` 的每一项指向一个**含 `.openthree/` 的项目目录**（与 `-C` 同构），只取其中的
`.openthree/functions/*.py`：

```
openthree -profile ~/.openthree-profiles/base,~/work/team-tools "任务…"
```

- 支持逗号分隔，也支持重复给（`-profile a -profile b`），两种写法等价、按出现顺序拼接。
- `-profile` 是单横线长选项：argparse 显式注册即可，`-reasoning` 已有先例（`cli.py:140`）。
- 只认 `<P>/.openthree/functions` 这一种版式，与 project 完全一致。裸的 `<P>/functions/`
  不认：在 python 项目里它是常见包名，误扫等于把别人的库当工具加载。

### 2.2 距离与冲突：不新写任何裁决机制

现有的 `FunctionSource.distance`（`workspace.py:447-465`）就是同名词的唯一裁决依据，越小越近。
profile 只是往这条轴上插几档：

| 来源 | distance | 自动加载 | 说明 |
|---|---|---|---|
| project（`-C`） | `0` | ✅ | 恒为 0 —— 「以当前工作目录为准」在编码上就是这个数 |
| profile[0] | `1` | ✅ | 命令行**先写的更近**（PATH 语义） |
| profile[1] | `2` | ✅ | |
| … | `i+1` | ✅ | |
| user（`~/.openthree`） | `1_000_000` | ❌ 需 `register_function` | 全家共享，但不是命令行点名的 |
| 内置 | `10**9` | — | 任何外部同名工具都顶得掉它 |

于是：

- **project 与 profile 冲突 → project 赢**，零新代码；
- **profile 之间冲突 → 先写的赢**，零新代码；
- 覆盖会进 `Registry.overrides`，`list_functions` 的「同名覆盖」一节照旧如实列出。

`-profile` 若指到 project 自己（或重复给了同一个路径），`function_sources` 的 canonical 去重
（`workspace.py:1350-1357`）会自然处理：先加的那档（更近）留下。**不需要专门写 if。**

### 2.3 自动加载：一个不等号

`Registry.load_plan` 现在硬编码「只有 distance == 0 才自动加载」（`registry.py:190-196`），
它的本意是「存在 ≠ 可用」—— 祖先目录和用户目录里的文件，发现了也要显式 `register_function`。

换成一条数字判据：

```python
if src.distance >= USER_FUNCTION_DISTANCE:   # 原来的 `if src.distance != 0`
    continue
```

**近于 user 的档自动加载，user 及其外的档需要登记。** 理由：profile 是**命令行点名的**，
点名就是授权；而 `~/.openthree` 是整个账户共用的，不该因为某个人放过一个文件就自动执行。
这条判据仍然只有一个数，不比较 `kind` 字符串 —— 与 `FunctionSource` docstring 里
「同一件事只该有一个判据」一致。（`registry.py:25-26` 的 `from .workspace import …`
要顺手加上 `USER_FUNCTION_DISTANCE`。）

`lookup_functions`（`builtins.py:1255`）与 `register_function` 的边界检查都经由
`function_sources`，**零改动**就正确：profile 档自动出现在「搜索范围」里，那份文件显示「已生效」。

### 2.4 校验时机：早于 Runtime

`Runtime.__init__` 会 `Workspace(project_dir).init()` **落盘**（`runtime.py:83`）。
所以 profile 的校验必须在构造 Runtime **之前**：

- 每一项展开 `~`、`resolve()`、按 canonical path 去重（保留第一次出现）；
- 要求 `<P>/.openthree` 是目录；否则 stderr 报是哪个路径、缺什么，**exit 2**；
- 校验失败时项目目录也不能被创建。

`--check`（升级扫描）**不接** profiles：它扫的是「本项目对本内核版本」的漂移，
profile 不是本项目的一部分。

---

## 3. 可写：共享库也能演化

profile 的内容必须是可改的，否则「共性」只是一次性快照。但**写默认仍落 project**。

### 3.1 `write_function(name, code, target="")`

```
target=""            → 本项目 .openthree/functions/（默认，行为与今天逐字节相同）
target="~/prof/base" → 该 profile 的 .openthree/functions/
```

- 定位方式：在**已加载的** profile 档里按 canonical path 匹配
  （profile 档的根 = `src.path.parents[1]`，不必给 `FunctionSource` 加字段）。
- `target` 未命中任何已加载档 → 报错并列出现有清单。**不允许写到未加载的地方** ——
  那必然是「写了不生效」，正是要防的坑。
- 只收路径，不收序号：`list_functions` / `lookup_functions` 输出里的就是路径，
  再发明一套 `profile:1` 编号等于让「哪一层」有两个真源。
- 静态审计照旧（`_static_audit`：语法 / 公开函数 / 是否覆盖内置 / 高危调用），
  `reload_functions()` 生效的规矩照旧。想真跑一次 import 用现成的
  `verify_tool(<绝对路径>)`（它本来就收任意 `.py` 路径）。

### 3.2 影子：报告，不拒绝

写进 profile 的文件，可能在本项目里**根本不生效** —— project（或更靠前的 profile）
提供了同名工具。这是静默失败的经典形态，所以写入后必须检查并明说：

```
[ok] 已写入 ~/prof/base/.openthree/functions/foo.py
注意：本项目仍用 project 的 foo（d=0 < d=1），这份在你当前项目里**不会生效**。
     → 想改本项目的 foo：target 留空
     → 想让本项目也用共享这份：删掉或改名 .openthree/functions/foo.py
```

判据按**工具名**查（`_static_audit` 已经算好了新文件里的公开函数名），不是按文件名 ——
一个文件可以提供多个工具；按 `audit["functions"]` 逐个问 `reg.get(name)` 的
`source_file` 是不是刚写的这份。

**不拒绝，也不加 force 参数。** 「更新共享版、而本项目保留自己那份」是合法且常见的演化姿势
（先在项目里长熟，再提升上去），拒绝它就得再造一个 `--force`。

### 3.3 共享层的破坏半径更大

同一份文件被多个项目加载，一处语法错误会让**所有**项目下一次 `reload` 都吃到。
这不需要新机制来兜（静态审计已在写入口拦语法错误，`verify_tool` 可提前 import 一次），
但值得写在工具文案里：**共享层的东西，写完就自己验一遍再 reload。**

---

## 4. 三处必须跟着改的「读数」

加载面变了，看它的地方也必须变，否则模型会得到错的事实：

1. **`list_functions` 的分桶**（`builtins.py:1129-1143`）。现在按 `source_distance == 0`
   二分，profile 工具会落进「外部登记 —— lookup_functions + register_function 拿来的」，
   **而它们从没被登记过**。改成三桶，仍然只比较数字：
   ```python
   local    = [t for t in user if t.source_distance == 0]
   prof     = [t for t in user if 0 < t.source_distance < USER_FUNCTION_DISTANCE]
   external = [t for t in user if t.source_distance >= USER_FUNCTION_DISTANCE]
   ```
   （`USER_FUNCTION_DISTANCE` 从 `workspace` 导入即可，`builtins.py:44` 已在同处导入它的邻居。）

2. **处境里的一行**（`prompt.py:63-75`，`build_static_block` 的计数在 144-155）。
   N=0 时**一个字节都不能变**（静态段是缓存前缀，多一个空行就让整份工具 schema 失效）——
   所以整行条件拼接，不要用模板占位符：
   ```
   - profile：base, team（额外 1 / 2 个工具；同名时本项目优先）
   ```
   有 profile 才出现。`可用工具：…（内置 x / 自造 y）` 那行不动 —— `y` 的口径是
   「非内置」，profile 工具确实算在内，多一行说明它从哪来就够了。

3. **`self_inspect` 与横幅**（`runtime.py:634-661`）：非空时加一行 profile 路径。
   与第 2 条同源，同一句话。

---

## 5. 删掉 ancestor

「祖先目录逐级向上」这一套整体移除，**不是降级保留**。用户明确：这套设计被 profile 取代，
且此前没被真正使用，不必考虑兼容。

下表是 `grep -rn "ancestor\|祖先"` 在 `openthree/` 与 `tests/` 里的**全部真实命中**（21 处），
一处不漏才算删干净：

| 位置 | 处理 |
|---|---|
| `workspace.py:471` `_MAX_SOURCE_DEPTH = 64` | 删 |
| `workspace.py:1361-1370` 爬父目录的 `while` | 删（`_add` 保留 —— profile 循环复用它） |
| `workspace.py:451`「`kind` 只有三档：project / ancestor / user」 | 改为 project / profile / user |
| `workspace.py:458-459`「祖先每上一层 +1」 | 改为「profile 按命令行顺序 +1」 |
| `workspace.py:1352` 去重注释「user 恰在祖先链上时会发生」 | 改为「同一路径被重复点名 / 指到 project 自己时会发生」 |
| `registry.py:12` docstring「祖先目录、用户目录里的文件不会自动加载」 | 改为「user 目录里的文件不会自动加载；profile 会 —— 命令行点名就是授权」 |
| `registry.py:77-78` `source` / `label` 注释举例 `ancestor1` / `ancestor:D:\projects` | 改为 `profile1` / `profile:~/prof/base` |
| `registry.py:139`、`builtins.py:1195`、`builtins.py:1253` 注释「重建整条祖先链」 | 改为「重建整条来源表」 |
| `registry.py:191`「祖先目录与用户目录里的文件属于发现了但没被授权」 | 改写；判据本身在 §2.3 换成不等号 |
| `tool.py:79`「来源标识：project / user / `ancestor:<目录>`（逐层）/ builtin」 | 改为 project / profile / user / builtin |
| `builtins.py:871-873` `register_function`「本项目 / 祖先目录 / 用户目录」 | 改为「本项目 / 命令行点名的 profile / 用户目录」 |
| `tests/test_function_sources.py:6-7, 78, 93-94, 138, 500-508` | 祖先用例改写为 profile 用例（§9） |
| `README.md:241` `<父目录>/.openthree/functions ← 只发现（d=1，再上一层 d=2 …）` | 改为 profile 档的说明 |

**验收判据（可 grep）**：源码里 `ancestor` / `祖先` 命中数归零。

注意**不要**把 `父目录` 一起纳入判据：`builtins.py:169`（检索时跳过某目录）与
`builtins.py:478`（`write_file` 创建父目录）讲的是文件系统，与来源无关 —— 一起扫会把
两处无关代码误判成漏改，然后被迫改出错误注释。

代价：机器级的「全家默认能力」没有了。它由 `~/.openthree`（登记后生效）和一份机器级 profile
（命令行点名）接管 —— 同样是全家继承，但**是声明的，不靠目录位置去猜**。

---

## 6. 明确不提供（这份清单就是设计）

以下一项都不做，也不留开关：

| 不做 | 为什么 |
|---|---|
| memory 合并读 | 记忆是经历，不是能力（§1） |
| config / limits / context 兜底 | 多键文件合并读会引出溯源问题（「这个预算是谁定的」），收益小 |
| 提示词 off/fallback/append | 两份「你是谁」会自相矛盾；规范请写成 function 或按需读的文档 |
| `knowledge/*.md` 自动注入 | 同上。要共享知识，就写一个返回纯文本的 function —— 它天然按需进上下文 |
| `--profile-init` | `openthree -C <path> --init` 已经够了，多一个命令等于两套初始化 |
| `--profile-ro` / 只读戳 | 写权限交给文件系统（目录权限 / git review），内核不发明第二套 ACL |
| `--profile-lenient` | 严格报错（exit 2）就够；宽容模式会把打错的路径变成静默空档 |
| `list_profiles` 工具 | `list_functions` 的「搜索范围」一节已经在列，再加一个就是两个真源 |
| `promote_function` 工具 | 读源文件 + `write_function(target=…)` 两次调用即可，不必造第三个写入口 |
| 运行期增删 profile | 启动参数就是启动参数。要改就重启，别让「从哪加载」在运行中漂移 |
| profile 里再引用 profile | 不递归。要组合就一次列全 |
| sha256 校验 / 版本锁 | 共享库的本义就是「内容会变」，加锁只会让人绕过它 |

一句话：**profile 只多几条「从哪找 function」的路，别的什么都没有。**

---

## 7. 改动清单

| 文件 | 位置 | 改动 | 量级 |
|---|---|---|---|
| `workspace.py` | `Workspace` 数据类（474） | 加 `profiles: Tuple[Path, ...] = ()`（有默认值 → 现有 `Workspace(root)` 三处调用全部不动） | +1 |
| `workspace.py` | `function_sources`（1332-1380） | 加 profile 档；删 ancestor 循环 | +8 / −10 |
| `workspace.py` | `FunctionSource`（447-465） + `_MAX_SOURCE_DEPTH`（471） | docstring 改写；常量删除 | +5 / −3 |
| `registry.py` | `load_plan`（190-196） + docstring（128-141） | 判据换成一个不等号；注释改写 | +1 / −1（另有注释） |
| `cli.py` | `build_parser`（87+） | 加 `-profile` / `--profile`（`action="append"`） | +5 |
| `cli.py` | 新增 `_collect_profiles`（仿 `_collect_images`，399-417） | 切分 / 展开 / resolve / 去重 / 校验 | +25 |
| `cli.py` | `main` 里构造 `Runtime` 之前 | 校验 + 透传；失败 exit 2 | +6 |
| `cli.py` | `_print_banner` / `self_inspect` 调用处 | 透传（若需要） | +2 |
| `runtime.py` | `__init__`（55-105） | 接 `profiles` 参数，传给 `Workspace(...)` | +3 |
| `builtins.py` | `write_function`（628-704） | 加 `target` 参数 + 定位 + 影子检查 + docstring | +25 / −3 |
| `builtins.py` | `list_functions`（1129-1143） + import（44） | 三桶 | +8 / −4 |
| `prompt.py` | `STATIC_TEMPLATE`（70）+ `build_static_block`（144-155） | 条件加一行 profile | +3 |
| `tests/` | `test_function_sources.py` 等 | ancestor 用例改 profile；新增 §9 | +150 |
| `README.md` | 241（来源清单那张图） | `<父目录>/.openthree/functions ← 只发现（d=1…）` 改成 profile 档 | +2 / −2 |

整体：**改 6 个源文件、删多于加**。没有新模块、没有新工具、没有新配置文件。

---

## 8. 不变量

1. **冲突只有一个判据：distance。** project 恒 0。裁决代码里不许出现按 `kind` 字符串做相等比较。
2. **N=0 时行为不变** —— 唯一的差异是 ancestor 已删除，那是独立的一次删除（§5），由它自己的测试兜住。
3. **写默认落 project。** 随手实验不污染共享层；写共享层必须是显式的 `target`。
4. **profile 除 functions 外不贡献任何东西。** 不读它的 memory / config / prompts / identity / plan。
5. **加载面的变化必须可见** —— `list_functions` 三桶 + 处境里的 profile 行，两处都要跟上。
6. **校验早于 Runtime** —— 因为 `Runtime.__init__` 会落盘（`runtime.py:83`）。
7. **写入必须自知生效范围** —— 落到 profile 的写入，返回里必须说明它在本项目里会不会生效。

---

## 9. 测试

| # | 断言 |
|---|---|
| 1 | `-profile X` 后 X 里的工具出现在 `list_functions`，且 `source_distance == 1` |
| 2 | project 与 profile 同名 → project 赢，`reg.overrides` 有记录 |
| 3 | 两个 profile 同名 → 先写的赢；顺序反过来结论也反过来 |
| 4 | profile 里有语法错的 `.py` → 启动不崩，`reg.load_errors` 里有一条 |
| 5 | 不传 `-profile` → 工具名单与改动前逐名相同 |
| 6 | 挂一个含 `memory.jsonl` / `system-prompts.md` / `config.yaml` / `limits.json` / `plan.md` 的 profile → 这些**一个都没被读**（「不提供」的可执行断言） |
| 7 | `write_function(target=profile)` 落盘位置正确；未 `reload` 前注册表不变，reload 后可见 |
| 8 | `write_function(target=未加载路径)` → 报错并列出现有清单；文件未写 |
| 9 | 影子场景（project 有 `foo`，把 `foo` 写进 profile）→ 返回文本含「不会生效」且指出赢家 |
| 10 | `-profile` 指向不存在的路径 → exit 2，且项目目录的 `.openthree` **没有被创建** |
| 11 | profile 指到 project 自己 → 不重复加载，也不报错 |
| 12 | `grep -ri ancestor openthree/` 无命中（源码层面） |

---

## 10. 已知限制（写下来，免得被当成 bug）

- **运行期不可变**：`-profile` 是启动参数，`reload_functions` 只重扫同一批目录。改 profile 列表要重启。
- **共享层没有锁**：同一个 profile 被两个项目用，行为随其内容漂移 —— 这是共享库的本义，不是缺陷。
- **只能按名字裁决**：不提供「只在某一层改写某个工具」的语法，那要靠文件本身的位置表达。
- **不递归**：profile 里不能声明别的 profile。

---

## 11. 收尾

这份设计可以浓缩成一行代码和一个约定：

```python
# workspace.function_sources()
_add(FunctionSource("project", "project", self.functions_dir, 0))
for i, p in enumerate(self.profiles):          # 命令行点名的，自动加载
    _add(FunctionSource("profile", f"profile:{p}", p / DIRNAME / "functions", i + 1))
_add(FunctionSource("user", "user", home / DIRNAME / "functions", USER_FUNCTION_DISTANCE))
```

约定是：**profile 装能力，不装身份；距离决定谁赢，不发明第二套优先级。**
