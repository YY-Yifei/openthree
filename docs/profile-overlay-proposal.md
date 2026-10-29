> **已被 `profile-functions-proposal.md` 取代（2026-10-29）。** 设计收敛为「profile 只提供额外的
> function 来源」：memory 合并读、config/limits 兜底、提示词 fallback、knowledge 注入、
> `promote_function`、`list_profiles`、只读戳等全部砍掉。本文保留作历史归档，不代表当前设计。

# `-profile`：可叠加、可演化的多层工作区（设计提案 v2）

> 一句话：`-profile A,B` 把若干**别的** `.openthree/` 叠到当前工作区上；
> 读的时候同名冲突一律**以当前工作目录为准**，写的时候**默认也落在当前工作目录**，
> 只有显式点名目标层时才写进共享层。
>
> **挂载的是能力与经验，不是身份**：`prompts/` 与 `identity.json` 永远只属于 project（§3.5）。
>
> 目录逐级向上找（ancestor）那一套**移除** —— profile 是它的替代，而且是更好的那个：
> 声明式（写在命令行上）而不是猜（顺着父目录爬），并且**可写**（共享层也能演化）。

---

## 0 两个决定，以及它们各自的理由

**决定一：ancestor 移除。**

原来的「逐级向上找 `.openthree/`」有两个毛病：

1. **它是猜出来的。** 你在 `D:\projects\foo` 下工作，内核会去看 `D:\projects\.openthree`，
   再去看 `D:\.openthree`。挂了什么、为什么挂上，用户没有表达过，只有目录结构在暗示。
   换台机器、换个 clone 位置，加载面就变了 —— 这不是一个可复现的运行时。
2. **它只解决了一半。** 就算发现到了，还得 `register_function` 才加载（「存在 ≠ 可用」）。
   于是「团队共享工具库」这个真实需求，被拆成「碰巧在父目录」+「逐个人工登记」两步。

profile 把这两步合成一步：**你在命令行上写出来的，就是你授权加载的**。位置不再有语义，
列表顺序才是语义。

> 代价：`~/projects/.openthree` 这种「放一份全家继承」的用法没有了。
> 补偿是 `~/.openthree/config.yaml` 里的 `profiles:` 列表（§6.3）—— 同样是「机器级默认」，
> 但它是**声明**的，不是靠目录位置猜的。

**决定二：profile 可写。**

只读的共享库不符合这个框架的设定。openthree 的能力是在循环里长出来的：
它今天写下的 `pdf_extract`，明天就该能被另外九个项目用上；反过来，
在项目 A 里踩到的一个坑，也应当能写回共享层的记忆里，让项目 B 别再踩。

但这不等于「随便写」。可写带来三个必须回答的问题，§5 整章就是答案：

1. **写到哪一层？** → 默认 project；显式 `target` 才写别层。
2. **写进去会不会有效？** → 写之前按距离做影子检查，写了也看不见就**拒绝**，不静默。
3. **别人同时在写怎么办？** → 只在能保证原子性的资源上开放写入（§5.5）。

一句话：**可写范围 = 「这一层拥有的资产」，不是「这台机器上的任何文件」。**

---

## 1 现状：单根假设落在哪些地方

这套设计能小成本落地，是因为现有代码已经有两处分层先例；方案只是把它们接到同一条轴上。

| 事实 | 位置 | 对方案的意义 |
|---|---|---|
| 工作区是**单根**的：`Workspace(root)` → `dir = root/.openthree` | `workspace.py:481-485` | 读路径接受多档，**写路径默认一行不用动**（全部由 `ws.dir` 派生） |
| functions 已有「来源档 + 距离裁决」：`project(0)` / `ancestor(depth)` / `user(1e6)` | `workspace.py:1332-1380` | profile 就是**替换掉 ancestor 那一档**，裁决逻辑复用 |
| 同名由 `distance` 唯一裁决，近者生效 | `registry.py:128-216`、`workspace.py:1390-1399` | 冲突规则不需要新写，只需要**给 profile 编号** |
| 自动加载的判据是 `distance == 0` | `registry.py:190-207` | 要扩到 profile 档（§4） |
| `register_function` 的族内边界复用同一个 `function_sources()` | `registry.py:141`、`builtins.py:908-914` | profile 一进 `function_sources`，边界检查自动包含它 |
| 写盘已有原子实现 `atomic_write`（临时文件 + `os.replace`） | `workspace.py:340-357` | 共享层的单文件写入可以直接复用，不必新造 |
| LLM 配置已有分层：`env → 用户级 → 项目级 → 根 openthree.yaml` | `llm.py:250-278` | profile 插在「用户级」与「项目级」之间 |

结论：**profile 不是第二种工作区，而是同一套工作区视图上多出来的、可写的共享层。**

---

## 2 层模型：谁可写、写什么

| 层 | 来源 | 距离 | 自动加载 functions | 可写 |
|---|---|---|---|---|
| `project` | `-C/--dir`（默认 cwd）下的 `.openthree/` | `0` | ✅ | ✅ **默认写目标** |
| `profile[i]` | `-profile` 第 i 个目录下的 `.openthree/` | `i + 1` | ✅（命令行点名 = 授权） | ✅ **显式指定时** |
| `user` | `~/.openthree/` | `1_000_000` | ❌（要 `register_function`） | ✅ 显式指定时（仅 §3.1 / §3.2 的资源） |
| `registered` | 登记簿里的单文件 | `1_000_001` | ✅（已授权） | — |

**四条判据：**

1. **读：project 永远赢。** `distance` 是唯一真源，profile 只是 1..N 那几档。
   「有冲突时以当前工作目录为准」在编码上的落点就是 `project == 0`。
2. **写：project 永远是缺省。** 不传 `target` 时行为与今天逐字节相同 —— 随手写的实验
   不该污染共享库。**「本地优先」是双向的默认，写共享层是显式的例外。**
