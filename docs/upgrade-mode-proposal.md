# 方案：`openthree -u` —— upgrade 模式

> 状态（2026-10-27）：**已实施。** 实现见 `openthree/upgrade.py`（判据与扫描）、
> `openthree/cli.py`（`-u` / `--check` / `--dry-run` 与 `:staged` / `:apply` / `:undo`）、
> `openthree/workspace.py`（`required_entries()` / `workspace.json` / `seed_text()`）。
> 测试：`tests/test_upgrade_scan.py`（判据）、`tests/test_upgrade_cli.py`（命令行）。
>
> 目标一句话：`openthree -u` 读一遍**当前项目的 `.openthree/`**，把它相对
> **本机安装的内核**差了什么算清楚、摆成人能读的清单，然后开一个交互会话，
> 由模型逐条给出升级指导、人来确认/补充，确认后才落盘。

---

## 一、结论先行

| 问题 | 现状 | 本方案的答案 |
|---|---|---|
| `.openthree/` 知道自己由哪个内核铺的吗 | **不知道**（无任何版本戳） | 新增 `.openthree/workspace.json`：`{schema, kernel_version, seeds{path→sha256}}` |
| 老工作区能升级吗 | **不能**。`Workspace.init()` 只看「文件在不在」，不看「内容是否落后」 | 拆出「检测」与「施工」两件事：检测是纯函数，施工走人确认 |
| 谁来判「差了什么」 | —— | **内核算**（确定性清单）+ **模型判**（结合项目实际的建议）。两者不混 |
| 谁来动手 | —— | 模型提议 → **人确认** → 内核落盘（两阶段，可回滚） |
| 「多出来的内容」算漂移吗 | —— | **不算**。多出 = 个体所有，必须保留。只报「缺」与「旧」 |

三句话：

1. **检测不调模型。** `drift()` 是一个纯函数：读工作区 + 读当前内核的种子 →
   出一份结构化清单。它可以在 CI 里跑（`-u --check`），也可以被测试逐项断言。
2. **指导由模型给。** 把清单交给一次正常的 ReAct 循环（不新写引擎），模型据此
   写议程、逐项解释、征求补充。它已有 `write_plan` / `write_system_prompt` /
   `write_function` / `audit_functions`，一行新机制都不需要。
3. **落盘由内核办。** 覆写型动作分两阶段：模型只写「提案」到
   `.openthree/upgrade/proposal-<ts>.md`，人 `:apply` 之后内核才动文件，
   动之前打快照、动之后记 manifest。`:undo` 按 manifest 整体还原。

---

## 二、判据：什么叫「漂移」

这是整套设计的**唯一判据**，先把它钉死，后面所有实现都只是它的推论。

### 2.1 三类漂移，一类免疫

| 类 | 判据 | 例 |
|---|---|---|
| **缺（missing）** | 内核声明必备，工作区没有 | 没有 `memory/`、没有 `context.json`、提示词缺「自管运行边界」整节 |
| **旧（outdated）** | 有，但版本落后于本机内核 | `workspace.json.kernel_version = 0.1.4 < 0.1.7`；`schema = 1 < 2` |
| **不合规（nonconforming）** | 有、版本也不算旧，但不符合当前规范 | `functions/x.py` 缺模块 docstring / 函数缺 `Returns:` / 无命令行尾钩 |
| **多（extra）** | 工作区有，内核种子里没有 | **不报。** 那是个体长的东西 |

### 2.2 为什么「多」必须免疫 —— 本仓库自己就是病例

实测（2026-10-27，对 openthree 仓库自身）：

```
种子 _SYSTEM_PROMPT_SEED      5548 字符
磁盘 prompts/system-prompts.md 5823 字符
```

diff 出来的东西**两个方向都有**：

- **磁盘缺**（内核新增，该吸收）：整段「三档命名（`__x__`/`_x`/`x`）+
  每个文件加命令行尾钩」——这是后来做 function-CLI 规范化时加进种子的。
