# 方案：让 function 承载「指导」—— meta / 指导型工具

> 状态（2026-10-23）：本文是**分析归档**。实际落地的只有其中最小的一条 ——
> 三处文字（种子提示词、`write_function` docstring、模型自己的工作区提示词），
> 机制一行未改；文中其余备选方案（`GUIDE` 标记、`read_tool_doc`、schema 扩展）
> **均未采纳**，保留在此仅作当时的推理记录。
> 一句话：工具返回的是纯文本，所以 function 天生不只输出「动作结果」，还能输出
> **说明书、检查清单、写法约定、few-shot 模板**。做到「一个 .py 就完整描述一个能力」,
> 缺的不是机制，而是**意识（提示词没教）+ 发现（模型看不见文件级文档）+ 边界（正文不许进常驻）**。

---

## 一、结论先行

| 层 | 现状 | 判定 |
|---|---|---|
| 返回通道 | 工具返回 `str` → 进 history 的 tool 消息 → 模型必读 | **已通，零改动** |
| 文件级描述 | 模块 docstring 从不进入任何模型可见面 | **断了** |
| 常驻成本 | 工具 schema 实测 18897 字符、description 合计 8270 字符 | **正文绝无可能进 schema** |
| 意识 | 提示词与 `write_function` 的 docstring 只把 function 当「动作」 | **缺口在本项** |

建议三个改动，按性价比排序：

- **A（零风险，只改文字）**：提示词补一节「function 也是文本 —— 指导型工具」，
  并给出「这段文字是否每步都必须看见」的判据。
- **B（小改 + 测试）**：注册器把**模块级 docstring 与 `GUIDE` 标记**带进 `Tool`，
  工具清单（digest / `list_functions`）里只多一个 `⟐` 标记 —— 正文仍**按需拉取**。
- **C（新增一个内置）**：`read_tool_doc(name)` 按需返回某个 function 文件的完整说明书
  （模块 docstring + 各函数完整 docstring），让「A 里写的约定」真的有地方读。
- 顺手修：`tool.build_tool` 把参数说明在 description 里**写了两遍**（见 §2.4）。

---

## 二、现状（每条都带证据）

### 2.1 通道已通：返回文本 = 模型拿到指导

- `openthree/agent.py:907-929` `_execute()`：`return str(out), None` —— 工具返回什么字符串，
  模型就收到什么字符串。
- `openthree/agent.py:780-786`：结果原样进一条 `role: "tool"` 的 history 消息。
- 因此今天就能写：

  ```python
  def guide_release() -> str:
      """发布 openthree 到 PyPI 之前必须做的检查清单。随时可调，无副作用。"""
      return "1. 版本号两处同步 …\n2. build …\n3. twine check …"
  ```

  **不需要任何内核改动。** 这一点是整份方案的地基：所谓「meta function」不是新机制，
  而是对既有返回通道的**用法升级**。

- 天然的分页器：`agent.py:891-905` `_spill()` 按 `tool_result_limit`（默认 4000 字符）
  截断，超长部分落盘并在结果里给出路径。于是「指导很长」不是问题 —— 第一屏给纲要，
  要细节就 `read_file` 续读。**约束不在长度，而在常驻。**

### 2.2 注册单元是函数，不是文件 —— 文件级描述在注册层被丢弃

- `openthree/registry.py:110-136` `_load_file()`：遍历 `dir(module)`，跳过 `_` 开头、
  跳过类、只收 `__module__ == mod_name` 的可调用对象。
- `openthree/tool.py:146` `build_tool()`：描述只取 `inspect.getdoc(func)`。
- 全仓 grep `__doc__|getdoc`，模型可见通道里只有这两处（另一处是 `verify_tool` 子进程脚本
  里的 `_first_doc`，它只用来给**模型看的验收报告**取首行，也不是文档通道）。

**后果**：`.openthree/functions/safe_edit.py` 开头那 529 字符（动机、两条核心保证、用法示例）
写得极好 —— 但它只对**人**有效。模型读不到它，除非用 `read_file` 把整份源码读进上下文
（贵，而且模型多半不需要那 100 行实现）。

这正是用户要的那句话的缺口：**「一个 python 文件完整描述了一个能力」在文件层成立，在注册层丢失。**

### 2.3 常驻成本已很高（实测数字，非估算）

在本仓库现状（33 内置 + 2 自造）下实测：

| 指标 | 实测值 |
|---|---|
| 全部工具 schema 序列化后 | **18897 字符** |
| description 合计（仅 33 内置） | **8270 字符** |
| description 均值 / 最大 | 251 / 780（`search_files`） |
| `STATIC_TEMPLATE` 里的 digest | **2726 字符** |