3. **profile 之间按命令行顺序**：先出现的更近（`A,B` 里 `A` 压过 `B`）。
   顺序是用户唯一能表达的优先级，不再有任何隐式规则。
4. **user 层地位不变**（可发现、不自动加载、可登记、可写）。
   若有人把 `~/.openthree` 也写进 `-profile`，它就变成 profile 档并**自动加载** ——
   同一个目录被点名，语义就变了，这是想要的：点名即授权。canon 去重只保留更近的那条。
5. **身份不参与叠加。** 挂载一个 profile 只借它的**能力与经验**（functions / memory / 配置），
   不借它的名字与处境：`prompts/`、`identity.json`、`plan.md`、`logs/` 永远只属于 project（§3.5）。

---

## 3 资源矩阵：哪些叠加、怎么叠加、能不能写

不同资源语义差别很大，用一条规则套会把它们弄坏。按「读法」分四类，并给每一类划清写边界。

### 3.1 A 类：叠加（多源共存，按距离裁决）

| 资源 | 读法 | 冲突裁决 | 可写层 |
|---|---|---|---|
| `.openthree/functions/*.py` | 全部加载 | 近者生效（project < profile[0] < profile[1] < … < user） | project ✅ / profile ✅ / user ✅ |
| `.openthree/knowledge/*.md` | 按名字查，近者生效 | 同上 | project ✅ / profile ✅（P3 新增，现状代码里还没有这一档） |

这是 profile 的主战场：一份「通用工具库」`~/.openthree-profiles/backend` 被十个项目共享，
而它的第 11 版可以在任何一个项目里被改出来（§5）。裁决逻辑、覆盖记录
（`Registry.overrides`）、报告展示都已存在，**零新增机制**。

### 3.2 B 类：合并读（多源合并成一份视图）

| 资源 | 读法 | 去重 | 可写层 |
|---|---|---|---|
| `memory/memory.jsonl` | 逐层读 → 按 `ts` 排序 → 取尾部 N 条 | 按 `sha256(content)` 去重 | project ✅ / profile ✅（append） |

记忆是最适合叠加的资源：它是**经验**，不是**身份**。一个 profile 把领域踩坑记住，
所有挂它的项目都受益；反过来，项目里学到的东西也能写回 profile（§5.3）。

注入点必须收敛成**一次扫描**：现在 `prompt._memory_block` 先 `read_memory(limit=…)`
再 `count_memory()`，已经是两次全扫；乘以 N 层会变成 `2N` 次文件扫描，而这发生在**每一步**。

> 落地要求：新增 `Workspace.memory_snapshot(limit, query) -> (items, total)`，
> 一次扫描同时给出两者；`read_memory` / `count_memory` 改成它的薄封装（对外签名不变）。
> 每个文件按 `(mtime_ns, size)` 做进程内浅缓存，跨步不重复解析。

### 3.3 C 类：不叠加（单值真源，永远是 project）

| 资源 | 当前读点 | 理由 |
|---|---|---|
| `prompts/system-prompts.md` | `ws.read_system_prompt()` | 「一」写的是**这一层**的自我与处境（§3.5） |
| `prompts/user-prompts.md`、`current-task.json` | `ws.current_task_text()` | 任务只能是「这里」的任务 |
| `prompts/plan.md` | `prompt._plan_block` | 计划是当下这具躯体的待办，跨工作区合并没有语义 |
| `identity.json` | `prompt._identity_line` | 名字表示「这具躯体是谁」。**能力可借，身份不能借** |
| `logs/sessions/`、`transcript.jsonl`、`assets/` | `recorder` / `append_turn` / `stage_images` | 运行状态，属于本次执行 |
| `functions.json`（登记簿） | `ws.read_functions_meta()` | 它是**本工作区的授权记录**，不该被 profile 带进来 |
| `versions/`（提示词历史） | `snapshot_system_prompt` | 历史必须是本地的，否则 `restore` 会把人吓一跳 |
| `channels.yaml` + `channels/` | `channels/config.py` | 频道带 token 与游标，是**身份 + 状态** |
| `workspace.json`（结构戳） | `read_workspace_meta` | 只属于 `init()` / `upgrade` 的本地口径 |

C 类全部**不可写进 profile**，也不从 profile 读。这一栏的判据很干脆：
*它描述「此刻是谁、在干什么」，还是「这里有什么能力」？* 前者一律 C 类。

### 3.4 D 类：单值，profile 兜底（只读合并，不开放写）

| 资源 | 合并语义 | 可写层 |
|---|---|---|
| `config.yaml`（`llm` 节） | `env < 用户级 < profile[远] < profile[近] < 项目级` | **仅 project** |
| `limits.json` | 键级兜底：本地没写这个键，取**最近**的、写了这个键的 profile | **仅 project** |
| `context.json` | 同上 | **仅 project** |

**为什么 D 类不开放写：** 这三份都是**多键共享文件**，写一个键就得读-改-写整份，
而 profile 可能同时被 N 个进程持有 —— 那是丢更新的经典配方（§5.5）。
而且它们的写需求很弱：本地写一条就够，兜底只用来「少配一遍」。

每处都要能回答「这个值是谁定的」：`limits_usage` / `context_budget` 已有 provenance，
扩一个 `profile:<name>` 来源即可。

### 3.5 提示词与身份：不进 profile

先把「prompt」在这套设计里是什么说清。

内核（ReAct 循环、工具注册器、schema、上下文与边界自管）**与模型实现无关、与挂载无关**，
它不读任何提示词。`.openthree/prompts/system-prompts.md` 是**工作区里的一个文件**：
`init()` 铺一份种子，`write_system_prompt` 覆写它，旧稿进 `prompts/history/`。
它是**这一层工作区此刻的自我定义** —— 组装物，不是内核参数。

于是 profile 的定位可以用一句话说清：**profile 是可挂载的能力与经验层，不携带身份。**

由此这里没有开关，只有一条规则：**profile 的 `prompts/` 与 `identity.json` 一律不读、不写。**