- **磁盘多**（个体所有，该保留）：整节「写给长存的读者（注释 / README / 文档）」，
  是这个工作区的智能体自己写进去的。

**同一个 diff 文件里，一条该抄回去、一条该留着。** 任何硬编码的升级器都无法
区分这两者 —— 这就是为什么「检测」必须保持机械（只报缺/旧/不合规），
而「要不要吸收、怎么吸收」必须交给模型 + 人。

顺带一条施工时必须知道的坑：`Workspace.analysis_sections()` 现在把**代码块里的
`# 注释`也当成标题**（判据只是 `line.strip().startswith("#")`）。上例里那条
「缺失章节 # 命令行入口（命名成 dunder…）」就是假阳性 —— 它躺在 python 示例
代码块里。真正用它做漂移判据之前，必须先让它跳过 ` ``` ` 围栏。否则清单第一屏
就是假警报，人会直接不信整份报告。

---

## 三、缺口（证据）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 版本号只有两处，同为 `0.1.7` | `openthree/__init__.py:35`、`pyproject.toml:8` |
| 2 | 工作区**没有任何版本戳** | `.openthree/` 实测：`context.json` / `identity.json` / `limits.json` / `README.md` / `prompts/` / `functions/` / `logs/` / `memory/` / `assets/`，无一处记版本 |
| 3 | `init()` 幂等但**不升级**：`if not X.exists()` 才写 | `workspace.py:491-527` |
| 4 | 种子是 7 份模块级私有常量，无访问器 | `workspace.py:50/207/216/224/235/245/260` + `_ensure_readme():529` |
| 5 | 必备清单硬编码在 `init()` 的两个循环里，**没有声明式真源** | `workspace.py:493-505`（目录）、`507-524`（文件） |
| 6 | 已有可复用的「规范化体检」判据（6 项） | `builtins.py:870-915` `_audit_module()`、`918` `audit_functions()` |
| 7 | 已有可复用的版本存档 / 回滚（但只管提示词） | `workspace.py:665 snapshot_system_prompt()`、`684 list_prompt_versions()`、`763 restore_system_prompt()` |
| 8 | `-u` 未被占用（现有短参数 `-C -m -n -i -q -V`） | `cli.py:73-141` |

第 5 条是**必须一起解决的问题**：如果 `drift()` 自己再列一遍「该有哪些文件」，
本仓库就有第 2 份必备清单 —— 而 `docs/function-cli-proposal.md` 记着的教训正是
「注册判据有 4 份拷贝，其中 1 份与注册器不一致」。所以：

> **`Workspace.required_entries()` 是唯一真源。`init()` 与 `drift()` 都从它读。**

再加一条对称的：种子内容也要有**公开访问器**（`Workspace.seed_text(kind)`），
否则 `drift()` 得 `from .workspace import _SYSTEM_PROMPT_SEED`——私有常量一旦
被外部 import，就再也没法自由重构了。

---

## 四、设计

### 4.1 CLI 形状

```
openthree -u                  升级模式：扫描 → 报告 → 进交互会话（人确认后才落盘）
openthree -u --check          只扫描，打一份清单就退出（不调模型、不改文件）
openthree -u --dry-run        扫描 + 让模型出提案，但不落盘（等价于「看方案」）
openthree -u --yes            不逐项问，凡「低风险动作」自动接受（高风险仍要问）
```

规矩：

- **`-u` 与任务参数互斥**：`openthree -u "改个 bug"` → 报错退出 2
  （「升级模式下不接受任务参数」）。含糊的行为比没有更糟。
- **`-u` 隐含交互**：它就是 `-i` 的一个特化入口 —— 升级必须在人面前做。
  stdin 不是终端时（CI / 管道）**只允许** `--check` / `--dry-run`，否则拒绝并说明。
- **`-u` 与 `--init` 互斥**：`--init` 是铺新工作区，`-u` 是让已有工作区跟上，
  两个语义会在「这个工作区到底该以谁为准」上打架。
- 退出码：`0` 无漂移或人确认完成；`1` 有漂移未处理 / 人中途退出；`2` 环境错误
  （配置缺失、参数冲突）。与现有 `cli.py:239` 的口径一致。

### 4.2 状态文件：`.openthree/workspace.json`

```json
{
  "schema": 1,
  "kernel_version": "0.1.7",
  "seeded_at": "2026-10-27T11:20:03",
  "upgraded_at": null,
  "upgrades": 0,
  "last_proposal": null,
  "seeds": {
    "prompts/system-prompts.md": {"sha256": "9f2c…", "kernel_version": "0.1.7"},
    "README.md":                 {"sha256": "41ab…", "kernel_version": "0.1.7"}
  }
}
```

三条设计要点，每条都有理由：

1. **`schema` 与 `kernel_version` 分开。** 结构可以变而包不必发版（也可能包发了版
   但结构没动）。混成一个数，就无法回答「要不要动文件结构」和「要不要动文案」
   这两个不同的问题。

2. **`seeds` 只登记「结构型」种子 —— 目前只有 `system-prompts.md` 和 `README.md`。**
   `plan.md` / `current-task.json` / `user-prompts.md` / `identity.json` 是**运行内容**，
   不是模板：把它们的 hash 记下来、再拿种子去「对齐」，等于清空计划、重置任务、
   抹掉名字。它们只参与「在不在」判定，永不参与内容对齐。

3. **`sha256` 记的是「当时那份种子的 hash」，不是「当前磁盘内容的 hash」。**
   它回答的是唯一一个问题：**`磁盘现在这份 == 当初铺的那份种子吗？`**
   - 相等 → 没人动过 → 可以安全地整文件换成新种子（`replace`）；
   - 不等 → 已经被模型/人改过 → **只能**提议补丁（`merge`），绝不许整文件覆盖。

   这一条是整套安全性的地基：`-u` 永远不覆盖「有主张的文件」。

老工作区没有 `workspace.json`：视为 `kernel_version: null`（legacy）。升级的第一动作
就是把它补上，且**把当前磁盘内容当作 baseline**（`sha256` 记为 `null`，即
「来历不明」）—— 于是所有结构型种子都会走 `merge` 分支，逐段提议、逐段确认。
对老工作区来说这恰恰是正确且安全的默认。

### 4.3 唯一真源：`Workspace.required_entries()`

```python
@dataclass(frozen=True)
class Entry:
    path: str          # 相对 .openthree 的路径
    kind: str          # "dir" | "seed" | "content" | "policy"
    seed: str = ""     # kind == "seed" 时对应哪个种子
    since_schema: int = 1
