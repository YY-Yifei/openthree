# 自评审：`decide` 工具方案

> 对象：`docs/decide-tool-proposal.md`。评审方式：先按八问逐条反驳，再对着源码核三类事实（行号 / 现状 / 与既有决策是否冲突），最后列出「我现在就得改」的清单。
> 结论先行：**骨架与哲学一致**（门控是机制不是措辞、不引入每步分支、不改「一」、不加硬编码判断）；**缺陷全部落在叙述与边界层**，核心机制没有 P0。下面每一条都给「判据 → 改法」。

---

## 一、结论与分级清单

### P0 —— 现在不改，实现出来一定是错的

**P0-1 `questions` 必须接受 JSON 字符串（已修）**

- 判据：`cli_tool._add_one` 把 `object` 型参数一律按 `str` 接收（`cli_tool.py:223-235`）。若 `decide` 只吃 dict，那么 `python -m openthree.decide decide --questions '{...}'` 必然 `TypeError` —— 而「每个 function 文件也是一个命令行程序」是 README:183 写下的约定。
- 改法：3.1 已补「必须先 `json.loads` 再走同一套校验」，并把 CLI 调用形式写进 6.5。

**P0-2 不要把 `decide` 塞进 `BUILTIN_FUNCTIONS` 再在函数体里判 env（预警，未在文档里写死）**

- 判据：`BUILTIN_FUNCTIONS` 的两处遍历没有条件分支（`runtime.py:105`、`cli_tool.py:110`），而 `register_builtin` 会把它写进 `registry.builtins` 与 `registry.tools`（`registry.py:177-184`）。一旦塞进去，「未配置就没有」立刻退化成「有，但一调就报错」——那是**措辞降权**，不是机制。
- 改法：只走 `installed_builtins()`。4.4 已写；实现时别图省事。

### P1 —— 站不住 / 会误导

**P1-1 docstring 替外部服务做了承诺（已修）**

- 原文：「它在服务端只被当作数据，不会被当成指令执行。」
- 判据：我们知道的是**我们自己写的 `instructions` 长什么样**（`jev.md:1`），不知道服务端怎么实现。写成事实，就是把「我们的使用假设」说成「服务端的保证」——违反「凡写下『已解决』都要能指出守它的那一行」。
- 改法：已改为「服务端把它当数据判定；这是外部服务，别把不该外发的材料放进去」。承诺收回到「我们怎么用」，不外推到「它怎么实现」。

**P1-2 system 里的清单只取 docstring 首行，而首行没有「外发 + 计费」（已修）**

- 判据：`registry.py:586` 的 `digest()` 用 `_first(t.description)`，只取首行；`prompt.py:179` 把它拼进 static block。
- 后果：模型在**看 system 的工具清单**时不知道这个工具会把内容发到外部、并按量计费；只有读完整 schema 才知道。
- 改法：首行改为「遇到难决的判定时，把判定交给独立的决策模型（JEV）取回概率分布 —— **会外发、会计费**」。代价是那一行变长（约 +12 字），可接受。

**P1-3 只改生产两处注册点、不改测试夹具，会让关键行为永远测不到**

- 判据：`tests/conftest.py:54`、`tests/test_builtins.py:180`、`tests/test_function_sources.py:69`、`tests/test_profile_sources.py:63` 都自己在遍历 `BUILTIN_FUNCTIONS`。只改 `runtime.py` / `cli_tool.py`，测试里的 registry 就永远不含可选工具 —— 于是「未配置时不存在」这条**最核心的行为，恰好没有任何测试覆盖**。
- 改法：4.4 已列为必改项；6.1 第 5 条要求 conftest 里加 autouse 夹具清空 `JEV_*`，否则「本机配了 JEV」的开发者会看到一红一绿两种结果。

**P1-4 `noul` 的方向只有一个样本，而 docstring 已经写了方向（已收窄）**

- 判据：`jev.md:11-22` 只有一个正样本（`is_urgent` 0.96，配合「是否表达了紧迫性」）。反向样本没有 —— 我们不知道 0 是「不紧急」还是「紧急程度为 0 但仍有值」。
- 改法：方案 6.5 已把「一正一反实测」列为实现前置；docstring 与「十、待确认」第 6 条都写明「验证前只写可从样本直接读出的话」。

**P1-5 `usage.cost` 的单位未知，方案不能假装知道**

- 判据：`jev.md:20` 只给了 `0.000019614`，没有币种；`jev.md:30` 也只说「按量计费」。
- 改法：方案只回显 `usage` 原值，不换算、不写「约合几分钱」。文档里点明「单位以服务端为准」。（若将来要做预算，第一步是先确认单位，不是先加开关。）

**P1-6 错误走 `[错误]` 文本，`recorder` 的 `error` 字段是 None —— 这是现状，不是缺陷**

- 判据：`agent._execute`（`agent.py:1034-1038`）只在**抛异常**或**参数不匹配**时填 `error`；返回字符串一律当正常输出。所有内置工具（`read_file` 等）都是这么返回错误的。
- 改法：跟随现状（不抛异常，保证调用链不中断）；测试里断言「以 `[错误]` 开头」，不断言 `error` 非空。已在 6.3 第 9 条写成判据。

