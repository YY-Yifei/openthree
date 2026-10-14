# 内核设计整体分析（2026-10-13）

> 分析对象：`openthree` v0.1.0，HEAD = `4fe7574`（上下文自管）
> 方法：通读 14 个模块 2,368+ 行源码 + 实跑 207 tests + 对最近真实会话日志做量化取证
> 立场：以「设计哲学 = 模型驱动（道·一二三）」为标尺，看偏离在哪

---

## 0. 一句话结论

内核的骨架是**干净且自洽**的，五个「模型驱动面」里已有**四个**真正打通，
第五个（上下文）本轮补齐。**残余偏离不在「上下文」这条线**，而在
**「模型的运行边界」——迭代上限、生成参数、工具结果上限、超时，
仍全部由人类 config/CLI 掌握，模型读得到却改不了。**

---

## 1. 内核的构成：一 · 二 · 三 的映射

| 哲学层 | 代码落点 | 是否「模型可写」 |
|---|---|---|
| **道**（智能本身） | 模型 | — |
| **一**（世界观） | `prompts/system-prompts.md` + `Workspace` | ✅ `write_system_prompt`（带历史/回滚） |
| **二**（能力） | `.openthree/functions/*.py` + `registry.py` + `tool.py` | ✅ `write_function` + `reload_functions` |
| **三**（循环） | `agent.py` 的 `run()` ReAct | —（控制流不预设） |
| **万物**（自造） | 模型写出的 function / prompt / 工具 | ✅ 实证：`edit_file.py`（3 工具，模型自造） |

核心把手：`kernel.py` 的 `KERNEL`（30 行）—— 给自造 function 递上
`workspace / registry / recorder / agent / config / state`。
磁盘即状态，每轮重读，改一下下一轮生效。

---

## 2. 「模型驱动」的五个面：现状盘点

| 面 | 载体 | 读 | 写 | 状态 |
|---|---|---|---|---|
| 提示词 | `system-prompts.md` | ✅ `read_prompts` | ✅ `write_system_prompt`（自动存档+回滚） | **通** |
| 能力 | `functions/*.py` | ✅ `list_functions` | ✅ `write_function`+`reload`+`verify` | **通** |
| 计划 | `prompts/plan.md` | ✅ `read_plan`（每轮注入） | ✅ `write_plan` | **通** |
| 记忆 | `memory/memory.jsonl` | ✅ 每轮注入 + `recall_memory` | ✅ `remember` | **通** |
| **上下文** | `.openthree/context.json` | ✅ `context_usage` + 处境一行用量 | ✅ `context_budget` + `compact_context` | **本轮补齐** |

### 2.1 上下文自管（4fe7574）的设计要点
- **策略文件** `context.json`（预算 / 自动折叠开关），`tick()` 每轮重读。
- **看得见**：处境每轮注入一行
  `已用 X/Y 字符（n%）；可折叠 step 范围 a-b；自动折叠 开/关；已折 k 次`。
- **折得掉**：`compact_context(start,end,summary)` —— 摘要留空=**直接丢弃**（最省），
  **立即生效**，如实返回「省了多少字符」（修掉旧 REVIEW P1-3.8「报成功却什么都没发生」）。
- **兜底降级为安全网**：内核 `_auto_compact` 仍默认开，但**模型可关**
  （`context_budget(auto_fold=false)` 或预算调大）。
- **硬闸在环境**：`OPENTHREE_CONTEXT_AUTOFOLD=0`（CLI `--no-auto-fold`）——
  模型改不了环境，这是它唯一绕不过的一层。
- **末步保护**：`_foldable_steps` 排除最后一个 step（否则其后的 tool 消息成孤儿、序列非法）。
- **合并折叠**：模型折叠与内核兜底合并为**一次编辑**，不再各折一次、step 编号互相踩。

---

## 3. 本次实跑取证（事实，不是印象）

最近一场真实会话 `session-20261013-092425`：**135 步 / 374 events / 134 次 LLM 调用**。