```

`init()` 改成遍历 `required_entries()`（行为逐字不变，`tests/test_workspace.py`
是它的守卫），`drift()` 也从同一份清单读。`Entry.kind` 决定升级能对它做什么：

| kind | 含义 | 允许的动作 |
|---|---|---|
| `dir` | 只是目录 | `create` |
| `seed` | 受内核管理的模板（提示词、README） | `create` / `replace` / `merge` |
| `policy` | 结构化策略（`context.json` / `limits.json`） | `create` / **补缺失键**，不改已有值 |
| `content` | 运行内容（`plan.md` / `current-task.json` / `identity.json` / `user-prompts.md`） | `create`（缺了才建），**永不覆盖** |

### 4.4 扫描器：`drift()` —— 纯函数，不调模型

```python
def scan_upgrade(ws: Workspace) -> UpgradeReport
```

报告是一组结构化条目，**每条都带「我建议怎么做」和「为什么」**：

| 动作类型 | 触发 | 风险 | 谁来动手 |
|---|---|---|---|
| `create` | 必备项缺失 | 低（只新增） | 模型可直接做 |
| `fill` | `policy` 缺键 / 提示词缺整节 | 中 | 需人确认 |
| `replace` | 种子被内核更新，且磁盘仍是原样种子 | 中（但可整体回滚） | 需人确认 |
| `merge` | 种子被内核更新，但磁盘已被改过 | **高**（要逐段判） | 必须逐项确认 |
| `advise` | 不规范但内核不替你改（函数缺 `Returns:`、无尾钩） | 低 | 只给建议 |
| `locked` | 身份 / 对话档案 / 日志 / 记忆 / 当前任务 | —— | **只报告，不动作** |

`advise` 直接复用 `_audit_module()` 的 6 项判据（`builtins.py:870`），
不新写一份 —— 又是「单一真源」那条。

`locked` 是刻意单列的：`-u` 跑出来一份「这 5 个文件我不碰」的清单，比悄悄跳过
诚实得多。被跳过而没人说的东西，最后都会变成事故。

报告落盘到 `.openthree/upgrade/report-<ts>.json` + 一份 `.md`（给人看）。

### 4.5 升级官：模型 + 提示词规程（复用现有 ReAct）

**不新写引擎。** `-u` 就是 `Runtime.run()` 的一次特化调用：

```
任务正文 = 内核生成（drift 报告摘要 + 升级规程）
工具集   = 全量（不裁剪 —— 安全边界放在「人确认」，不放在「禁工具」）
会话     = 交互（-u 隐含 -i）
```

提示词规程用一个**指导型 function** 承载，而不是塞进常驻 system：
新增 `.openthree/functions/upgrade_guide.py`，只返回纯文本 —— 升级规程：
分档判据、什么能自动改、什么必须问人、提案文件写什么格式、怎么回滚。
平时零成本（不进 system），`-u` 时才被读进上下文。

（`docs/meta-function-proposal.md` 已经论证过这条通道：工具返回 `str` 就是
模型必读的文本，「指导型 function」不需要任何内核改动。）

模型在 `-u` 会话里的典型动线：

```
drift 报告 → write_plan（把清单变成带勾选框的议程）
          → 逐项：为什么要升 / 升了会怎样 / 需要人补什么 → note
          → 增补型动作：直接 write_function / write_system_prompt（低风险）
          → 覆写型动作：写 upgrade/proposal-<ts>.md，等 :apply
          → 全部处理完 → 更新 workspace.json → finish