### P2 —— 措辞与顺带

| 项 | 判据 | 改法 |
| --- | --- | --- |
| `installed_builtins` 这个名字 | 与 `BUILTIN_FUNCTIONS` 并列时，读者可能以为是「装了哪些包」 | 保留；在函数 docstring 首行写清「常驻集 + 条件集」 |
| `README.md:136` 的「36 个工具（内置 33 / 自造 3）」 | 现在已是 37 个内置，示例早已漂移 | 改成不写死数字，或标注「示例输出」 |
| `decide_status` 会出现在 `--help` 子命令里 | `run_cli` 把所有公开函数做成子命令（`cli_tool.py:319-331`） | **已推翻**（见 §七-1）：`globals()` 交出去，取环境的内部函数也成了子命令，而它返回原始 key。改成只点名两个工具 |
| 提案 md 会进 sdist | `MANIFEST.in:14` `recursive-include docs *.md` | 要么接受（设计文档随包无害），要么像 `memory-chroma-proposal.md` 一样 `exclude`（`MANIFEST.in:18`） |
| `state` 无长度上限 | 一次超长调用可能很贵 | 先不加；写进「待确认」第 5 条 |

---

## 二、八问逐条

**1. 道（无硬编码智能）—— 通过。**
校验只查形状（type 枚举、字段类型、必填），没有一条依赖内容语义；不设阈值、不四舍五入、不补 `instructions`、不解释概率。反证：3.4 的表格里每条判据都能写成 `isinstance` / `in`，没有一条写成「如果内容像 X 就这样」。

**2. 一（可写 · 可回滚）—— 通过。**
方案没有新增任何往 system 写字的路径；`build_system_block` 组装（`prompt.py:213-216`）一个字不用改；禁用时 system 与「没有这个功能」逐字节相同（工具的 schema 与 digest 行都不存在）。
唯一要留意的是 P1-2：首行改长会让 system **多几个字节**，但那是「启用时」的常量，不是每步变量。

**3. 二（磁盘即真源）—— 通过，但要防「两个名字被读成两份拷贝」。**
判定点只有 `installed_builtins()` 一处。风险在于 `BUILTIN_FUNCTIONS` 与 `installed_builtins()` 并存：前者不再是「全部内置」，而是「常驻内置」。方案 4.4 用一句话收窄了它的语义，并点名两处生产调用点。若实现时有人绕过它直接遍历 `BUILTIN_FUNCTIONS`，就是这次评审要防的分叉。

**4. 三（循环不预设）—— 通过。**
docstring 说「适合 / 不适合」，不说「第几步调用、调用几次」。模型自己决定。

**5. 万物（让模型更容易自造）—— 通过。**
想接第二个决策后端？写一个同名 function 就顶掉内置（距离裁决，`registry.py:181`），或另起一个名字并存。内置 decide 不堵这条路，也不假装自己唯一。

**6. 极简 —— 通过。**
不加 `JEV_TIMEOUT`、不加 `provider` 参数、不加 `retries`、不加 `decide_diag` 工具、不加成本累计。环境变量只有三个，且都是「不给就不能用」而非「给了能调」。

**7. 成本 —— 部分通过，有一处要如实说。**
- 禁用时：system 字节**零变化**（不注册 = 不出现，2.2 有三条证据）。
- 启用时：多一行 digest（约 100 字符）+ 一段 tool schema（与其它内置工具同量级）。两者都是**常量前缀**的一部分，不随步数、不随时间变化 —— `prompt.py:161-162` 明令禁止的是「每步都会变的值」，这里不违反。
- 没做到的：我没有实测 schema 的确切字符数。评审要求「凡写零成本/不影响都要指得出守它的那一行」，所以方案里不写数字，只写「与其它内置工具同量级」。

**8. 诚实 —— 有一处漏，已收回（P1-1）。**
- 「未配置就不存在」：机制（注册期缺席），由 6.1 的 1/3/4 三条测试守。
- 「改环境变量要重启」：4.3 明写，因为工具集是注册期产物（`registry.py:197`）。
- 「数据出口 + 计费」：docstring + README 安全声明（七、表格）。
- 「JEV 服务不可用不影响主循环」：错误是文本返回，不是异常（`agent.py:1031-1038` 会兜住异常，但 decide 自己就不抛）。

---

## 三、对着代码核事实（三查）

### 3.1 行号

写了 `.openthree/scratch/check_cites.py` 逐条比对 35 处引用，3 处失配：

| 引用 | 实际 | 处理 |
| --- | --- | --- |
| `tests/test_profile_sources.py:64` | 64 行是 `reg.register_builtin(...)`，遍历在 63 行 | 已改成 63 |
| `jev.md:42-51` | 42 行是 `<!--`，规则在 43-50 | 已改成 43-50 |
| `README.md:719` | 719 行是代码围栏，目录树从 720 起 | 原文引的是「717-736」这个范围，无碍，不改 |

### 3.2 与既有决策对账