| 观测 | 数据 | 判断 |
|---|---|---|
| 工具输出最大长度 | **4,020 字符**（175 条中） | 旧 REVIEW P0-3.2（20,353 字符）**已根治** |
| 带「已截断/spill」标记的 tool 结果 | 5 条 | `_spill` 落盘机制**在工作** |
| prompt token 增长 | 12,733 → 80,489 → 112,496 | **每轮重发全量 history，仍线性累积** |
| 测试 | **207 passed** | 全绿 |
| 自造工具 | `edit_file.py`（edit_file / replace_all / read_span） | 演化闭环**有真实产物** |

结论：**成本卫生（单条结果）已好，累计成本（重发历史）仍是活问题。**

---

## 4. 残余偏离：模型运行边界的「不对称」

「上下文」补上后，**模型的运行边界**成为唯一没交给模型的一族旋钮：

| 边界 | 现状 | 模型能读? | 模型能改? |
|---|---|---|---|
| 迭代上限 `max_iterations=200` | 构造函数常量 | ✅ 处境回显 | ❌ |
| 生成参数 `max_tokens=32768` / `temperature=0.3` | `config.yaml` | ✅ 处境回显 | ❌ |
| 工具结果上限 `tool_result_limit=4000` | 构造函数常量 | ⚠️ 间接 | ❌ |
| 命令超时 `timeout` | 工具参数 | ✅ | ✅（逐次） |

**这是「模型驱动」最欠缺对称性的地方**：
- `max_iterations` 与 `context_budget` 本质同构（都是「还能走多远」的预算），
  但一个已模型可写，一个还是人类的固定常量。长任务被 200 步硬截断时，
  **模型只能眼睁睁看着自己被叫停**——它读到了「第 201/200 步」，却无权延长。
- `max_tokens` 更刺眼：截断发生时，内核的恢复话术是
  **「请调大 config.yaml 的 llm.max_tokens 后重试」**——把一件模型本能自己做的事，
  推给了一个它够不着的文件。

---

## 5. 一处口径隐患（不是功能 bug，是「读数不诚实」）

`history_stats()` / 处境读数的「已用字符」= `_estimate_chars(self.history)`，
**只算 history，不算 system**。而 system 每轮注入：工具 digest（29 个工具）+ 记忆块
（上限 2000）+ 计划块（**无上限**）+ 演化史（10 条）+ 处境块。

后果：
1. **螺旋风险**：真实占用 > 读数。若某一轮 system 因计划/记忆膨胀而逼近上限，
   读数仍可能显示「远未超预算」→ 兜底不触发 → 下一轮 system 更大。
2. **口径不一致**：记忆有 `MEMORY_INJECT_LIMIT_CHARS=2000` 护栏，
   **计划块 `_plan_block` 完全没有护栏**——计划越长，每轮注入越贵，且不计入读数。

---

## 6. 优先级建议（按「是否服务于模型驱动」排序）

1. **【哲学一致】让运行边界也可模型自管** ✅（2026-10-13 已落地）
   把 `max_iterations / max_tokens / tool_result_limit / temperature` 收进
   `.openthree/limits.json`，配 `limits_usage()`（读）+ `set_limits()`（写），
   与 `context.json` 同源同构；内核默认只是起点，显式启动参数与
   `OPENTHREE_LIMITS_LOCK` 是模型绕不过的两层。
   —— 「还能走多远」这族旋钮和「上下文」一样，成了模型的一只手。
2. **【读数诚实】把 system 计入 `history_stats`**
   估算 `_estimate_chars(history) + len(build system)`，让处境读数与真实占用对齐。
3. **【护栏】`_plan_block` 加字符上限**（如 4000），与记忆块同构。
4. **【可选】小模型摘要折叠**：目前折叠靠模型自愿 / 内核机械。真实会话里
   模型**并未主动压缩**（成本仍线性涨）。可考虑：超预算时先让模型「总结当前进展」
   再丢弃原文——把「压缩」也变成一次模型决策，而非内核的机械切除。

---

## 7. 总评

openthree 的**哲学与实现一致性**依然罕见地高：五个驱动面里四个早已打通、
第五个本轮补齐，且不改「极简 / 无硬编码智能 / 道一二三」三条底线。
它现在的短板不再是「写错了」，而是**「边界还没交出去」**——
模型能改自己的记忆、能造自己的工具、能管自己的上下文，
却**不能决定自己走多少步、生成多长、单条结果留多宽**。

把这一族「运行边界」也做成一读一写的磁盘策略，openthree 的模型驱动闭环就**完整**了。