理由不是「两份「一」拼起来会打架」（那是表象），而是一条更硬的：
这份文件里写着**这具躯体自己的处境** —— 它的工作目录、工具清单、上下文读数、计划与记忆。
把 profile 的版本拼接进来，等于同时告诉模型两个工作目录、两套工具、两个名字。
那些字句逐条都对，只是对另一具躯体成立。**拼出来的不是共性，是幻觉。**

**那共性规范写在哪？** 写在**工具**里，不写在提示词里：

1. 把规范写成一个**返回纯文本的 function**（「function 也是知识」）——
   模型需要时才调，规范进上下文；不需要时不占静态前缀，也不跟本地人格冲突。
   这是本框架已有的一等形态，不必新增机制（§12 风险 6 记着这条退路）。
2. 把长文档写成 `.openthree/knowledge/*.md`，配 `read_knowledge(name)` 按需读（P3）。
   它等价于「一个知道去哪读的 function」，与 `read_file` 没有本质区别。
   **按需读进上下文的知识，不是静态前缀里的 prompt** —— 这条界线要守住。

**规范是能力，人格是身份。** profile 只装前者。

> 一个推论：`write_system_prompt` 永远只改 project（§7.3）。
> 要维护某个 profile 自己的「一」（如果它也想有个名字），就把它当普通工作区打开
> （`openthree -C D:/profiles/base`，§6.5）。但那属于「在 profile 里干活」，
> 不属于「挂载 profile」—— **挂载从不带走它的提示词与名字。**

---

## 4 距离编号：唯一裁决依据

```python
# workspace.py
def function_sources(self, include_missing: bool = False) -> List[FunctionSource]:
    ...
    _add(FunctionSource("project", "project", self.functions_dir, 0))
    for i, prof in enumerate(self.profiles):                     # 新增
        _add(FunctionSource("profile", f"profile:{prof.name}",   # 新增
                            prof / DIRNAME / "functions", i + 1))# 新增
    # ancestor 那整段 while 删除（连同 _MAX_SOURCE_DEPTH）
    if home is not None:
        _add(FunctionSource("user", "user", home / DIRNAME / "functions",
                            USER_FUNCTION_DISTANCE))             # 不变
```

三条性质：

1. **近者生效**：`project(0) < profile[0](1) < profile[1](2) < … < user(1e6) < registered(1e6+1)`。
2. **`N = 0` 时与今天完全一致**：只剩 `project(0)` 与 `user(1e6)` 两档，数值一字不差。
   所以「不传 `-profile` 时行为不变」不是靠小心翼翼的 if，而是靠**这一档不存在**。
3. `distance` 仍是**唯一**裁决真源：`function_source_for` / `function_distance` /
   `lookup_functions` 的状态推算 / `Registry._note_conflict` 全部经由它，不需要各自加分支。

示例（`-profile A,B`）：

```
project .openthree/functions        d=0     可写（默认目标）
profile A/.openthree/functions      d=1     ← 先写的更近
profile B/.openthree/functions      d=2
user ~/.openthree/functions         d=1000000
```

### 4.1 ancestor 移除清单（一次改干净）

| 位置 | 现状 | 改成 |
|---|---|---|
| `workspace.py:471` | `_MAX_SOURCE_DEPTH = 64` | **删除** |
| `workspace.py:1361-1370` | `while depth < _MAX_SOURCE_DEPTH:` 爬父目录 | **删除整段**，换成 profile 循环 |
| `workspace.py:451` | `FunctionSource` docstring：「`kind` 只有三档：project / ancestor / user」 | 改三档为 `project / profile / user` |
| `workspace.py:458-459` | 「祖先每上一层 +1」 | 改成「profile 按命令行顺序 +1」 |
| `workspace.py:1352` | 去重注释「user 恰在祖先链上时会发生」 | 改成「user 被同时点名为 profile 时会发生」 |
| `registry.py:12` | docstring「祖先目录、用户目录里的文件不会自动加载」 | 改成「user 目录里的文件不会自动加载；profile 会，因为命令行点名就是授权」 |
| `registry.py:77-78` | `source` / `label` 注释举例 `ancestor1` / `ancestor:D:\projects` | 改成 `profile1` / `profile:backend` |
| `registry.py:190-196` | `if src.distance != 0: continue` | `if src.kind not in ("project", "profile"): continue` |
| `tool.py:79` | 「来源标识：project / user / `ancestor:<目录>`（逐层）/ builtin」 | 改成「project / `profile:<名字>` / user / builtin」 |
| `builtins.py:871-873` | `register_function`：「本项目 / 祖先目录 / 用户目录」 | 改成「本项目 / 命令行点名的 profile / 用户目录」 |
| `builtins.py:931-936` | `if src.distance == 0` →「无需登记」 | 改判 `src.kind in ("project", "profile")`：两档都自动加载，登记多余 |
| `builtins.py:1195`、`1253` | 注释「每次调用都会重建整条祖先链」 | 改成「重建整条来源表」 |
| `builtins.py:1226-1233` | `lookup_functions` docstring「沿项目逐级向上找」 | 改成「项目 + 命令行点名的 profile + 用户目录」 |
| `tests/test_function_sources.py:71-100`、`138`、`493-510` | 祖先用例 | 改写为 profile 用例（§10） |

**判断口径**：移除后 `function_sources()` 最多返回 `1 + N + 1` 项（project + N 个 profile + user）。
任何还提到「父目录 / 祖先」的注释或文案，都属于没改干净。

---

## 5 写入：目标、检查、生效范围（本方案的核心）

「可写」不是「把 `ws.dir` 换成别的目录」那么简单。下面四条必须一起成立，
否则共享层会变成一个静默失败的泥潭。

### 5.1 写目标：一个显式的 `target` 参数

所有写工具新增同一个参数（默认空 = 当前项目，行为与今天逐字节相同）：

```
target=""                 → project（默认）
target="project"          → project
target="profile:base"     → 命令行点名的那个 profile（按目录名匹配）
target="profile:2"        → 距离为 2 的那一档（= N 很大时省得记名字）
target="user"             → ~/.openthree
```