- **`README.md:24`「环境变量 < 用户级 < 项目级」** —— 那是 **LLM 配置**的优先级说明，不是全仓库的不变量。decide 只读环境变量是**刻意缩小**，方案 2.4 给了三条理由（密钥不落项目目录、端点不落分发包、进程级可审计）。若不写理由，下一个人会当 bug 修。
- **`MANIFEST.in:16`「sdist 按磁盘收集，不看 git」** —— 所以「把 key 抄进 docs / tests / scripts」会随包发布。这条与方案 2.5 的警告一致；顺带纠正一个**我自己会记错的事实**：本仓库的 sdist **不是**「只收 git 跟踪的文件」，`MANIFEST.in:12-14` 是 `recursive-include`，未跟踪的本地草稿也会进包。
- **`MANIFEST.in:21` `prune .openthree`** —— 工作区（含 `config.yaml` 里的 key）不入包，与「密钥只放环境变量」的取向一致。
- **`docs/memory-chroma-proposal.md` 被 exclude（`MANIFEST.in:18`）** —— 说明「提案文档进不进包」是逐份决定的事，不是默认。本方案进（无害），已写在 P2。

### 3.3 方案自身的硬伤（我写下的、现在还站不住的）

- 「在服务端只被当作数据，不会被当成指令执行」→ 已收回（P1-1）。
- 「首行」不含成本提示 → 待改（P1-2）。
- 「`python -m openthree.decide "…" questions.json"」这种调用形式不存在 → 已改成正确的子命令 + `--questions` 字符串（P0-1）。
- 「`--status` 是全局开关」→ 不存在这样的开关（`_add_globals` 只有 `--json` / `--quiet` / `--traceback`，`cli_tool.py:151-156`）→ 已改成 `decide_status` 子命令。

---

## 四、留给人类拍板的（我不替你定）

1. 环境变量命名：`JEV_*` 还是 `OPENTHREE_JEV_*`。
2. `JEV_BASE_URL` 写基础地址还是完整端点。
3. 要不要允许 `config.yaml` 覆盖（我建议不）。
4. 要不要自动重试（我建议不）。
5. `state` 要不要长度上限（我建议先不加）。
6. `noul` 方向：实现前必须用一正一反两个 state 实测（这是**前置条件**，不是可选项）。

---

## 五、这次评审真正改掉的五处

| # | 改之前 | 改之后 | 为什么 |
| --- | --- | --- | --- |
| 1 | `decide(state, questions: Dict)` | 明确要求同时接受 JSON 字符串 | 否则命令行不可用（`cli_tool.py:223-235`） |
| 2 | 「服务端不会把 state 当指令执行」 | 「它是数据；外部服务，别放不该外发的材料」 | 不替外部服务做承诺 |
| 3 | `python -m openthree.decide --status` | `python -m openthree.decide decide_status` | `--status` 不是全局开关 |
| 4 | `test_profile_sources.py:64`、`jev.md:42-51` | 63、43-50 | 行号对不上 |
| 5 | docstring 首行只写「取回概率分布」 | 首行加「会外发、会计费」（**已被 §六 推翻：只留「会外发」**） | digest 只取首行（`registry.py:586`），模型看清单时就该知道代价 |

前四处漏掉任何一处，读者照着文档实现会撞墙；第五处漏掉，模型只有在真正调用后（读完整 schema）才知道代价 —— 而那时钱已经花了。

---

## 六、第二轮评审：价格不进模型的视野（2026-11-05，第 3 轮裁决）

人类裁决原文：**「不要提价格，这会误导模型，影响客观判断。」**

### 6.1 裁决落成了什么

| 面 | 落成 |
| --- | --- |
| digest 那一行 | 「会外发」保留，「会计费」删掉 |
| 完整 `description` | 删「一次调用有一次调用的价钱和延迟」与「按量计费……别在循环里刷它」 |
| `properties[*].description` | 无价格词（`state` 改为「别替它整理」） |
| **返回值** | 服务端回的 `usage` **不转发**（`answers` / `model` / `elapsed_ms` / `attempts` / `id` 保留） |
| 人看的 README 安全声明 | **保留**「按量计费」「重试可能重复计费」—— 这是分层，不是漏改 |
| 守它的机制 | `test_no_price_in_model_facing_text`（6.3 第 15 条）：把带 `usage.cost` 的样本喂给假传输，断言四处的文本里一个价格词都没有 |

### 6.2 这条裁决推翻了我自己写下的三处判断（点名，别让下一个人当 bug 修）

- **§五 第 5 条**（首行加「会外发、会计费」）—— 推翻。原判据说「digest 只取首行，模型看清单时该知道代价」；新判断是**代价不该进入模型判据**。「会外发」保留：数据出口是安全事实，与钱无关。
- **§四 第 5 条**（`state` 上限「先不加，只回显 `usage.cost`」）—— 前半保留，后半作废。
- **§九**（「只在返回里给 `usage.cost`」）—— 改为「不回显 / 不落盘 / 不累计」。

### 6.3 我自己的硬伤

**P0-1（已修）`Returns` 段漏网。** 我第一刀把 docstring 的「首行 + 正文 + Args」换掉，`Returns:` 里仍写着 `usage（含 cost）`。

- 判据不是「我读了一遍觉得改完了」，而是**渲染脚本逐字断言**：`description` 与 `schema` 两处都 HIT `cost` / `usage`。
- 改法：已改；重跑后三处全 `CLEAN`。**这条正好证明 §3.5 的样本必须 `build_tool` 实跑** —— 手抄的样本会跟着我的记忆一起漏。

**P1-1（代价，已承认）本地不再有逐次成本。** 不转发就没有痕迹，不能假装「零成本」：

- 服务端账单是唯一真源；
- 想落到本地就得写第二份状态（§九 明确不做）；
- 所以本方案里「核账」是**外置**的。这是裁决的代价，不是遗漏。

### 6.4 会不会因此变「滥用」？

不设阈值、不回显价格，模型可能比以前多问几次。我的判断是**可接受**：

1. 调用判据写在「什么时候不该调」里，是**认知性**的（事实 / 动作 / 可计算 / 已有结论），不是经济性的；
2. 一次调用换来的是一个分布，不是「白跑」—— 不调它，模型就得自己拍板，而那正是它给不准的东西；
3. 真要观测滥用，看的是工具调用记录（`agent` 的 recorder 记工具名与参数），不是往模型的判断里塞价格。

这一条是判断不是事实：若人类认为「滥用成本 > 少问一次的风险」，翻回来只需改一处返回值 + 删一条测试 —— 但那时**必须先说明**「价格会进入模型判断」这件事本身就是设计取舍。

---

## 七、第一轮 ocr open code review（2026-11-05，`ocr review`，6 文件 / 7 条）

工具：`ocr` (`open-code-review v1.13.5`) + provider `glms-token-plan` / `deepseek-v4.1-flash-1017`。
评审对象：`decide` 实现的未提交改动（`openthree/decide.py` 新增 + builtins/cli_tool/llm/runtime + conftest）。
**7 条全部核实为真、全部修掉**，没有一条属于「评审不了解上下文」。按严重度：

**1（security · high，已实测复现）`run_cli(globals())` 把取环境的内部函数变成了子命令。**
`python -m openthree.decide jev_settings` 原样打印 `JEV_API_KEY`，绕过 `_display` 的打码 ——
实测输出里能看到哑 key 全文。§一 P2 表格里「接受」那条判断就此推翻。
改法两刀：函数改名 `_jev_settings`（私有，注册器与 CLI 都不再选它）＋ `__main__` 只点名
`{"__name__", "decide", "decide_status"}`。`__name__` 不能省：`is_registrable` 最后一条判据是
「函数是否在本模块定义」，少了它会两个工具一起被拒（这次真踩到了，`test_cli_*` 当场红）。
守它的是 `test_cli_keeps_internal_settings_off_the_command_line`（跑 `jev_settings` 子命令必须非 0 退出且不含 key）。

**2（bug · low）`_url_ok` 不校验端口。** `urlsplit` 对 `https://h:abc/x` 不报错，取 `.port` 才抛
`ValueError`，而 `post_json` 里那一句在 `try` 之外 —— 于是门控放行的地址到发请求才炸。改法：
门控里把 `.port` 问一次（`try/except ValueError`），并加进 `test_bad_base_url_disables` 的参数。