结论很硬：**正文不许进 schema / description**。digest 现在每行只是
`- name(sig) — docstring 首行`，已经是「只够认出它是什么」的颗粒度。所以指导型工具的
正文只能**按需拉取**，这不是口味问题，是 18897 这个数字决定的。

同时这也给出这条设计的最大价值：把一段「每步都要看见」的知识改成「需要时才拉」，
就等于给上下文腾出等量空间 —— 它是自管上下文的**第三条路**（前两条是折叠、调预算），
而且是无损的（知识没被丢弃，只是移到了磁盘上）。

### 2.4 现成的浪费：参数说明在 description 里写了两遍

- `openthree/tool.py:174-177`：

  ```python
  extra = [f"- {k}: {v}" for k, v in arg_docs.items() if k in parameters]
  if extra:
      long_doc = long_doc.rstrip() + "\n\n参数：\n" + "\n".join(extra)
  ```

  而 `long_doc` 本身就是 `inspect.getdoc(func)`，里面**已经含 `Args:` 段**（`arg_docs` 正是从它解析出来的）。

- 实测 `search_files`：description 780 字符里，「要找的文本…」这条参数说明出现 **2 次**，
  `Args:` 与「参数：」两段并存。

修法（低风险）：**若 docstring 已含可解析的 `Args:` 段，就不再追加**；只在 docstring 没写
`Args:` 而参数确实有说明来源时才追加。保守估计能省掉 description 总量的 15~25%。

### 2.5 意识缺口：提示词只把 function 当「动作」

- `openthree/workspace.py:144-157`「造 function 的约定」六条：公开函数才会注册、类型注解→Schema、
  docstring 首行→描述、`kernel` 首参注入、返回 `str`、出错返回字符串、写完先验收、模块级变量是暂存。
  —— **全是「怎么让一个动作变成工具」，没有一句「返回值可以是一份指导」。**
- `write_function` 自己的 docstring（`builtins.py:604-627`）同上。
- 种子提示词（`workspace.py:50+`）把 functions 称作「能力」，没有「知识 / 说明书」这一维度。
- 结果是：现有「知识」全部堆在「一」上 —— 12 节 system prompt、计划、记忆，**全部常驻**。
  模型自然会觉得「想留下经验，就写进提示词或记忆」，而不是「固化成一本可按需翻阅的手册」。

---

## 三、要建立的核心观念（拟写进提示词的原文）

### 3.1 `function` 的返回值是**通道**，不只是结果

返回值可以装三类东西，地位平等：

1. **动作结果** —— 现在的主流用法（读文件、跑命令、查数据）。
2. **指导文本** —— 说明书 / 检查清单 / 写法约定 / 模板 / few-shot。
   没有副作用，随时可调，调了就进上下文。
3. **结构化数据** —— JSON、表格、清单，供后续步骤消费。

### 3.2 「指导型 function」= 把常驻知识改成按需拉取

判据一句话：**这段文字是不是每一步都必须看见？**

- 是 → 写进「一」（system prompt）。它是身份与律法。
- 否 → 做成 function。system 里只留一行「有这么一本手册」，正文等真的要用时再 call。
- 半是半否 → 提示词里留一句「怎么用它」的索引，细节全在 function 里。

### 3.3 一个 `.py` = 一个能力的完整描述

一个文件里三处文字各司其职（登记顺序就是读者的阅读顺序）：

| 位置 | 写什么 | 谁读 |
|---|---|---|
| **模块 docstring** | 为什么有这个能力、什么时候用、有什么坑、整体用法 | 人和（未来）`read_tool_doc` |
| **公开函数 docstring** | 这一支怎么调：Args / Returns / 示例 | 模型的工具 schema（首行）+ 按需全读 |
| **`_` 私有函数** | 内部实现，不注册 | 人和维护者 |

于是「造一个能力」= 造一个文件，文件本身就是它的说明书 —— 不需要再在提示词里补一段解释。

### 3.4 建议直接写进「造 function 的约定」的新条目

```markdown
- **function 也可以返回「指导」，不只是「结果」。** 返回值是纯文本通道：
  说明书、检查清单、写法约定、few-shot 模板，都可以直接 `return` 出去。
  这类工具叫**指导型工具**：不写文件、无副作用、随时可调、调了就进你的上下文。
- **判据：这段文字是不是每一步都必须看见？** 是 → 写进提示词；
  否 → 做成指导型 function，让「一」里只留一句索引。
  这是你自管上下文的第三条路（前两条是折叠与调预算），而且无损：知识没丢，只是搬到了磁盘上。
- **一个 .py 就是一份完整说明书。** 模块 docstring 讲「为什么 / 何时用 / 有什么坑」，
  函数 docstring 讲「怎么调」，`_` 私有函数讲实现。
- **指导型工具的文件请写 `GUIDE = True`。** 它会让工具清单里出现 `⟐` 标记，
  你一眼就能看出「哪个是可翻阅的手册」；正文仍然只在调用时才进上下文。
```

