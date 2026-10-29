> **历史归档（2026-10-29）。** 本文评审的是 v2 设计（可写 overlay + memory/config/knowledge 合并）。
> 设计其后收敛为「profile 只提供额外的 function 来源」，见 `profile-functions-proposal.md`。
> 本文仍有效的部分：§5 那几条关于「一个文件提供多个工具」与 `list_functions` 三桶的发现 ——
> 它们在新稿里被原样继承（新稿 §3.2 / §4.1）。

# 自评审：`-profile` 方案 v2

> 评审对象：`docs/profile-overlay-proposal.md`（805 行，v2）
> 评审日期：2026-10-29 · 方法：逐条对着源码核，不用记忆代替证据
> 结论：**P0 可以开工**（纯删除，无设计争议）；**P0.5 的 §5 有 4 处必须先修文档，否则实现出来是错的。**

---

## 0 结论先说

这份方案的骨架是站得住的：distance 单一真源、N=0 时数值不变、提示词不进 profile。
但它有**一类系统性错误**：把「同名」当成了「同名文件」。

工具名来自**文件里的公开函数名**，不是文件名。方案的核心机制（§5.2 影子检查）按文件名推，
于是它专防的那个坑——「写了但不生效」——恰好会从它自己身上漏过去。

| 级别 | 数量 | 处置 |
|---|---|---|
| **硬伤**（实现出来是错的） | 4 | P0.5 开工前必须改文档 |
| **清单漏项**（与方案自己的判据冲突） | 4 | 随 P0 一起改 |
| **设计漏洞**（不阻塞，但会变成下一个 bug） | 10 | P0 之后逐条收 |
| **写对了、且是承重的** | 5 | 保留，别在改的时候顺手删掉 |

---

## 1 事实核对：它引用的代码，站得住吗

逐条实测（不是抽查），18 条引用全部命中，**没有一条行号是编的**：

| 方案声称 | 实测 | 结论 |
|---|---|---|
| `workspace.py:471 _MAX_SOURCE_DEPTH = 64` | 同 | ✅ |
| `workspace.py:1361-1370` 爬父目录 | 1363 `while depth < _MAX_SOURCE_DEPTH` | ✅ |
| `workspace.py:451/458-459/1352` docstring 与去重注释 | 同 | ✅ |
| `workspace.py:340-357 atomic_write` | 340 定义，tempfile + `os.replace` | ✅ |
| `workspace.py:1390-1399 function_distance` | 同 | ✅ |
| `registry.py:12 / 77-78 / 190-196` | 同 | ✅ |
| `tool.py:79` 来源标识注释 | 同 | ✅ |
| `builtins.py:871 / 931 / 1195 / 1253` | 同 | ✅ |
| `builtins.py:677` 用 `path.write_text`（非 atomic） | 同 | ✅ |
| `cli.py:140` 单横线长选项先例 `-reasoning` | 同 | ✅ |
| `runtime.py:83` `Workspace(...).init()` 会落盘 | 同 | ✅ |
| `cli.py:387` / `cli_tool.py:96` 位置构造 | 同 | ✅ |
| `llm.py:250-271` 分层 `env → user → 项目 .openthree → 项目 openthree.yaml` | 261/264/269/271，顺序一致 | ✅ |
| `prompt.py` 记忆注入是**两次全扫** | 293 `read_memory(...)` + 298 `count_memory()`（后者内部再 `read_memory()`） | ✅ 属实 |
| `read_memory` 会跳过损坏行（§5.5 的前提） | 1305-1308 `json.loads` 失败 `continue` | ✅ 属实 |
| `requires-python >= 3.10`（§7.1 `kw_only` 的前提） | pyproject `>=3.10` | ✅ |
| `registry.py:141` 复用 `function_sources()` 做族内边界 | 同 | ✅ |
| `Registry.overrides` 展示已存在（§7.9） | `builtins.py:806-811`、`1144-1149` | ✅ |

**这一项上方案是干净的。** 下面所有问题都不是「行号写错」，而是**判据与清单**。

---

## 2 硬伤：实现出来是错的

### H1（最严重）影子检查按「文件名」是错的判据 —— 必须按「工具名」

§5.2 的表格写的是「本项目已有同名（`project/foo.py` 提供 `foo`）」，
§7.3 的落点是 `resolve_target(target).dir / "functions" / f"{name}.py"`。