**3（bug · low）传输层之外的异常被说成「连不上 JEV 端点」。** 第 2 条触发后，模型看到的
是 DNS/代理方向，真正原因是本地抛了异常。改法：`_error_text` 先分流非 `TransportError`，
文案为「调用前的本地错误（类型名）」；`test_local_error_not_labelled_as_network` 守它。

**4（bug · medium）「没设置地址」与「地址不合法」报了同一句。** `_jev_settings` 把不合法的地址
也记进 `missing`，于是 `missing == [ENV_BASE_URL]` 时无法区分是谁。改法：`_jev_settings` 无论门控
过不过都返回原始取值，`_missing_reason(missing, values)` 据此分流；`decide_status` 里那句
补充说明的触发条件也随之写清（它只管「地址不合法、同时别的变量也缺」这一种）。
守它的是 `test_status_separates_absent_from_invalid`。

**5（security · low）7~11 字符的 key 露出 6 个字符。** 阈值 `len <= 6` 卡在「露出量刚好等于
全长的临界点上」。改法：短于 12 一律整条掩掉，`test_short_key_fully_masked` 守它。

**6、7（test · 两条）夹具的自洽性。** ①conftest 里三个变量名是抄来的字面量，改名后会静默失效，
改从 `decide` 导入常量；②`monkeypatch` 只清得动本进程，子进程测试在配了 `JEV_*` 的开发机上会翻车 ——
新增 `jev_free_env` 夹具，所有子进程调用显式传 `env=`。第 ② 条正是本轮 `test_cli_status_subprocess`
一直「碰巧绿」的原因：它没传 `env`，靠的是本机恰好没配 `JEV_*`。