`target` 的解析结果是一个 `WriteTarget(kind, label, dir, distance, writable)`：

- **名字 → 路径**只在本次启动的 `self.profiles` 里查，不做任何模糊匹配；
  找不到就报错并列出可选值（`project` / `profile:base` / `profile:web` / `user`）。
- 同一个目录名重复出现时（`a/base` 与 `b/base`），标签自动加序号（`profile:base#2`），
  横幅里如实展示（§8）。
- 跑 `--inspect` / `list_profiles()` 就能看到所有可用写法 —— 不要求用户记。

**不新增第二个参数体系**：`overwrite` / `force` 保持现有含义，只是多作用于一档。

### 5.2 写前检查：影子、覆盖、只读

按顺序过三关，**任何一关不过就不写**（dry-run 式地先算，再落笔）：

**① 只读检查**

- profile 目录里有 `.openthree/readonly` 文件 → 该层只读；
- 或启动时 `--profile-ro=base,web` 点名 → 本次运行只读；
- 命中时返回：`[错误] profile:base 是只读层（<path>/.openthree/readonly 存在）`。

**② 影子检查（最重要的一条）**

写进 profile 之后，本项目**到底跑哪份**？按距离算一遍：

| 情形 | 本项目立即生效？ | 其他挂载它的项目 |
|---|---|---|
| 新工具名，本地与本项目都还没有 | ✅（reload 后 profile 自动加载） | ✅ |
| 本项目已有同名（`project/foo.py` 提供 `foo`） | ❌ project 赢 | ✅ |
| 更近的 profile 已有同名 | ❌ 更近者赢 | 看各自的挂载顺序 |
| 目标层已有同名（覆盖它） | 视上面三条 | ✅ 拿到新版 |

第三列为 ❌ 时，写入**默认拒绝**（`force=True` 才写），错误信息要把输赢说清：

```
[错误] 写进 profile:base 不会在本项目生效：
  foo 现在由 project（d=0）提供，profile:base 是 d=1。
  → 想改本项目的 foo：target 留空（或 "project"）
  → 想改共享的 foo：先确认本项目不再需要它，删掉 project/foo.py 后再写
  → 确实要写一份「影子」：force=True
```

**③ 覆盖检查**

目标层已有同名文件时，沿用现有 `overwrite` 语义。但提示文案要升级：
**这不是改一个本地文件，这是改九个项目的下一次启动**。

### 5.3 写完之后：如实报告生效范围

写工具返回的最后一行必须是「生效范围」，不许只说「已写入」：

```
[ok] 已写入 profile:base → D:\profiles\base\.openthree\functions\pdf_extract.py
  生效范围：本项目立即生效（新工具名）；所有挂载 profile:base 的项目下次启动生效。
  本次改动已记入 profile:base 的演化日志（谁从哪个项目改的）。
```

对应的三种语气（按 §5.2 表格）：

- **本项目立即生效** → 「已生效。reload_functions 后 list_functions 里能看到它。」
- **本项目不生效（被更近的同名盖住）** → 不写，除非 `force`；写了就明确说「影子」。
- **覆盖了共享层已有的版本** → 「这会影响所有挂载它的项目；旧版本 sha256 = …，
  已在演化日志里留痕。」

### 5.4 `promote_function`：把长熟了的能力提升到共享层

演化最常见的动作不是「一开始就写进共享库」，而是「在项目里长出来 → 用熟了 → 提升上去」。
所以给一个专门的动作，而不是让模型手抄代码：

```
promote_function(name, to="base", overwrite=False, note="")
```

它做的事：

1. 找到**本项目目前生效**的那份（`reg.get(name).source_file`，必须 `distance == 0`）；
2. 在落盘前跑一遍现有审计（`_static_audit`）+ 子进程 `verify_tool` —— 共享层不接受
   跑不起来的代码，这比本地严格一档是合理的；
3. 复制（**不是移动**）到 `profile:<to>/functions/`，`atomic_write` 落盘；
4. 跑一遍 §5.2 的影子检查（新名字 → 本项目也立即生效；同名 → 报告）；
5. 记双向审计（§5.6），返回「已提升 + 生效范围 + 需要的话下次怎么回滚」。

> 为什么不移动：移动会让本项目立刻失去这个工具（若没有更远层兜底）。
> 提升的语义是「这份代码从此有第二个家」，不是「搬家」。

### 5.5 并发：只在能保证原子性的资源上开放写

共享层可能同时被 N 个进程持有，所以写入按资源分级：

| 资源 | 写方式 | 为什么安全 |
|---|---|---|
| `functions/*.py` | 整文件 `atomic_write`（临时文件 + `os.replace`） | 要么旧版完整，要么新版完整；**最后写者赢**，可接受（共享库本该用 git 管） |
| `knowledge/*.md` | 同上 | 同上 |
| `memory.jsonl` | **append 单行**（一次 `write`，不读改写） | 追加不会覆盖别人的行；损坏行读取时已被跳过（`read_memory` 现有行为） |
| `limits.json` / `config.yaml` / `context.json` | **不开放**（§3.4） | 多键读-改-写会丢更新 |

「最后写者赢」是可接受的，因为共享层真正的版本管理工具是 **git**，不是内核。
内核的责任只是：**绝不写出半截文件**，并且**把这次改动记下来**（§5.6）。

### 5.6 审计：谁从哪个项目改了什么

写 profile 是**跨项目生效**的改动，只记在本地 `logs/evolution.jsonl` 不够 ——
profile 的维护者看不到。所以双向记：

```
# <profile>/.openthree/logs/profile-evolution.jsonl（append）
{"ts": "...", "event": "write_function", "tool": "pdf_extract",
 "action": "create", "sha256": "...", "from_project": "D:\\projects\\foo",
 "from_identity": "…"}
```