---

## 四、落地方案（三档，可分批）

### 4.1 A 档：只改文字（零风险，建议先做）

改动点：

1. `openthree/workspace.py` `_SYSTEM_PROMPT_SEED` 的「造 function 的约定」：追加 §3.4 的条目。
2. `openthree/builtins.py` `write_function` 的 docstring：同一条意识的简写版
   （它会在工具 schema 的 description 里，模型造工具前一定看得见）。
3. `README.md`「自查」一节 + `docs/` 里留档。

**必须注意的兼容性事实**：`generate()`（`workspace.py:486-489`）只在
`system_prompt_file` **不存在**时才落种子。所以**存量工作区不会自动升级**：
已有的 `.openthree/prompts/system-prompts.md` 保持原样，而那正是模型自己维护的「一」。

两种处理，二选一：

- **保守**：什么都不做。文档里说明「存量工作区需要模型自己 `write_system_prompt` 补上」，
  并在 release notes 里给一段可复制的补丁文本。
- **主动**：给 `generate()` 加一个「种子版本号」机制（`.openthree/prompts/.seed`，
  记录写入时的种子版本），仅当提示词**逐字节等于旧种子**时才升级。模型改过的提示词一律不动。

  这第二种做法会引入一个新概念（种子版本），但换来「所有人都吃到这次意识升级」，
  且**不会覆盖模型的自定义**。建议先 A 保守，观察一两个版本再决定。

### 4.2 B 档：注册器带上文件级元信息（小改 + 测试）

目标：让「文件级文档」在**模型可见面**里存在一个 1 行索引，正文仍然按需拉取。

- `openthree/tool.py` `Tool` 增加两个字段：

  ```python
  module_doc: str = ""          # 模块 docstring 首行（只放首行，正文按需拉取）
  guide: bool = False           # 是否为指导型工具
  ```

- `build_tool(func, *, origin, source_file, module_doc="", guide=False)` —— 都是关键字参数，
  默认值让现有调用点（`builtins.py` 注册、`tests`）**零改动**。
- `openthree/registry.py` `_load_file()`：`exec_module` 之后模块已 import，
  直接读 `getattr(module, "__doc__", "")` 与 `getattr(module, "GUIDE", False)`，
  再把同一份值传给本文件里每个 `build_tool`。**不需要再 parse 一遍源码。**
- `Registry.digest()`（`registry.py:157-162`）：行尾加标记，例如

  ```
  - `guide_release()` — 发布前的检查清单 ⟐
  ```

  并在 digest 的表头加一句解释。注意 digest 位于 `STATIC_TEMPLATE`（缓存前缀），
  加标记会让 system 变一次 —— 但只在 `reload_functions` 之后变，与「改一下下一步生效」
  的设计一致，**不破坏缓存不变量**（`tests/test_prompt_prefix_cache.py` 保的是逐字节稳定
  与「此刻段在末尾」，不是「永远不变」）。
- `list_functions()`（`builtins.py:793-814`）同步显示 `⟐`，并给指导型工具单独的分组说明。

为什么用模块级 `GUIDE = True` 而不是靠 docstring 猜：

- 显式、可静态识别（`_static_audit` 用 `tree.body` 就能认出，`ast` 不 import，零风险）；
- 不会把「随手写的模块注释」误判成指导手册；
- 一个 `True` 常量不会被注册成工具（`registry` 只收 callable），不会污染清单。

### 4.3 C 档：新增内置 `read_tool_doc(name)`（让文档真的有地方读）

B 只给了索引，正文得有人去拉。今天模型唯一的拉法是 `read_file` 整份源码 —— 贵且浪费。
所以补一个内置：

```python
def read_tool_doc(name: str = "") -> str:
    """按需读一本「手册」：某个 function 文件的模块说明 + 各函数完整 docstring。

    正文不进工具 schema（那是常驻成本），只在这里按需拉取。
    不传 name 时列出所有带 `GUIDE = True` 的手册。

    Args:
        name: function 文件名或工具名；空 = 列出手册清单
    """
```

返回内容建议：