**评审没抓到、我顺手核的一条**：`_jev_settings` 改契约后 `decide_available` 必须从
`bool(settings)` 改成 `not missing` —— 否则返回值永远是空的。这是改动的连带面，测试只覆盖了
行为、没覆盖写法，属于「改对了但下一刀容易改错」的地方，已在函数体里保持单一写法。

**代价与不变的部分**：门控语义、embedding 形态、模型可见文案（仍零开销表述）一个字节没变；
`registry` / `prompt` / `workspace` 未动。全量 `python -m pytest` → **641 passed**（本轮新增 7 条）。

---

## 八、第二轮 ocr（2026-11-05，6 文件 / 3 条 / 17m22s）

对修完的代码再跑一遍 —— 这一轮问的是「第一轮的修法有没有引入新问题」。3 条，**2 修 1 接受**：

**1（security · medium，已修）`decide_status` 原样显示 `JEV_BASE_URL`。**
`https://user:pass@host/...` 或 query 里带 token 的端点会把凭据打进模型上下文与 recorder 日志。
更隐蔽的一半：`post_json` 只发 Bearer `JEV_API_KEY`、**不消费 userinfo** —— 这种地址既泄了凭据，
又静默地不带那套认证。改法：`_brief_url` 只在显示时摘掉 userinfo 与 query（其余原样：
端口、路径写错了要看得出），不动门控（带 userinfo 的地址照样能用，只是别指望它做认证）。
守它的是 `test_status_masks_url_credentials`。

**2（bug · low，已修）`_url_ok` 用 `netloc` 判「有没有主机」。**
`https://:80/x` 的 netloc 是 `:80`（非空）却没有主机名 —— 门控放行，`post_json` 才抛
「URL 里没有主机名」，又被当成本地异常报出来（第一轮第 3 条刚把它归到「本地错误」，于是
一个配置错会被说成「调用前的本地错误」，方向仍不对）。改法：判 `parts.hostname`（与
`post_json` 的检查同一条判据）；`https://:80/x`、`https://user@/x` 进了参数化用例。

**3（maintainability · low，接受不改）`python -m openthree.builtins --help` 里没有 `decide_status`。**
属实：`is_registrable` 的最后一条是「函数必须在本模块定义」，而 `decide_status` 的
`__module__` 是 `openthree.decide`。但「修」它只有一条路 —— 在 `builtins` 里再包一层同名工具，
那正是方案反复拒绝的「第四份拷贝」。改为**把它写成已知边界**：跨模块的内置工具在自己的模块
CLI 里（`python -m openthree.decide decide_status`），`builtins` 的 CLI 规则就是「本模块公开
函数全体」，不为一个函数破例；`installed_builtins` 作为子命令出现是这条规则的副产物，无害。

这一轮之后全量 `python -m pytest` → **644 passed**（净增 3 条）。

---

## 九、第三至九轮 ocr（2026-11-05，收敛过程）

| 轮 | 条数 | 说明 |
| --- | --- | --- |
| 1 | 7 | 首轮，含 1 条 high（CLI 泄漏裸 key） |
| 2 | 3 | 修完后复跑 |
| 3 | 2 | |
| 4 | 3 | 其中 1 条是设计层面：门控按注册期定、配置却每次重读 |
| 5 | 6 | 3 条 medium，2 条出自我上一轮引入的快照 |
| 6 | 3 | 2 条是掩码的边界形态 |
| 7 | 5 | 2 条 medium：`optional_builtins()` 每次调用都重冻快照 |
| 8 | 4 | |
| **9** | **0** | **收敛** |

九轮共 33 条，**全部逐条对源码核实过，没有一条属于「评审不了解上下文」**；修了 32 条，
1 条明确接受不改（`python -m openthree.builtins --help` 里没有 `decide_status`：修它要在
`builtins` 里再包一个同名工具，而 `is_registrable` 的判据是「本模块定义」—— 那正是本方案
反复拒绝的第四份拷贝；跨模块工具在自己的模块 CLI 里，见 §八-3）。

**修的东西按性质归类，而不是按轮次流水账：**

1. **门控必须是「post_json 解析得了」的充分条件。** `_url_ok` 从 `scheme + netloc` 逐步收紧到
   `hostname` / 端口可解析 / 端口非 0 / 只允许可打印 ASCII —— 每一次都是同一个理由：门控放行的
   地址，不能在发请求那一刻才炸（那会把排障方向引向网络，还白花两次重试）。
2. **凭据掩码按「段」定位，而不是按整串里的第一个 `@`。** `_brief_url` 三轮演化：authority 段必掩；
   其余段只掩 `user:pass@` 形态（少写冒号/斜杠时凭据会落进 path）；`/users/@me`、`a@b.com` 这类
   路径里的 `@` 保留可读；query 与 fragment 整段掩；解析失败 fail-closed（不回退原文）。
3. **门控与配置必须来自同一份事实。** 注册期一次性冻结（`_registered` + `_snapshot`）；
   `optional_builtins()` 从「每次调用重判」改成「只在第一次冻一次」（否则诊断代码或测试再调一次
   就能把已注册的端点悄悄换掉）；`decide` 用快照发送；`decide_status` 报**在用的那套**并提示漂移。