但本框架**一个文件可以提供多个工具**，而裁决单位是**工具名**：

- `_static_audit` 返回的是 `audit["functions"]`（复数），`builtins.py:938` 直接
  `names: List[str] = list(audit["functions"])`；
- 注册器按**函数名**建 tool，同名就按 distance 打架。

于是有一个方案完全没覆盖的组合：

```
project/toolbox.py    定义了 foo, bar        （d=0）
你要写 profile/base/helper.py，其中定义了 foo （d=1）
```

按文件名检查：`profile/base/foo.py` 不存在于 project → **放行**。
实际结果：`foo` 仍由 project 提供，**这次写入静默不生效** —— 正是 §5.2 存在的理由。

反过来也会**误拒**：project 有 `foo.py`，但你要写的 `profile/base/foo.py` 里只定义了
`bar` —— 按文件名算「同名文件存在」，实际零冲突。

**修法**（成本很低，`_static_audit` 已经算好了名字）：

```
关闭「文件名推导」，改成「符号查表」：
  names = _static_audit(code)["functions"]          # 已有
  for n in names:
      owner = reg.get(n)
      if owner and owner.source_distance < target.distance:
          → 影子（默认拒绝，force 才写）
      elif owner and owner.source_distance == target.distance \
           and owner.source_file != 目标文件路径:
          → 同层重名（另一个文件也提供了 n）→ 也算冲突，报出来
```

一个附带好处：这样写出来的错误信息能说**具体哪个符号**不生效，而不是笼统说「同名文件」。

### H2 `list_functions` 会把 profile 工具说成「外部登记」

§7.8 的工具表里**没有 `list_functions`**，§8 第 3 条说「`--functions` / `lookup_functions`
已按来源分档，天然可见」。**不成立。**

`builtins.py:1124-1143` 只有两桶：

```python
user     = [t for t in reg.tools.values() if t.origin == "user"]
local    = [t for t in user if t.source_distance == 0]     # 「自造」
external = [t for t in user if t.source_distance != 0]     # 「外部登记」
```

profile 工具 `source_kind == "profile"`、`source_distance ∈ 1..N` → 全部落进
**「外部登记（lookup_functions + register_function 拿来的）」**。

证据链完整：`tool.py:77` 的 `origin` 只有 `user` / `builtin` 两个取值，
所以 profile 工具必然在 `user` 那一桶里，再被 `source_distance != 0` 切进 external。

那是**事实错误**：它们从没被登记过，是 `-profile` 带上来的。
而这个列表是模型每轮判断「哪些工具是我自己长的」的主要依据 —— 说错就等于让它
以为自己写过一个并不存在的登记。

**修法**：按 `source_kind` 分三桶（本项目 / profile:<label> / 外部登记），
`1129-1130` 那句「『自己写的』= 距离 0」的注释同步改。

### H3 `promote_function` 与 §5.2 的默认拒绝自相矛盾

- §5.2：目标为 ❌ 时**默认拒绝**，`force=True` 才写；
- §5.4：promote 的第 4 步是「跑一遍 §5.2 的影子检查（新名字 → 立即生效；**同名 → 报告**）」。

但 promote 的定义就是「把**本项目已生效**的那份复制上去」——
那份的名字必然由 project（d=0）提供，对 profile（d≥1）**必然是影子**。
照 §5.2 的默认走，promote **永远写不进去**。

**修法**：在 §5.4 明写「影子是 promote 的正常结果，内部按 `shadow="report"` 走，
不走默认拒绝」。这是两个语义不同的模式，不能靠调用方记得传 `force=True`。

### H4 `target="user"` 写 function 是几乎无效的写

§2 / §3.1 说 user 层的 functions 可写 ✅。但 user 层**不自动加载**（要 `register_function`，
`registry.py:190-196`）。所以：

```
写 ~/.openthree/functions/foo.py
→ 本项目：没有 foo（d=1000000，未登记不加载）
→ 别的项目：也没有 foo
→ 生效范围：零。除非有人逐个项目 register_function。
```

§5.3 的三种语气没有一种能描述它，而「写工具返回的最后一行必须是生效范围」是硬规则。
一个 model 很可能以为「写进 user 层 = 全局可用」，于是写进去、reload、`list_functions` 里
没有、再写一遍 —— 标准的静默失败循环。