```

### 4.6 人确认闸门：两档动作 + 提案文件

**闸门不在工具层，在文件层。** 模型想改 `system-prompts.md` 的既有段落时，
它写的不是那个文件，而是：

```
.openthree/upgrade/proposal-<ts>.md
```

格式刻意做成「人能直接读、也能直接 reject」：

```markdown
## 1. prompts/system-prompts.md —— 吸收「命令行尾钩」整段   [replace 候选]
- 现状：本节缺失。磁盘上其余段落与种子不同（已被改过 12 处）
- 建议：在第 4 节后追加以下 12 行（不替换任何既有内容）
- 影响：提示词 +412 字符；旧稿已存 prompts/history/
- 确认方式：`y` 接受 / `n` 跳过 / 或直接告诉我要怎么改

```diff
+ - **每个文件末尾都加命令行尾钩** …
```
```

人要做的只有三件：**看、说 y/n、或者补充一句「第 3 项改成…」**。
没有新协议 —— 人说的话就是一条 user 消息，走的是既有通道。

内核级命令只给三个（这三件是内核职责，不该由模型自己决定）：

| 命令 | 做什么 |
|---|---|
| `:upgrade` | 重跑扫描（工作区随时可再扫一次） |
| `:apply` | 把提案里已确认的项落盘（打快照 → 逐文件原子写 → 记 manifest） |
| `:undo` | 按最近一份 manifest 整体还原 |

`--yes` 只对 `create` 与 `advise` 生效；`replace` / `merge` 永远要人点头。

### 4.7 备份与回滚

`snapshot_system_prompt()` 只保提示词，不够 —— 升级会碰 `functions/` 与 `*.json`。
所以 `:apply` 之前打一份工作区快照：

```
.openthree/upgrade/<ts>/
  manifest.json          # {at, from_version, to_version, files:[{path, sha256_before, sha256_after, action}]}
  prompts/system-prompts.md
  functions/x.py
  context.json …