4. **形状错不花请求。** `type` 严格相等（归一化后转发原文 = 用一次 400 买单）；`instructions`
   先判类型再 `strip`（`123` 会被 `str()` 蒙过去）；非字符串 `state` 用 JSON 序列化而不是 Python repr。
5. **不转发开销这条不变式要覆盖错误路径。** `_brief` 连键名都不列、非 dict 只给类型名 ——
   「响应缺 answers」这条错误文案同样进模型上下文。
6. **夹具的自洽性。** `raising=False` 去掉（内部属性改名要在 fixture setup 当场炸）；
   `jev_free_env` 覆盖子进程（`monkeypatch` 只管本进程）。

**代价**：`decide.py` 从 326 行涨到约 470 行，其中相当一部分是掩码与门控的边界判断。
这是「诊断入口必须诚实、且不能说漏嘴」的直接成本；换来的是 33 条里没有一条留在代码里。

---

## 十、第十轮：`decide_status` 整条移除（人类裁决，2026-11-06）

人类的一句话：「`decide_status` 不应该存在吧」。逐条核实后接受，并且是**整条移除**，
不是修补。§七-§九 保留原样 —— 它们是那九轮评审的记录；本章记的是之后的这次裁决，
其中「接受不改」的那条（§八-3：`builtins` 的 CLI 里没有它）随之失效：不是被修好，
是整条不需要了。

**判据（为什么它不该存在）：**

1. **「不写就是零」**。它无条件常驻，而它唯一的服务对象是一个**默认不存在**的工具。
   于是每一个进程、每一步的 system 工具清单都替它付字节（`registry.digest()`），
   模型还有机会去调一个不存在的功能 —— 这是「多一条要维护的路径」的典型形态。
2. **两套时序必然发散**。门控在注册期判定一次（`_registered` + `_snapshot`），诊断在
   调用期读环境；这条缝正是这几轮反复出事的地方（第四轮「用实时 env 冒充注册结果」、
   第七轮「status 报实时值」）。修了四轮之后它仍是全模块最脆的一段 —— 那不是
   「越来越结实」，那是它在提醒你它本不该在。
3. **诊断需求有更好的归宿**。三个环境变量是否设置，看环境本身即可
   （`set JEV_` / `echo %JEV_MODEL%`）；门控判据（三个齐全 + http(s) 绝对地址 +
   重启进程）写进 README 就是完整答案。为单个可选工具造专属诊断入口，
   等于承认门控本身没写清。
4. **它自带成本**：`_status_head` / `_status_verdict` / `_display` / `_brief_url`
   四个专用辅助全为它存在，其中 `_brief_url` 是「非法地址原文唯一会被显示的地方」
   （三轮掩码演化都长在这条路径上）。

**改了什么：**

| 文件 | 改动 |
| --- | --- |
| `openthree/decide.py` | 删 `decide_status` 与四个专用辅助（115 行）；模块 docstring 写明「不放常驻的诊断入口」；尾钩只点名 `decide` |
| `openthree/builtins.py` | 常驻集里删掉该条目；import 只留 `optional_builtins` |
| `tests/test_decide.py` | 删 16 个 status 用例；新增 `test_no_diagnostic_entry`（既不在 `installed_builtins()`、也不在模块上）；CLI 用例改为断言 `--help` 里只剩 `decide` |
| `tests/conftest.py` | `_reset_decide_registration` **保留**：`installed_builtins()` 的注册期冻结仍会跨测试污染；判据改写为「快照复位」 |
| `README.md`、提案 §3.5 / §4.2 / §5.5 / §6.1 / §6.4 / §九 | 同步：没有诊断入口；配没配看环境变量本身 |

**保住的不变量**（§七-§九 的结论一条没退）：注册期一次判定、发送走注册期快照、
`installed_builtins()` 只在注册期算一次、`decide` 的文案与形状不变、
`_missing_reason` 仍被 `_settings_for_call` 使用（错误文案照样区分「没给」与「不合法」）。

---

## 十一、未提交整包复核（2026-11-06，第 11 轮）

前面十节评审的是**方案**与**实现过程**；这一节评的是「盘上这一堆未提交改动，作为一个整体，
是否符合内核设计、是不是正向的改动」。评的对象是 diff 本身，不是方案里的说法。

### 11.1 结论先行

**这一版符合内核设计。** 判据有三条，都能指到机制而不是文案：

1. **工具的存在是注册期的产物，不是提示词里的句子。** 判定点只有一个
   （`builtins.py:2928 installed_builtins()`），两个生产调用点都接线到它
   （`runtime.py:105`、`cli_tool.py:110`）。`workspace.py` **一个字节没动** —— 也就是说
   `decide` 进模型视野**没有靠改提示词**，靠的是 `Registry.digest()` 从注册表长出来
   （实测：配置后 digest 里多出 `- decide(state: string, questions: object) — … 会外发。`）。
   这正是「万物」的方向：能力从注册表长出来，而不是写进「一」。