- 模块 docstring **全文**（这是「为什么 / 何时用 / 有什么坑」，正是模型最缺的）；
- 每个公开函数的 `signature_hint()` + 完整 docstring（含 Examples）；
- 文件路径与行数（要继续深读就用 `read_file`）；
- 可选参数 `with_source=False`：为 True 时再附源码 —— **默认不带**，别把代码当文档喂。

实现要点：`ast.parse(path.read_text())` + `ast.get_docstring`，**不 import** 目标文件
（与 `_static_audit` 同一条路径，安全、不会触发副作用），这也顺带保证它对
「import 会炸的文件」仍然可读。

### 4.4 顺手修（建议与 B 同批）

- `tool.py:174-177` 参数说明重复：docstring 已含可解析 `Args:` 段时不再追加。

### 4.5 测试清单（若落地）

| 断言 | 位置建议 |
|---|---|
| 模块 docstring 首行出现在 digest，且带 `⟐` | `tests/test_verify.py` 或新增 `test_guide_tools.py` |
| `GUIDE=True` 的文件，其公开函数仍正常注册（不多不少） | 同上 |
| `read_tool_doc()` 返回模块 docstring 全文 + 每函数签名；不传 name 时列清单 | 同上 |
| `read_tool_doc(with_source=False)` **不含**函数体代码 | 同上 |
| description 不再重复参数说明（`search_files` 的长度显著下降） | `tests/test_builtins.py` |
| 「改一下下一步就生效」不退化：`write_function` + `reload` 后新 `⟐` 出现在下一步 system | `tests/test_layered_prompt_evolution.py` 的 E4 扩展 |
| schema 总字符数不因为本改动增长超过 ~每工具 1 行 | 可做一个宽松上限断言（防回归） |

---

## 五、边界与不变量（这四条别越）

1. **指导正文绝不进 schema / description。** 18897 是现在的常驻成本；一旦允许正文常驻，
   这条设计立刻退化成「用工具装提示词」，白白多花 token 还没换来任何东西。
2. **指导型工具必须无副作用。** 只读、不写文件、不改状态 —— 这样它才能被随手调用、
   被反复调用、被并行调用，且不会因为「调一下看看」而污染环境。
3. **reload 语义不变。** 模块级变量是暂存（reload 会重载模块），所以指导内容必须写在
   docstring 或字面量里，**不许靠运行期生成**。`KERNEL.state` 仍只作草稿纸。
4. **「每步都要看见的」不许下沉成 function。** 身份、律法、当前任务、计划 —— 这些留在
   「一」与自维护段。指导型工具是**参考书**，不是**宪法**。

### 与「记忆」的边界（值得在提示词里点明）

- `remember` 记的是**我这个智能体的经历**：「上次在这里踩了 GBK 的坑」。
- 指导型 function 记的是**这件事该怎么做**：「Windows 下改 CRLF 文件的正确姿势」。

一条经验稳定、可复用、和「我是谁」无关时，就该从记忆**升级成 function** —— 从
「我记得」变成「我有这本手册」。这条升级路径建议也写进提示词，它是自我演化的第二次跃迁
（第一次是把动作变成工具）。

---

## 六、明确不做的事

- ❌ **不给工具 schema 加 `prompt` / `notes` 字段。** 那是把正文塞进常驻，与 §2.3 的数字直接冲突。
- ❌ **不做「指导型工具的自动注入」。** 自动注入 = 变回常驻，等于什么都没做。
- ❌ **不让内核自动把记忆提升为工具。** 该不该固化成一个能力，是模型的判断；内核只提供
  `read_tool_doc` / `⟐` 标记这类**看得见的手段**，不替它决定。
- ❌ **不做「纯手册文件」放行**（一个 public 函数都没有、只有 docstring 的文件）。
  当前 `registry.py:139-142` 会把这种文件记成「没有可注册的公开函数」。
  先不动 —— 手册至少留一个可调用的入口函数（`guide_xxx()`），调用才有「按需」这件事；
  没有入口的手册，模型永远不会想起来去读。

---

## 七、建议实施顺序

1. **A 档（文字）** —— 今天就能做：提示词 + `write_function` docstring + README。零风险。
2. **B 档（注册器 + digest 标记）** —— 半天，含测试。让「手册」在模型眼里**存在**。
3. **C 档（`read_tool_doc`）** —— 半天，含测试。让手册**可读**。
4. **4.4 顺手修** —— 跟 B 同批，省 token 且无兼容风险。

A 做完，行为上就能跑通（模型会开始写指导型 function）；B、C 做完，这套用法才**可发现、
可维护**，而不是「碰巧有人这么写过一次」。