本地同时记一条 `wrote-into-profile`（含目标层与路径），于是「我这次动了共享库」
在本地报告里也查得到。

这条日志是**append-only 的平文本**：谁想审计、想回滚，直接 `git log` 或看这个文件，
不需要内核提供额外命令。

### 5.7 只读戳：给共享库一个刹车

三种只读来源，优先级从高到低：

1. `.openthree/readonly`（文件存在即只读）—— 落在共享库里，跟着 git 走，团队一起生效；
2. `--profile-ro=A,B` —— 本次运行临时锁（CI 里很有用）；
3. 文件系统权限 —— 最后一道，内核不管。

命中只读时**报错而不是跳过**：静默跳过会让模型以为写成功了，然后反复重试。

---

## 6 CLI 契约

### 6.1 语法

```
-profile A,B,C            逗号分隔（主用法）
--profile A --profile B   可重复，等价于追加
-p A,B,C                  短别名
--profile-ro=A,B          本次运行把这几层锁成只读
--profile-init=PATH       创建/初始化一个 profile（等于在这个目录跑一次 --init 的 functions 部分）
--profile-lenient         profile 路径有问题时降级为警告（默认严格，见 §6.4）
--no-profile              忽略环境变量与 config 里的 profiles，只用命令行给的
```

> 单横线长选项在 argparse 里合法，本仓库已有先例（`-reasoning`，`cli.py:140`）。

### 6.2 解析规则

对每个元素依次：

1. `strip()`，空项丢弃（`A,,B` → `A,B`）；
2. `expanduser()`；相对路径按**当前进程 cwd** 解析（不是 `-C` 指定的目录 ——
   profile 是本机共享库，与项目位置无关），并 `resolve()`；
3. 若路径**本身**就叫 `.openthree`（`p.name == DIRNAME` 且 `p.is_dir()`）→ 取 `p.parent`；
   否则要求 `<p>/.openthree/` 是目录；
4. `canon_path` 去重；与 project 自己的 `.openthree` 相同的项**丢弃并提示**（自己叠自己没有意义）；
5. **顺序保留**，即优先级：先出现的更近。

### 6.3 三处来源的合并顺序

```
最终列表 = 命令行 + 环境变量 OPENTHREE_PROFILES + ~/.openthree/config.yaml 的 profiles:
```

- 命令行在最前（最优先），依次往后排；
- `--no-profile` 只保留命令行给的（忽略环境变量与 config）；
- 这是 ancestor 的替代：**机器级默认层写进 config，而不是靠目录位置猜**。

```yaml
# ~/.openthree/config.yaml
profiles:
  - D:/openthree-profiles/base
  - D:/openthree-profiles/backend
```

### 6.4 错误处理：默认严格

| 情形 | 行为 |
|---|---|
| 路径不存在 | **报错，退出码 2**，不进入 Runtime |
| 路径存在但没有 `.openthree/` | **报错，退出码 2**，并打印「你给的是 X，我找的是 X/.openthree」 |
| profile 里的 py 语法错 / import 炸 | 不退出：进 `Registry.load_errors`，横幅与 `--inspect` 里以警告列出（与 project 内坏文件同待遇） |
| profile 文件与 project 同名 | 正常覆盖，进 `Registry.overrides`，不算错误 |
| `--profile-lenient` | 前两条降级为警告并跳过（给 CI / 机器间路径不一致留出口） |

**校验必须发生在 `Runtime` 构造之前。** `Runtime.__init__` 会 `Workspace(...).init()` 写盘
（`runtime.py:83`），参数错了不该以「先建了一个工作区」为代价。`--check` 已经是这么做的
（`cli.py:211-237`），沿用同一位置。

### 6.5 `--profile-init`：profile 自己也是一个工作区

因为可写，profile 需要一个最小骨架。`openthree --profile-init D:/profiles/base` 创建：

```
D:/profiles/base/.openthree/
├── functions/            # 共享工具（就是主战场）
├── knowledge/            # 按需读的领域文档
├── logs/profile-evolution.jsonl
└── workspace.json        # 结构戳：让 openthree -C 这里 也能正常工作
```

于是**维护一个 profile = 在一个工作区里工作**：

```bash
openthree -C D:/profiles/base "把 pdf_extract 的 ocr 分支补上，跑通再提交"
```

这一条不新增任何概念 —— profile 就是一个工作区，只不过它常常被别的项目挂在 `-profile` 上。

---

## 7 代码落点

### 7.1 `workspace.py`（核心）

```python
@dataclass
class Workspace:
    root: Path
    dir: Path = field(init=False)
    profiles: List[Path] = field(default_factory=list, kw_only=True)   # 新

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.dir = self.root / DIRNAME
        self.profiles = normalize_profiles(self.profiles)   # expanduser → resolve → 去重 → 校验
        self._last_image_error = ""
```

`kw_only=True` 需要 Python ≥ 3.10（本仓库 `requires-python = ">=3.10"`）；
现有位置调用（`runtime.py:83`、`cli_tool.py:96`、`cli.py:387`）不受影响。

新增方法：

- `normalize_profiles(paths)`：模块级纯函数，做 §6.2 的第 2~4 步。CLI、`cli_tool`、测试共用。
- `profile_dir(i)` / `profile_label(i)`：`profile:<目录名>`，重名加 `#2`（§5.1）。
- `function_sources()`：替换 ancestor 段（§4）。
- `write_targets() -> List[WriteTarget]`：所有可写档（project / profile… / user），
  每档带 `kind / label / dir / distance / writable / readonly_reason`。
  **这是写工具的唯一查找入口** —— 别让每个工具自己拼路径。
- `resolve_target(target: str) -> WriteTarget`：解析 §5.1 的写法，失败抛 `ValueError`
  （调用方转成 `[错误] …`，附可选值清单）。
- `memory_layers()` / `memory_snapshot(limit, query)`：§3.2。
- `read_limits_merged()` / `read_context_policy_merged()`：D 类兜底（只读）。

不改：