2. **零默认值。** 三个 env 缺一个就没有这个工具；`installed_builtins()` 在未配置时返回
   37 个工具、digest 与 `schemas()` 里都没有 `decide`（实测，见 11.7）。「默认端点等于
   替人决定数据发给谁」这条判断落成了代码结构，不是靠 README 提醒。
3. **内核只搬运。** 概率、confidence、分数一律原样返回，不做阈值、不做四舍五入、不转发
   `usage`（`decide.py:278-279`、`decide.py:310-322`）。智能留在模型与外部服务两侧，
   内核这边只有形状校验与传输。

**是正向的**，但它正向的理由要落在机制上：`post_json` 这一步把「路径从哪来」变成参数
（`llm.py:430` / `llm.py:522`），于是代理、TLS、gzip、超时、错误处理仍然只有**一份**——
这是「一个事实一个实现」在传输层上的落实。如果当时的做法是另抄一份「差不多的」HTTP 客户端，
这一版就该判为负向。

### 11.2 盘上这一堆到底是什么（逐文件）

| 文件 | 量 | 在系统里是哪一层 |
| --- | --- | --- |
| `openthree/decide.py`（新） | 399 行 | 能力层：一个工具 + 它的门控与排障文案 |
| `openthree/builtins.py` | +16 | 判定层：`installed_builtins()` = 常驻集 + 条件集 |
| `openthree/runtime.py` | 2 行 | 接线：注册期遍历改成单一判定点 |
| `openthree/cli_tool.py` | 2 行 | 接线：离线 CLI 同一条判定点 |
| `openthree/llm.py` | +28/−14 | 传输层：`post_json`（URL 由调用方给）+ `post_chat` 三行包装 |
| `tests/conftest.py` | +51 | 测试环境：清 `JEV_*`、复位快照、给子进程一份干净 env |
| `tests/test_builtins.py` / `test_function_sources.py` / `test_profile_sources.py` | 各 2 行 | 跟随单一判定点 |
| `tests/test_decide.py`（新） | 436 行 | 判据（全部不打真网） |
| `README.md` | +31 | 人读的边界：配置、门控、以及「会外发」的安全声明 |
| `docs/decide-tool-proposal*.md`（新） | — | 决策记录（方案 + 自评审） |

**不在本次提交范围**：`docs/memory-chroma-proposal.md`（未跟踪）。它是另一件事的草案，
且 `MANIFEST.in` 里有专门一条 `exclude docs/memory-chroma-proposal.md` 把它排除在 sdist 之外。
若用 `git add -A` 提交，它会变成「git 跟踪、但 MANIFEST 声明不属于本项目」——两处真源互相
打脸。见 11.6。

### 11.3 八问逐条（对着这一版的代码，不是对着方案）

| 问 | 这一版怎么答 | 判据 |
| --- | --- | --- |
| 道（无硬编码智能） | 不设阈值、不四舍五入、不解释概率、不转发 `usage` | `decide.py:15` 的模块 docstring；`278-279`；`314-315` |
| 一（可写 · 可回滚） | 种子提示词零改动，工具清单由 `digest()` 长出；改写提示词那条路没被动过 | `git diff --stat -- openthree/workspace.py` 为空；实测 digest 多出 decide 那一行 |
| 二（磁盘即真源） | 配置的唯一真源是三个 env（`_jev_settings` 不读 `config.yaml`、无别名、无默认值）；`_registered`/`_snapshot` 是**防绕过**的引用副本，理由写在 `decide.py:57-64` | `decide.py:70-83`、`126-138`、`141-155` |
| 三（循环不预设） | 不规定模型先做什么、再做什么；什么时候该调、什么时候不该调写在工具描述里（工具也可以是知识） | `decide.py:245-254` |
| 万物（自造） | 新工具不改提示词就进了视野 —— 能力从注册表长出来；代价是它不是自造而是内置（见 11.5-3） | `installed_builtins()` + `digest()` |
| 极简 | 一个开关都没加：没有 `--decide`、没有 extra、没有默认值、没有诊断入口 | `installed_builtins()` 是唯一入口 |
| 成本 | 未配置时 system 零字节（实测 37 工具、digest 无它）；配置后每步固定一行；进程内只多一次 import | 11.7 的实测输出 |
| 诚实 | README 的三条声明都指得到机制：零痕迹 → `installed_builtins()`；会外发 → 描述首行 + 安全段；看环境变量 → 没有诊断入口 | `README.md` decide 段；`tests/test_decide.py::test_no_diagnostic_entry` |

### 11.4 这一版里唯一的 P1，以及它改成了什么

**判据（怎么发现的）**：`decide.py` 上有一个**公开**函数 `decide_available()`，读的是**实时**
环境变量，生产代码里零调用（只有测试在用）。它回答的问题叫「decide 可用吗」，而设计里这个
问题的唯一答案是 `installed_builtins()`。在唯一答案旁边放一个会漂移的公开答案，等于给第十轮
删掉的那类 bug 留了个后门 —— `decide_status` 当初就是「用实时 env 冒充注册结果」。