```

`manifest.json` 兼两个用途：`:undo` 的依据，和 `workspace.json.last_proposal` 的指针。
逐文件**原子写**（写 temp → `os.replace`），中途失败时 manifest 里已成功的项
保持 `applied: true`，下次 `:apply` 从中断处续 —— 不做「全有或全无」，
因为那会在失败时把可用的中间态一起丢掉。

### 4.8 明确不做（边界）

| 不做 | 理由 |
|---|---|
| 不自动改 `config.yaml` | 里面有 api_key。升级器碰凭据是事故 |
| 不删用户 function | 只 `advise`「不符合规范」，删不删由人定 |
| 不动 `logs/` `memory/` `transcript.jsonl` | append-only，历史不重写 |
| 不改 `identity.json` 的 `name` | 那是它的自我认知，只有它自己能改 |
| 不联网下载新内核 | 升级对象是**工作区**，不是包。包升级是 `pip install -U openthree` |
| 不自动降级 | 工作区版本 > 本机版本时，只报「工作区比内核新，本工具不做降级」 |

最后两条要写在 `-u --check` 的输出里，因为它们是用户最容易误解的地方：
`-u` 不会让你的 openthree 变新，它只会让**这个项目的 `.openthree/` 追平你本机
已经装着的那个 openthree**。

---

## 五、交互长什么样

一个文件只出一条，动作取**最保守**的那个。否则同一份 `system-prompts.md` 会既
被报成 `replace`、又被报成 `merge`，人看到两张自相矛盾的条目，就再也不会相信
整份报告。

```
$ openthree -u

openthree 0.1.7 · 升级模式
工作区   D:\proj\.openthree
基线     kernel 0.1.4 → 0.1.7（落后 3 个小版本，schema 1 → 1）
扫描     12 项：缺 2（可直接补）/ 旧 1（需你判断）/ 不合规 5（只给建议）/ 不碰 4

  1 项需要你判断，2 项可直接补，5 项只给建议，4 项不碰。
  [中] prompts/system-prompts.md —— 你改过 12 处，与 0.1.7 种子有 2 段差异
       （缺「命令行尾钩」整段 / 多「写给长存的读者」整节 → 多的保留）
  [高] functions/safe_edit.py —— 合规体检 1 项：`insert_lines_after` 缺 Returns 段

进入升级会话。输入 :apply 落盘已确认项，:undo 回滚，:q 退出。

你 ▸ 第 1 项接受，第 2 项我自己改
  （模型据此写 proposal-20261027-1130.md，只把第 1 项标为 confirmed）
  ✓ 已写提案 .openthree/upgrade/proposal-20261027-1130.md（1 项待落盘）

你 ▸ :apply
  ✓ 快照 upgrade/20261027-1131/（1 文件）
  ✓ prompts/system-prompts.md  +12 行（旧稿已存 prompts/history/）
  ✓ workspace.json  kernel_version 0.1.4 → 0.1.7，upgrades 0 → 1