- 所有 `*_dir` / `*_file` 属性（它们是**默认写**路径，仍是 project 的）；
- `init()` / `required_entries()` / `write_workspace_meta()`：profile 不参与 workspace 的结构构建，
  也不该被 `openthree -u` 扫（§7.9）。

### 7.2 `registry.py`（一行判据 + 注释）

```python
# load_plan()，原：
if src.distance != 0:
    continue
# 改为：
if src.kind not in ("project", "profile"):
    continue
```

注释一并改写成：

> 只自动加载**本项目**与**命令行点名的 profile**。用户目录里的文件属于
> 「发现了但没被授权」，要加载必须显式 `register_function`。profile 之所以例外，
> 是因为它出现在命令行上本身就是授权。

其余零改动：`_module_name_for` 用 `f"{src.kind}{i}"`，profile 天然得到 `profile0` 前缀；
`_evict_foreign_helpers` 按目录隔离；`_note_conflict` 按距离算 `winner/loser`。

### 7.3 写工具：统一加 `target`

| 工具 | 改动 |
|---|---|
| `write_function(name, code, overwrite, target="")` | `ws.function_path(name)` → `ws.resolve_target(target).dir / "functions" / f"{name}.py"`；落盘用 `atomic_write`（现在是 `path.write_text`，`builtins.py:677`）；返回里带「生效范围」（§5.3） |
| `delete_function(name, target="")` | 只能删目标层的；删 project 的时**必须报告下一层是否顶上**（同名浮现） |
| `remember(content, tags, target="")` | append 到目标层 `memory.jsonl`（一次 `write`，不读改写） |
| `promote_function(name, to, overwrite=False, note="")` | **新工具**，§5.4 |
| `list_profiles()` | **新工具**：每层一行（label / 路径 / d / 可写 / 工具数 / 记忆数 / 最近一次改动） |
| `audit_functions(run_help, target="")` | 可指定层；空 = 所有可写层一起体检 |
| `write_system_prompt` / `write_user_prompt` / `write_plan` | **不加 `target`**：C 类，永远是 project（§3.3） |

`write_function` 的 `overwrite` 语义不变，但**覆盖 profile 时**要额外打一句
「这会影响所有挂载它的项目」（§5.2 ③）。

### 7.4 `cli.py`

- `build_parser()` 增加 §6.1 的六个参数。
- `main()` 在建 `Runtime` 之前：

```python
profiles, err = resolve_profiles(
    args.profile,
    env=os.getenv("OPENTHREE_PROFILES"),
    config=user_config_profiles(),
    project_dir=project_dir,
    ignore_env=args.no_profile,
    lenient=args.profile_lenient,
)
if err:
    print(err, file=sys.stderr)
    return 2
```

- `Runtime(..., profiles=profiles, profile_ro=args.profile_ro,
  profile_prompts=args.profile_prompts)`。
- `--profile-init=PATH` 在解析之后、Runtime 之前单独处理（建骨架后退出，码 0）。
- `--init` / `--check` / `--upgrade` 也接受 `-profile`：`--check` / `-u` **只扫 project**，
  但把「已挂载的 profile」打印在表头，避免人误以为漂移清单包含了它们。

### 7.5 `runtime.py`

```python
def __init__(self, ..., profiles: Sequence[Path] | None = None,
             profile_ro: Sequence[str] | None = None,
             profile_prompts: str = "off") -> None:
    ...
    self.ws = Workspace(self.project_dir, profiles=list(profiles or [])).init(...)
    ...
    self.config = load_config(self.project_dir, model_override=model,
                              profiles=self.ws.profiles)
```

`self_inspect()` 增加一段（格式见 §8）。

### 7.6 `prompt.py`

- `build_static_block()` 增加一行 profile 读数，**只在有 profile 时才出现**：

```
- profile：base（可写·12 个工具）、web（只读·4 个工具）—— 同名冲突时以本工作区为准。
```

> 实现细节：这一行必须**整行条件拼接**（`profile_line = f"\n- profile：…" if ws.profiles else ""`），
> 而不是在 `STATIC_TEMPLATE` 里留一行占位 —— 留占位的话 `profiles == []` 时会多一个空行，
> system 字节就变了（违反 §9 不变量 1），缓存前缀也跟着失效。

这一行的价值是防三类误判：以为 profile 的工具是自己写的；去 profile 目录里改文件；
把 profile 的记忆当成本工作区的记忆。**可写之后还要加一句**：「写 profile 默认不影响本项目」。

- `_memory_block` 改调 `ws.memory_snapshot()`，条目后标注来源层（`[profile:base]`），
  只在多于一层的条目上标注。返回条目在 `{ts, content, tags}` 之外多带 `layer` 字段 ——
  增量字段，`recall_memory` 的调用方不受影响。

### 7.7 `llm.py`

```python
def load_config(project_dir=None, model_override=None, profiles=()):
    ...
    layers.append(user)                      # 2. 用户级
    for p in reversed(profiles):             # 3. profile（远 → 近；后写覆盖前写）
        layers.append(_pick_llm_section(_load_yaml(p / DIRNAME / "config.yaml")))
    layers.append(project)                   # 4. 项目级（永远最后 = 最强）
```

注意 `reversed`：合并是「后覆盖前」，而 `profiles[0]` 最近，所以要**最后**写它 ——
倒序 append。这个顺序与 §4 的距离语义严格一致，别凭直觉写。

### 7.8 `builtins.py` 其余

| 工具 | 现状 | 需要做什么 |
|---|---|---|
| `lookup_functions` | 遍历 `function_sources()` | 零改动即正确；docstring 去掉「逐级向上」 |
| `register_function` | 族内边界用 `function_sources()` | 零改动即正确；§4.1 表里那两处文案要改 |
| `verify_tool` | 只认 `ws.functions_dir` | 加 `target`，否则没法验收写进 profile 的工具 |
| `read_prompts` | 只读本地 | 保持 |

### 7.9 `upgrade.py` / `report.py`