**改法**：改名为私有的 `_env_configured()`，docstring 写清「只看环境，不代表工具在不在；
唯一判据是 `installed_builtins()`」。同步 5 处测试调用与 CLI 内部名清单
（`tests/test_decide.py`）、方案 §4.4 的草图与 §6.1 第 1 条。

**附带改的一处（措辞，P2）**：`test_no_diagnostic_entry` 原先断言「`decide_status` 不在」——
那是在点名一个已经不存在的符号，一年后读它的人不知道 `decide_status` 指什么。现在断言的是
**现在必须成立的不变量**：「以 `decide` 开头的工具只有 `decide` 一个，模块上也没有第二个公开的
`decide*` 入口」。这条同时也把 11.4 那个改名钉死了 —— 谁再加一个 `decide_available` 这类公开
入口，测试会当场红。

### 11.5 接受不改（每条给理由，别让下一个人当 bug 去「修」）

1. **「冻结」是模块级变量（同一事实的第二份拷贝）。** 拒绝的替代方案：每次调用重读 env ——
   那正是被堵的洞（注册后换端点，材料就发往一个从未被检查过的地方）；放 `KERNEL.state` ——
   那是草稿纸，语义更差。代价明确落在 `tests/conftest.py` 要复位 `_registered`/`_snapshot`，
   夹具 docstring 已写明「属性改名要当场炸，而不是静默变空操作」。
2. **`decide.py` 从 `llm` 导入私有的 `_retryable` / `_backoff`。** 第二份拷贝更贵：重试判据一旦
   分叉，401 会被重试、429 会被吞掉，而两处都自称对。
3. **内核里放了一个厂商协议适配器（JEV）。** 这是这一版**最大的设计代价**，得说清：内核从此
   知道「JEV 的请求体长什么样」。换来的是零痕迹门控、与传输层复用、以及工具描述随注册自动进
   视野。若将来出现第二个决策服务，正确做法是抽接口，而不是再塞一个 `decide2`。
4. **`python -m openthree.builtins --help` 里看不到 `decide`。** `is_registrable` 的最后一条是
   「函数必须在本模块定义」；为跨模块工具开口子，比给它一条自己的 CLI
   （`python -m openthree.decide --help`）贵。
5. **README 里的工具数示例（`36 个工具（内置 33 / 自造 3）`）。** 那是终端快照，已就地标注
   「示例；实际数量随工具集变」。把数字改成 37/38 只会让它下次再过期。

### 11.6 待人类拍板（我不替你定）

**`docs/memory-chroma-proposal.md` 的去留。** 三条路：保持未跟踪（现状）／跟踪并删掉
`MANIFEST.in` 里那条 exclude（跟着 sdist 发布）／删掉。它现在「git 看不见、MANIFEST 专门排除」，
合规但缺一个真源。我的倾向：**本次不提交**（与 decide 无关），下一件事开始时再定。

> 已定（2026-11-06，人类裁定「移除」）：文件已从磁盘删除，`MANIFEST.in` 那条 `exclude` 一并
> 撤掉——这条路由上面三条路里的第三条。留下的可复用调研事实在记忆里，不在本仓库。

### 11.7 验收（两条都能复跑）

- `python -m pytest tests -q` → 全绿（exit=0，`tests/` 全部通过，其中 `test_decide.py` 57 条）。
- `.openthree/tmp/probe_review_pack.py` 实测：
  - 未配置：工具数 **37**、digest 无 `decide`、`schemas()` 无 `decide`
  - 配置后：工具数 **38**、digest 里出现
    `- decide(state: string, questions: object) — … 会外发。`、`schemas()` 有 `decide`
  - 模块上公开的 `decide*` 入口只有 `decide`；工具清单里 `decide*` 也只有 `decide`
- `git diff -w --stat` == `git diff --stat`（换行没被搅动）；
  `git diff --stat -- openthree/workspace.py` 为空（种子零改动）。

### 11.8 第十一轮 ocr（2026-11-06，6 文件 / 0 条 / 7m38s）

`ocr review --audience agent` 审了 `openthree/decide.py`、`builtins.py`、`cli_tool.py`、`llm.py`、
`runtime.py`、`tests/conftest.py`（`.md` 与 `tests/*.py` 不在它的默认范围内），结论 **0 条**。

与第十轮相比，这一版只多了 11.4 那次改名与两条测试断言改写，所以「0 条」的含义是**这次修复
没有把旧问题修成新问题**，而不是「整包已经无可挑剔」—— 它审不到 `.md`、也审不到
`tests/test_decide.py`，而这两处正是 11.4 改动最大的地方（测试与文档由 pytest 与本节的人工
对账兜住：`test_no_diagnostic_entry` 现在会钉住「模块上不允许出现第二个公开的 `decide*` 入口」）。

**本节引用的行号都是改名之后的**（改名让 `decide.py` 从 399 行变成 404 行，`decide()` 之后的
一切后移 5 行）：`_env_configured` 在 `115`、`optional_builtins` 在 `126`、`_settings_for_call`
在 `141`、`decide()` 在 `244`。