**修法**（二选一，建议前者）：

1. **functions 不开放写 user**（`target="user"` 仅对 memory 有效），
   并把「共享给所有项目」的唯一出口定为 profile（它自动加载，且点名即授权）；
2. 保留，但补第四种语气：`[ok] 已写入 user —— **不会自动生效**：
   每个项目都要 register_function 才看得见。想让所有项目自动拿到它，用 profile。`

> 顺带一个正面副作用值得写进文档：若某项目已登记过这个路径，写入会改变 sha256，
> 那个项目下次启动会报「已登记文件内容已变」（`registry.py:173-184`）——
> 这是可见的，不是静默的。

---

## 3 清单漏项：与方案自己的判据冲突

§4.1 立了一条很好的可执行判据：

> 任何还提到「父目录 / 祖先」的注释或文案，都属于没改干净。

按这条判据扫全仓，它的清单漏了 4 处：

| 漏项 | 内容 | 为什么必须改 |
|---|---|---|
| `registry.py:139` | 「`ws.function_source_for` 每调一次都要重建整条祖先链」 | 同一句在 `builtins.py:1195/1253` 被列了，这里没列 —— 同一句话漏一处，下次 grep 的人会以为还剩一处 |
| `builtins.py:1101` | `if src is not None and src.distance == 0:` → 「它就在本项目的 functions 目录里，会继续被自动加载」 | 它把 `distance == 0` 当「会自动加载」用。profile 也自动加载却 `d>0` —— 口径已变（见 D1） |
| `builtins.py:1129-1132` | `list_functions` 的两桶判据（见 H2） | 方案说它「零改动即正确」，实际是错的 |
| `tests/test_function_sources.py:6-7` | 模块 docstring「祖先目录 / 用户目录里的文件不自动加载」「祖先逐层 +1」 | 方案列了 71-100 / 138 / 493-510，漏了文件头的总纲 |

另外 §4.1 里「`workspace.py:451` 改三档」这一行，应当把 **452-453** 一并划进范围：

> `distance == 0`，不要比较这里的字符串：同一件事只该有一个判据。

这正是未来的人 grep「怎么判断这是本项目的」时唯一会找到的那句话。
把它留在原地，改造完的下一个人会照着它写回 `distance == 0`。

---

## 4 设计漏洞：不阻塞，但会成为下一个 bug

### D1（建议采纳）三处散落的判据 → 收敛成一个 `auto_load`

`distance == 0` 被当作「会自动加载」的代理判据，现在有三处：
`registry.py:195`、`builtins.py:931`、`builtins.py:1101`。
方案改了前两处（口径不同：一处用 `kind in (...)`，一处也用 `kind in (...)`），
第三处没提，且两处各写一份字符串元组。

不改的后果不是这次出错，是**下次再改加载面时又要满地找**。

**建议**：给 `FunctionSource` 加一个属性，判据只写一次：

```python
@property
def auto_load(self) -> bool:
    """这一档自动加载吗？—— 授权来自「位置」（project）或「命令行点名」（profile）。"""
    return self.kind in ("project", "profile")
```

三处全部改成 `if not src.auto_load:` / `if src.auto_load:`。
`registry.py:194` 那句「判据是距离，别拿 kind 做比较」的精神也保住了：
不是散落的字符串比较，是**一个有名字的判断**。

### D2 limits / context 的 provenance 会被合并读骗

§3.4 说「`limits_usage` / `context_budget` 已有 provenance，扩一个 `profile:<name>` 来源即可」。
这个「即可」低估了现状。

`agent.py:444-466` 的 provenance 是这样算的：

```python
fd = {} if locked else self._limits_file_data      # ← 只读**本地**文件
...
elif fd.get(key) is not None and key not in bad:
    source[key] = "模型自管"                        # ← 有值就说「模型自管」
```

一旦值来自 profile 兜底，`fd` 里没有这个键 → 标签会变成「内核默认」；
若把 `fd` 换成合并视图，则又会变成「模型自管」——**两种都是撒谎**。

**修法**：`read_limits_merged()` 必须同时返回 `{key: origin}`（origin ∈ `local` / `profile:<label>` / `default`），
`limits_stats` 的标签改成直接读它，不要再从「文件里有没有」倒推。