- `openthree -u` **只扫 project**：profile 是别的项目 / 团队共享的资产，本项目的升级
  没权力改它。`--check` 表头写清这一点。给 profile 体检是另一个入口（`-C <profile> -u`，§6.5）。
- 身份卡与报告顶部列出各层（名字 + 路径 + 工具数 + 记忆数 + 可写性）。
- 已有的「同名覆盖」展示（`builtins.py:806-811`）会自动包含 profile 之间的覆盖，
  只是 `winner_kind` / `loser_kind` 多出 `profile` 这个值 —— 确认展示文案能容纳。

---

## 8 可见性：加载即执行、写入即跨项目，都必须说出来

**加载面**（profile 里的 `.py` 会被 import 执行）：

1. **启动横幅**（`cli._print_banner`）：

```
profile  D:\profiles\base        可写 · 12 个工具
profile  D:\profiles\web         只读 ·  4 个工具（1 个同名被 base 覆盖）
```

2. **`--inspect`**：每档一行，含距离、文件数、覆盖数、最近一次改动（读 `profile-evolution` 尾条）。
3. **`--functions` / `lookup_functions`**：已按来源分档，天然可见。
4. **`three-report.html`**：各层清单 + 覆盖清单 + 本次运行写过的共享层。

**写入面**（一次写入会影响别的项目）：

5. 每个写工具的返回都带「生效范围」（§5.3）—— 不许只说「已写入」。
6. profile 的演化日志按 §5.6 落盘；`list_profiles()` 能回看。
7. 提示词里说明「profile 可写，但写它默认不影响本项目」（§7.6）。

另有两条「说清楚」：

- profile 内的 py 加载失败 → 横幅与 `--inspect` 里以**警告**列出文件名，不静默吞掉。
- 只读层被写 → 报错并给出原因（`readonly` 文件 / `--profile-ro` / 权限）。

---

## 9 不变量（动这里要守住）

1. **默认路径零差异**：`profiles == []` 时，`function_sources()` 的距离、顺序、标签，
   以及 system 提示词字节，与今天完全一致。
2. **`distance` 是唯一裁决真源**：不许新增「先到先得 / 谁先加载谁赢」的第二种判断。
3. **写默认落 project**：不传 `target` 时，写入路径仍全部由 `ws.dir` 派生 —— 这一条
   保证「随手写的实验不污染共享库」。任何写工具漏了 `target` 的解析，就是 bug。
4. **写共享层必须过三关**（只读 / 影子 / 覆盖），并把结果如实说出来。
   **不许静默写入一个「不会生效」的位置** —— 那比报错更糟。
5. **共享层只在原子性可保证的资源上开放写**：单文件 `atomic_write`、记忆 append。
   多键配置一律不写（§3.4 / §5.5）。
6. **授权 = 命令行点名**：profile 自动加载不削弱「存在 ≠ 可用」—— user 目录仍必须
   `register_function`。
7. **加载即执行 ⇒ 可见；写入即跨项目 ⇒ 可见**。
8. **profile 不参与 `init()` / `upgrade` / `versions/` / 登记簿**。
9. **profile 的工具必须自包含**：不跨层 `import` 别的 profile 的辅助模块
   （`_evict_foreign_helpers` 按目录清 `sys.modules`，跨层 import 不可靠）。
10. **profile 不携带身份**：`prompts/`、`identity.json` 不从 profile 读、也绝不写进去。
    system 提示词里关于 profile 的那一行，是**本层对加载面的读数**（「我挂了谁」），
    不是别层的提示词（「我是谁」）—— 这两件事不许混成一段拼接文本。

---

## 10 测试计划

新建 `tests/test_profile.py`；改写 `tests/test_function_sources.py` 里 4 处祖先用例。

**解析（CLI 契约）**
- `A,B` / `A,,B` / 重复项 / `~` / 相对路径 / 直接给 `.openthree` 本身
- 不存在 → 退出码 2 且**未创建工作区**（断言 `.openthree/` 不存在）
- 有目录但无 `.openthree/` → 退出码 2，错误信息含「你给的是 X，我找的是 X/.openthree」
- `--profile-lenient` → 跳过并继续
- 命令行 + 环境变量 + config 三处合并顺序；`--no-profile` 只留命令行

**距离与裁决**
- `N=0` 时 `function_sources()` 的 `(kind, label, distance)` 与基线快照逐条相等
- `N=2` 时 `0 < 1 < 2`，且**不存在任何 ancestor 档**（断言 `kind` 集合 == {project, profile, user}）
- project 的 `foo.py` 盖住 profile 的 `foo.py`（`reg.get("foo").source_distance == 0`）
- `profile[0]` 盖住 `profile[1]`；`reg.overrides` 记到对的 `winner/loser`
- profile 里的 `_helper.py` 不被加载

**加载面**
- 只有 project 与 profile 的 py 被自动加载；user 目录的 py 不会被自动加载
- 父目录放一个 `.openthree/functions/*.py`，**确认它完全不出现**（ancestor 已移除的回归）
- profile 里的语法错文件只进 `load_errors`，不影响启动
- `register_function` 接受 profile 路径；对 profile 文件调它返回「无需登记」

**可写：目标解析**
- `target=""` / `"project"` / `"profile:base"` / `"profile:2"` / `"user"` 各自解析到对的目录
- 未点名 / 不存在的 profile 名 → `[错误]` 且**未写任何文件**，错误信息列出可选值
- 目录名重复时 `#2` 标签可用

**可写：三关检查**
- 只读戳：`.openthree/readonly` 存在 → 写入报错，目标目录文件清单不变
- `--profile-ro` → 同上，且不改磁盘
- 影子：project 已有同名 → 写 profile **被拒绝**；`force=True` 才写，且返回含「影子」
- 覆盖 profile 已有文件 → `overwrite=False` 拒绝；`True` 通过且返回含「影响所有挂载它的项目」