```

---

## 六、落地路线

| 阶段 | 内容 | 规模 | 可独立交付 |
|---|---|---|---|
| **0 地基** | `workspace.json` + `required_entries()` 唯一真源 + `init()` 改用它；修 `analysis_sections()` 代码块假阳性 | ~150 行 + 测试 | ✓ 行为不变，纯重构 + 新增状态 |
| **1 扫描** | `scan_upgrade()` 纯函数 + `-u --check`（不调模型） | ~200 行 + 测试 | ✓ 立刻可用于 CI |
| **2 会话** | `-u` 进交互 + `upgrade_guide.py` 指导型 function + 提案文件格式 | ~150 行 | ✓ 能出方案，还不能落盘 |
| **3 落盘** | `:apply` / `:undo` + 快照 + manifest + 原子写 | ~200 行 + 测试 | ✓ 完整闭环 |
| **4 可选** | `--yes`、`--from <version>`、`-u` 结束顺手重建 report | ~80 行 | |

阶段 0 的价值被低估了：即使 `-u` 一行都不写，有了版本戳与唯一真源，
以后任何「结构变了怎么迁移」的问题都从「猜」变成「读一个数」。

---

## 七、验收（测试清单）

`tests/test_upgrade.py`：

- **判据**：多出来的段落**不**进 `missing`；缺的段落在；代码块里的 `#` 不误报
- **版本**：`kernel_version` 相等 → `outdated` 为空；落后 → 一条；
  工作区比内核新 → 只报「不做降级」，不产生任何动作
- **种子判据**：hash 相等 → `replace`；hash 不等 → `merge`；老工作区无
  `workspace.json` → 一律 `merge`（绝无 `replace`）
- **免疫**：`plan.md` / `current-task.json` / `identity.json` / `user-prompts.md`
  在任何情况下都不出现 `replace` / `merge` 动作
- **边界**：`transcript.jsonl` / `logs/` / `memory/` 只以 `locked` 出现
- **CLI**：`-u` + 任务参数 → 退出 2；`-u` 非 tty 且无 `--check` → 拒绝；`-u --check`
  有漂移 → 退出 1，无漂移 → 退出 0
- **落盘**：`:apply` 前后 sha256 与 manifest 一致；`:apply` 中途失败 → 已成功项
  保持、可续；`:undo` 逐字节还原（含 CRLF）
- **REPL**：沿用 `tests/test_default_interactive.py` 的 `monkeypatch input` 手法

一条元验收：**`-u --check` 跑在 openthree 仓库自身上，必须报出「提示词缺
『命令行尾钩』段、多『写给长存的读者』节，且后者不产生任何动作」。** 这一条同时
守住了真阳性与假阳性两侧。

---

## 八、风险与取舍

| 风险 | 取舍 |
|---|---|
| 模型在升级会话里「热心过头」，改了一大堆 | 覆写型必须落提案文件、必须人 `:apply`；`--yes` 不覆盖高风险动作 |
| 老工作区没有 hash，全靠 `merge`，提案可能很长 | 这是**正确**的保守方向；配合 `write_plan` 分批、`:apply` 逐批落盘 |
| 版本戳一旦写错，后续判断全错 | `workspace.json` 内容由内核写、只读给模型看；坏了按 legacy 处理（退回 `merge`），不 crash |
| 与 `--init --force-prompts` 语义重叠 | 明确分工：`--init` 是「我要重来」，`-u` 是「我要跟上」 |
| 又多一个文件（`workspace.json` + `upgrade/`） | 换取「可回滚」与「CI 可判」；`upgrade/` 只增，可随时整体删（`README.md` 里注明） |

---

## 九、附录：本文两处实测发现

1. **`Workspace.analysis_sections()` 有假阳性**：它把代码块里的 `# 注释` 当章节
   标题（`workspace.py:759`，判据只有 `stripped.startswith("#")`）。对 openthree
   自身跑一次，就会凭空多出一项「缺失 `# 命令行入口（命名成 dunder…）`」。
   在做漂移判据之前必须先跳过 ` ``` ` 围栏。

2. **本仓库的提示词就是一份真实漂移样本**：磁盘 5823 字符 / 种子 5548 字符，
   一边缺「命令行尾钩」整段、一边多「写给长存的读者」整节。它是 `-u` 的第一个
   真实测试用例 —— 也是本文 §2.2 那条判据的来源。