**另一件事必须写死**（这条现状是对的，别在改造时弄坏）：
`set_limits` 是**读本地 → 只写显式传入的键 → 写回本地**（`agent.py:509-518`）。
它**必须继续只读写本地**。一旦改成「读合并、写合并」，就会把 profile 的当前值**固化**进本地文件
—— 之后 profile 更新，这个项目永远收不到。写路径与读视图必须是两套。

### D3 memory 去重策略没定义

§3.2 说「按 `ts` 排序 → 按 `sha256(content)` 去重」，但没说是**保留哪一条**。

如果先排序再保留「第一条」，同一个教训的**旧 ts** 会赢；模型看到的是旧时间。
必须写清：

> 同 content 归一：**保留 ts 最大的一条**，`tags` 取并集（教训相同、标签更全）。

### D4 不变量 2 与 memory 合并读冲突，需限定作用域

§9 第 2 条：**`distance` 是唯一裁决真源**；不许新增第二种判断。
但 §3.2 的 memory 是**按内容去重、完全不分层**（更近的不赢，全部合并）。

一条只按内容去重的规则，就是第二种判断。不写清作用域，未来的人要么以为 memory 写错了，
要么把不变量 2 删掉（那更糟）。

**修法**：把第 2 条限定为「**functions 的同名裁决**唯一真源」，memory 的合并规则单列一条，
并附一句为什么它可以例外：**记忆没有「名字」，所以不存在同名裁决；它只有去重**。

### D5 写共享层的两条路径，严格度不一致

- `promote_function`（§5.4）：落盘前 `_static_audit` + **子进程 `verify_tool`**；
- `write_function(target="profile:...")`（§7.3）：只 `_static_audit`。

同一个共享层，两个入口，一个严一个松。而共享层的坏代码**一次坏 N 个项目**。

**建议**：凡目标层 `distance > 0` 的 functions 写入，一律补一次子进程验收。
开销是秒级，换来的是「共享层里永远不会有跑不起来的代码」这条可以写进不变量的性质。

### D6 `--profile-init` 是一个不带来能力的新概念

§6.5 说它创建的是**残缺骨架**（functions / knowledge / logs / workspace.json，**没有 prompts**），
紧接着又说「维护一个 profile = `openthree -C D:/profiles/base` 进去干活」——
而 `-C` 进去必然 `init()`，`init()` 会把 prompts / memory / plan 全套补齐
（`workspace.py:655-796`）。

也就是说：**残缺骨架从来不是稳定状态**，它只是被 `--init` 覆盖前的一瞬间。
这个 flag 于是只增加一个要记的命令，不增加任何能力。

**建议**：删掉它，文档改成一句 ——

```bash
# 新建共享层 = 在目标目录跑一次 --init（已有命令）
openthree -C D:/profiles/base --init
```

### D7 `profile:2`（按距离）与 `profile:base#2`（同名消歧）是两套编号

§5.1 同时给了 `target="profile:2"`（距离为 2 的那一档）和重名时的 `profile:base#2` 标签。
两套编号在同一个列表里，且**目录名恰好叫 `2` 时就撞上了**。
而且「距离为 2」= 第二个 profile，靠的是 `i+1` 这个偏移，读的人得先知道编号规则。

**建议**：只认名字。同名用 `#2` 消歧（那是**标签**，不是索引）。
`list_profiles()` / `--inspect` 已经把可用写法列出来了，不需要第二个寻址方式。

### D8 profile 列表的来源，应当是安全不变量（现在只是实现细节）

§6.3 说合并来源是「命令行 + `OPENTHREE_PROFILES` + `~/.openthree/config.yaml`」，
但这是写在**语法章**里的，§9 的不变量里没有它。

而它是全篇最重要的一条安全边界：

> **profile 里的 `.py` 会被 import 执行。**

所以只要有人把 `profiles:` 也加进**项目内**的配置文件（`.openthree/config.yaml` /
项目根的 `openthree.yaml`）—— 一次 `git clone` + `openthree` 就等于**任意代码执行**，
而且是自动的、静默的。

**修法**：写进 §9 ——

> **profile 列表只能来自：命令行 / `OPENTHREE_PROFILES` / 用户级 `~/.openthree/config.yaml`。
> 绝不读项目内任何文件。** 项目内的文件可以被别人写进 git。