**可写：生效范围**
- 新工具名写进 profile → reload 后**本项目也能用**（profile 自动加载）
- 同名被 project 压住 → 写入被拒；`force` 写了之后，本项目仍是 project 那份
- 删掉 project 的 `foo.py` 后 → profile 的 `foo` **浮现**生效；`delete_function` 的返回事先就说了
- `promote_function`：只复制不移动；本项目仍能用；目标层落盘内容 == 源内容；
  跑不起来的代码被拒绝（子进程验收）

**可写：并发与审计**
- 两个进程各自 append 一条记忆 → 两行都在（不覆盖）
- 写入 profile 后：`<profile>/.openthree/logs/profile-evolution.jsonl` 有新行，
  含 `from_project`；本地 `evolution.jsonl` 也有 `wrote-into-profile`
- 写 `limits.json` 到 profile → **报错**（D 类不开放写）

**读隔离（C 类）**
- 把 profile 里塞满 `plan.md` / `user-prompts.md` / `identity.json` / `channels.yaml` /
  `logs/` / `functions.json`，断言本地值完全不受影响

**资源叠加（P1/P2）**
- memory：三层合并 → 按 `ts` 排序 → 去重 → `limit` 生效；`count_memory` / `read_memory` 口径一致
- `memory_snapshot` 每步只扫一次（mock `open` 计数，或断言每文件只读一次）
- config：`env < 用户级 < profile[远] < profile[近] < 项目级`，逐键断言
- limits / context：本地缺键时取最近 profile 的值，来源串以 `profile:` 开头

---

## 11 分期

| 期 | 内容 | 触碰文件 | 可交付价值 |
|---|---|---|---|
| **P0**（小） | **移除 ancestor** + 解析 + 距离 + 自动加载 + 横幅/`--inspect` + 测试 | `workspace.py`（删祖先段、`profiles`、`function_sources`）、`registry.py`（一行）、`cli.py`、`runtime.py`、`report.py`、`tool.py`、`builtins.py`（文案）、`tests/test_function_sources.py` | **能力共享**：一份通用工具库被多个项目复用，且加载面可复现 |
| **P0.5** | **可写**：`WriteTarget` + `resolve_target` + 三关检查 + `write_function`/`delete_function`/`remember` 的 `target` + `promote_function` + `list_profiles` + 双向审计 + 只读戳 | `workspace.py`、`builtins.py`、`prompt.py`（那一句） | **共享层可演化**：能力在项目里长熟后被提升上去 |
| **P1** | memory 合并读（`memory_snapshot` 收敛 + 缓存）+ 提示词里的 profile 读数 | `workspace.py`、`prompt.py` | **经验共享**：踩过的坑跨项目生效 |
| **P2** | config / limits / context 的兜底与 provenance（只读） | `llm.py`、`agent.py`、`workspace.py` | **配置共享**：端点、模型、预算一次配好 |
| **P3** | `knowledge/*.md` + `read_knowledge`（按需读的知识，不是提示词叠加） | `builtins.py` | **规范共享**：共性约定按需进上下文，不占静态前缀 |

P0 是纯重构（删多于加），P0.5 才是新能力。两者分开，出问题时容易定位。

### 命令行长什么样（P0.5 完成后）

```bash
# 基础能力包 + 领域包，前者更近
openthree -profile ~/.openthree-profiles/base,~/.openthree-profiles/backend "修一下这个 N+1"

# 机器级默认层写进 config，就不必每次敲
# ~/.openthree/config.yaml: profiles: [~/.openthree-profiles/base]

# 新建一个共享层
openthree --profile-init D:/profiles/base

# 维护共享层 = 在它里面当一个普通工作区干活
openthree -C D:/profiles/base "把 pdf_extract 的 ocr 分支补上"

# CI 里挂共享层但不许改
openthree -profile D:/profiles/base --profile-ro=base "跑一遍回归"
```

---

## 12 风险与开放问题

1. **「写了但不生效」是这个设计最容易长的坑。** 缓解：§5.2 的影子检查默认拒绝、
   §5.3 强制返回生效范围、§10 专门一组测试。这三道一起才够 —— 少一道就会退化。
2. **N 变化会改变 profile 的距离数值。** 不影响正确性（只比较大小），但测试应断言
   **相对关系**而不是绝对数值（`N=0` 那条除外，那里必须断言绝对相等）。
3. **profile 之间的依赖**：A 的工具调用 B 里的辅助模块。`_evict_foreign_helpers` 按目录
   清 `sys.modules`，跨层 import 不可靠。约定：**profile 的工具必须自包含**（§9 第 9 条）。
4. **可写让「共享库」有了被单个项目带偏的风险。** 缓解：默认写 project、
   promote 之前跑子进程验收、只读戳 + git。真正的治理工具是 git review，不是内核。
5. **`memory.jsonl` 跨项目合并会越来越长。** P1 落地时就要定上限与裁剪口径
   （按 `ts` 取尾部 N 条 + `sha256` 去重已经够用；若真撑不住，再谈分层归档）。
6. **「把规范写进提示词」的诱惑。** profile 一旦能提供提示词，就会有人把「团队约定」
   拼进 system，或者让新工作区「继承人格」。定死「不进 profile」是有意的阻力（§3.5）；
   真需要共性时用「返回纯文本的 function」或 `knowledge/*.md` —— 它们按需进上下文，
   可控、不占前缀、不与本地「一」打架。**若哪天这条阻力被绕开，应当回头查是谁想要它。**
7. **channels 要不要继承**：本方案定为不继承（token / 游标是身份与状态）。
   若确有多项目共用同一个机器人账号的需求，应做成「频道配置的独立文件 + 显式路径引用」。
8. **user 层要不要也被 profile 吞掉？** 现在二者并存：`~/.openthree` 作为 user 永远是
   「可发现、不自动加载」，点名成 profile 才自动加载。语义有点绕，但两个方向都真实需要
   （既要「机器级默认能力」，也要「一个不自动加载的公共工具箱」）。暂不动，留待 P2 之后回看。