这一条比 §9 现有任何一条都更该被守住：其余失守是「行为不对」，这条失守是「被入侵」。

### D9 `--check` 路径根本没接 profiles

§7.4 要求「`--check` / `-u` 只扫 project，但把已挂载的 profile 打印在表头」。
但 `cli.py:387` 是 `ws = Workspace(project_dir)` —— **一个参数**，
既没有 profiles，也没有经过 §7.4 那个「在 Runtime 之前」的解析。

要么把解析出的 profiles 传进去，要么明确写「`--check` 不显示 profile，
表头只说『只扫 project』」。两句话不能都要。

### D10 `__post_init__` 里做存在性校验，会跟 `--profile-lenient` 打架

§7.1 的落点是：

```python
self.profiles = normalize_profiles(self.profiles)   # expanduser → resolve → 去重 → 校验
```

而 `--profile-lenient`（§6.4）要求「路径有问题时降级为警告并跳过」。
校验若在构造函数里抛，CLI 的降级逻辑就没有机会跑 —— 它得在构造**之前**已经过滤完，
于是「在同一处做两件事」变成了「两处都要记得做」。

而且 `Workspace(...)` 的构造点有 40+ 处（3 处生产 + 全部测试），
每处都会开始为每个 profile 做 `is_dir()`。

**修法**：拆成两个纯/不纯函数，边界清楚：

| 函数 | 在哪跑 | 做什么 | 会抛吗 |
|---|---|---|---|
| `normalize_profiles` | `__post_init__` | strip / expanduser / resolve / 保序去重（**纯字符串**） | 否 |
| `validate_profiles` | **只在 CLI** | 存在性 / `.openthree` 结构 / lenient 降级 | 是（CLI 转成退出码 2） |

构造函数永不因 profile 抛错 —— 这条本身值得进 §9。

---

## 5 这份方案写对了、且是承重的

评审不能只列错。以下五处是它真正的价值，改的时候别顺手删掉：

1. **§4「N=0 时数值一字不差」** —— 把「行为不变」从一句承诺变成了一个**可断言的等式**，
   而且是用「这一档不存在」实现的，不靠 if。§10 里那条「与基线快照逐条相等」因此可写。
2. **§3.5「提示词是处境，不是规范」** —— 全篇最硬的论证。它把「为什么不叠加」从
   「怕打架」升级到了「拼出来的是幻觉：两个工作目录、两套工具、两个名字，逐条都对，
   只是对另一具躯体成立」。这条论证一旦删掉，§12.6 那道阻力就没了依据。
3. **§4.1 的验收口径**（「源码里再出现父目录/祖先都算没改干净」）—— 可 grep、可判定。
   虽然清单漏了 4 处（§3），但**判据本身是对的**，比「记得改干净」强得多。
4. **§6.4「校验必须发生在 Runtime 构造之前」** —— 抓住了 `runtime.py:83` 会 `init()` 落盘这个事实。
   §10 里那条「断言 `.openthree/` 不存在」是同一条洞察的可执行版本。
5. **§12.6 最后那句**「若哪天这条阻力被绕开，应当回头查是谁想要它」—— 罕见的、
   对未来的自己预设怀疑的写法。保留。

---

## 6 下一步

| 顺序 | 动作 | 理由 |
|---|---|---|
| 1 | 改 §5.2（按符号检查）+ §5.4（promote 走 report 模式）+ §5.3（补 user 层语气） | 这三条不改，P0.5 实现出来是错的 |
| 2 | 改 §7.8（补 `list_functions` 三桶）+ §4.1（补 4 处 + 452-453） | 随 P0 一起，边界小、可一次改干净 |
| 3 | 改 §9（补 D8 的安全不变量、限定 D4 的作用域、加 `auto_load` 判据） | 不变量是给未来的人看的，现在写最省 |
| 4 | 删 §6.5 的 `--profile-init`，改成 `-C PATH --init` | 少一个概念 |
| 5 | D2 / D3 / D5 / D10 写进对应章节的落地要求 | 留给 P1/P2 之前 |

**P0 不必等这些。** 它是纯删除 + 一个属性，删多于加，且 §10 的 N=0 快照测试
能证明「行为一字不差」。P0 落地之后，上面这些问题全部只影响 P0.5，不影响已交付的部分。
