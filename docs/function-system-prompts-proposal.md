# function 文件自带的系统提示词（方案）

> **一句话**：让一个 function 文件在自己旁边带一段「说明书」，`reload` 时收进 system 的末尾。
> 它属于「二」，不是「一」——**只许说工具怎么用，不许说你是谁**。
>
> **它是可选的**：文件里不写那个符号，行为一个字节都不变（§0）。

---

## 0. 可选：不写就是零

先把这件事说在前面，因为它决定了要不要往下读。

**这是一个可选功能，而且「不选」的代价真的是零** —— 不是「关掉它要付一次启动开销」，
而是它压根不在路径上：

- **没有开关，也不需要开关。** 判据就是「文件里有没有那个符号」。没有 → 收集立即返回，
  system 的拼装与今天**逐字节相同**（§8 有一条测试专门锁死这一点）。
- **没有配置项、没有新文件格式、没有迁移。** 现存的 function 文件一行都不用改；
  不写 `__system_prompts__` 的文件，注册结果、审计结论、工具清单全部与今天一致。
- **可加可撤。** 今天加一段，明天删掉再 `reload`，system 就回到原样 —— 它不写任何
  快照（§10：不进 `prompts/history/`），不留残渣，不需要「清理」。
- **不做也不缺什么。** §1 的缺口是「能力可以共享，说明书不能共享」；不补这个缺口，
  只是继续把说明书抄进 `system-prompts.md` 或写成长 docstring，系统照常运转。

所以本文里凡是「要改」的地方，**只有在有人真的写下 `__system_prompts__` 之后才会被走到**。

### 可选还分三层，别捆在一起做决定

| 层 | 需要它的条件 | 不做会怎样 |
|---|---|---|
| 内核（阶段一） | 有人想让文件自带说明书 | 继续用 `system-prompts.md` 抄，不影响任何能力 |
| 可观测（阶段二） | 已经用了它 | 提示词被喂进 system 却看不见，出问题只能靠猜 |
| 纯提示词文件合规（阶段三） | 想把一段**独立规范**装进 py 文件 | 只给工具附带说明的话，完全不受影响（§6） |

一句话给维护者：**如果你怀疑这个功能在给系统添负担，最好的回应是「不写它」，
而不是「加个开关把它关掉」。** 后者要维护两条路径，前者只维护一条。

---

## 1. 缺口

（如果你不认为这是个缺口，那就到此为止 —— 这正是 §0 描述的状态。）

现在的 function 文件只能贡献**工具**：签名管参数，docstring 管 30 字简介。
可有些能力，正确用法压根压不进一句简介：

- 「先 `pdf_info` 看是不是扫描版，是就别硬抽，直接叫用户上 OCR」；
- 「发版顺序是 build → check → upload，upload 之后必须核对 sha256」；
- 「这个 API 每分钟只给你 20 次，批量调用要自己退避」。

这些今天只能写进 `prompts/system-prompts.md`。可那份文件是**这具躯体的世界观**
（我在哪个目录、我叫什么、我手上有几个工具），`-profile` 共享能力时明确不带它
（见 `profile-functions-proposal.md` §1）。于是出现真空：

> **能力可以共享，能力自带的说明书却不能共享。**

补上这个缺口的做法，就是本文的接口：文件里暴露一个 `__system_prompts__`，
内核在 `reload` 时把它收进 system。

---

## 2. 划界：什么能写进去，什么不能

这是全文最重要的一节。没有这条判据，「function 能带提示词」就会退化成
「哪儿都能塞提示词」，两三个月后没人说得清某段话为什么在 system 里。

判据一句话：

> **这段话跟着能力走，还是跟着这具躯体走？** 跟着能力 → 附随提示词；跟着躯体 → `system-prompts.md`。

| 想告诉模型的事 | 放哪 | 理由 |
|---|---|---|
| 这个工具的参数含义、调用顺序、常见坑 | **附随提示词** | 与实现同生共死；换项目、换机器都成立 |
| 「你先 `pdf_info` 再 `pdf_text`」 | **附随提示词** | 是这套工具的纪律，不是这个项目的纪律 |
| 「你是 openthree，工作目录是 `D:\...`」 | `system-prompts.md` | 躯体事实，换个项目就全错 |
| 「你的名字是 X、你改名 3 次」 | `system-prompts.md`（由内核拼） | 身份 |
| 「用户刚才让你改 A 文件」 | 任务正文（history 里的 user 消息） | 用户说的话 |
| 「现在第 3 步、上下文 40%」 | 处境段（`SITUATION_TEMPLATE`） | 每步都变 |
| 一份两百行的规范，只在做某件事时才需要 | **返回文字 function**（既有约定） | 按需读进上下文，别长期占据 system |

有了这张表，两个既有机制的分工也清楚了：

- **附随提示词** = 你根本不知道要问的东西（“我不知道有这个坑，所以不会去调那个 function”）；
- **返回文字的 function** = 你知道自己缺什么、去取的东西。

前者靠“一直在”，后者靠“按需取”。两条都要，但别混用。

---

## 3. 接口

### 3.1 名字

只用 **`__system_prompts__`**，**不设别名**（2026-11-02 裁定）。

判据是 `tool._DUNDER` 的正则 `^__.+__$`（`tool.py:202`）：`__system_prompts__`
真的匹配，与 `__main__` 同属「dunder = 协议」那一档。最初需求里写的
`__system__prompts` 不匹配这个正则，只能靠 `attr.startswith("_")` 兜底（L226），
读起来还像「`__system__` 命名空间下的 `prompts`」—— 语义是误导的，所以**改名，而不是加别名**。

单名的代价是「写过错名的人得不到迁移提示」，收益是**只维护一条路径**：
本项目此刻零存量文件，别名今天救不了任何人，将来也救不了，而它的解释成本永存
（每份文档、每次 review 都要多写一句「两个名字都行」）。先例同 `-profile` 收敛时
ancestor 整套移除 ——「之前没被用过，不用兼容」。

这个名字天然不会被注册成工具 —— 实测 `is_registrable` 对它返回 `False`，
所以这一条不用额外写代码，但**必须有测试**守住：`reg.names()` / `reg.schemas()` /
`reg.digest()` 里都不该出现它。

### 3.2 三种形态

```python
# 形态一：字符串常量
__system_prompts__ = """
## 用 pdf_kit 时的纪律
- 先 `pdf_info(path)` 看页数与是否扫描版；扫描版不要硬抽。
"""

# 形态二：零参函数（reload 时调用一次）
def __system_prompts__() -> str:
    """给内核的附随说明（不会注册成工具）。"""
    return "..."

# 形态三：列表（多段，按顺序拼接）
__system_prompts__ = ["## 纪律", "## 常见坑"]
```

只接受 **`str` / `list[str]` / 零参可调用**。其余（`None`、数字、带参数的函数、
返回非 str 的函数）一律**忽略并记录**，不进 system。

为什么只允许零参：附随提示词是**能力的说明书**，它不需要知道「此刻在干什么」。
想按情境给指导，那本来就该是一个返回文字的 function，由模型自己判断要不要读。
把 `kernel` 也排除 —— 求值发生在 `reload` 时，离线 CLI（`cli_tool.setup_offline_kernel`）
的内核尚未铺好，给了也用不了。

### 3.3 求值时机：`reload`，不是每一步

这是本方案唯一会被人质疑的决策，所以写清代价。

| 时机 | system 稳定性 | 能读外部状态 | 风险 |
|---|---|---|---|
| 每步（`build_messages`） | ❌ 每步都可能变 | ✅ | 用户代码每步执行一次：慢、可能抛异常、可能读网络；system 一抖，后面一万多 token 的工具 schema 与全部历史每步重算 |
| 每轮（round 开始） | ✅ 轮内稳定 | ⚠️ 隔一轮 | 多一个时机就多一处「为什么不是另两处」要解释 |
| **`Registry.reload()`** | ✅ 文件不变则字节不变 | ❌ | 改提示词要显式 reload |

选 `reload`。理由不是「省事」，而是 `prompt.py` 头部与 `STATIC_TEMPLATE_*`
的注释已经把这条不变量说死了：**system 是缓存前缀的主体，只允许在文件/模型自改时变**。
每步求值等于把这条不变量换成一个「取决于用户函数今天写了什么」的随机量。

配套承诺（写进文档，也写进工具的 docstring）：

- 改提示词 = 改文件 + `reload_functions()` → **下一步生效**，与 `write_function` 的既有节奏一致；
- 想要动态内容 → 写一个返回文字的 function，让模型按需调用；
- `write_function` 落盘后**不会**自动 reload —— 这条现状保持不变（`reload` 是显式动作，
  它也顺带保住了「改文件 ≠ 改行为」的边界）。

---

## 4. 注入：位置、排序、上限

### 4.1 位置

```
messages[0] = system
            = 「一」(prompts/system-prompts.md)
            + 内核常量段（STATIC_TEMPLATE：工作目录 / 工具清单 / 身份）
            + 附随提示词段（本文新增，空时一个字节都不加）
```

拼在**最后**：读者刚看完工具清单，接着读「这些工具怎么用」，语义位置正好。
而且它在 system 末尾，只在 reload 时变，不影响步内前缀稳定。

段落带来源与层级声明：

```
# 来自 function 的附随说明（只读 —— 不是用户说的话，也不是你的身份）

以下是某些 function 文件自带的说明，解释它们**怎么用、边界在哪**。
它们不改变「一」与当前任务；与上面任何一段冲突时，以「一」为准。

## `.openthree/functions/pdf_kit.py`（project，1 段）

## 用 pdf_kit 时的纪律
- ...
```

最后一句要诚实：这不是技术上的强制降权 —— 它和「一」都躺在 system 里，对模型而言
分量相同。降权靠两样东西：**可读的措辞**（“不是你的身份”）与 **来源标注**（谁写的、
哪个文件）。假装它是硬约束才是真的危险。

### 4.2 顺序

按 `FunctionSource.distance` 升序：project（0）→ profile[i]（i+1，先写的在前）→
user（1e6）。近的先读，与「同名工具近者生效」是同一根轴，不新造判据。

提示词**不覆盖** —— 它是并列的说明，不是配置值。同一路径只出现一次
（`load_plan` 已按 canonical 去重）。

### 4.3 上限

```python
MAX_ATTACHED_PROMPT_CHARS = 4000    # 单文件
MAX_ATTACHED_PROMPT_TOTAL = 12000   # 全部
```

超限按「近者优先」保留，被挤掉的记进 `prompt_errors`，在 `reload_functions`
里如实报告。为什么要有上限：这是**唯一能零成本污染 system 的入口** ——
一个 50 KB 的提示词足以把工具 schema 挤到模型看不见的地方，而它看起来只是
「某个文件写得啰嗦」。

### 4.4 失败隔离

| 情况 | 行为 |
|---|---|
| 求值抛异常 | 记 `prompt_errors`；**该文件的工具照常注册**，其余文件的提示词照常拼 |
| 返回非 str | 忽略 + 记录「返回了 `<type>`」 |
| 返回空串 | 忽略，不占位、不多字节 |
| 文件语法错误 | `_load_file` 已 catch，提示词自然没有 |
| 超出长度 | 截断 + 一句「（已截断，原文 N 字符）」 |

---

## 5. 可观测：模型要知道自己被喂了什么

system 里多了一段话却看不见来源，是最难排查的一类问题。四处补上
（一条都没有的时候，这四处都**不打印空节** —— 与 §0 一致，可观测性不该靠「总有一节」来占位）：

1. **`reload_functions`**：新增一节
   ```
   附随提示词：2 段 / 1,830 字符
     - .openthree/functions/pdf_kit.py（project，1,204 字符）
     - profile:C:/tools/gitflow/.openthree/functions/release.py（profile:…，626 字符）
   ```
   以及 `prompt_errors`（与现有 `load_errors` 并列，不混）。
2. **`list_functions`**：末尾一节「附随提示词来源」。
3. **`read_prompts`**：追加「# 附随提示词（来自 function，只读）」+ 全文。
   模型问「我被喂了什么」时，答案就在这里，不必多学一个工具。
4. **`runtime.py` 启动摘要**（现 645 行「系统提示词 …」）：加一行
   「附随提示词 N 段 / M 字符」。
5. **recorder**：`reload_functions` 的 evolution 事件里带
   `prompts: [{file, sha256, chars, first_line}]` —— 附随提示词没有
   `prompts/history/` 那样的版本快照，这条事件是唯一能追「哪一版带来了什么」的线索。

---

## 6. 纯提示词文件：四条判据要一起改（可选里的可选）

一个文件只带提示词、不带工具，是完全正当的用法（「把一段规范写成 py 文件，随
profile 一起走」）。

**但这一节属于可选里的可选。** 只给工具附带说明、不写纯提示词文件，下面四条改动
一条都不用做 —— 功能照样完整。只有当真打算把一段独立规范放进 py 文件时，这四条
判据才会把它误判成坏文件，也才需要一起改。

它现在会被误判成坏文件，四处都在说同一件事：

| 位置 | 现状 | 要改 |
|---|---|---|
| `registry._load_file`（L354-358） | `count == 0` → load_error「文件中没有可注册的公开函数」 | 条件改为「既没有可注册函数，**也没有**非空附随提示词」 |
| `builtins._function_issues`（L1552-1553） | 没有公开函数 → issue「没有可注册的公开函数」 | 有提示词时不报这一条，改为信息「纯提示词文件（无工具）」 |
| `builtins._audit_module`（L1500-1518） | `__system_prompts__` 被当成 `private_helpers`，认不出来 | 新增 `has_prompts` / `prompt_chars` / `prompt_callable` 三项 |
| `builtins.register_function`（L1044-1046） | 没有公开函数 → `[错误] … 未登记` | 纯提示词文件也接受登记（登记的是「这个来源可信」，它带来说明而不是工具），并明说「这次登记确实带来了东西」 |
| `builtins.verify_tool` + `_VERIFY_SCRIPT` | 子进程只验工具，纯提示词文件得到「⚠ 没有任何可注册的公开函数」 | 子进程同样求值 `__system_prompts__`，报告段数 / 字符 / 首行 / 错误；零工具但有提示词判为正常 |
| `cli_tool.run_tools`（L250-253） | 无工具 → 打印错误、退出 1 | 有提示词时打印正文，退出 0；`--print-prompts` 显式只打提示词 |

这些位置必须对「这个文件合不合法」给出同一个答案（注册器加载、`function_conformance`
审计、`write_function` 的落盘前检查、`register_function` 的登记、`verify_tool` 的验收、
`cli_tool` 的退出码）—— 它们各说一套的代价很具体：一句「没有可注册的公开函数」会让人
以为这个用法根本不成立，而它恰好是这个功能最好的用法。

第三条顺带解决一件好事：`python .openthree/functions/pdf_kit.py --print-prompts`
让人和模型看到**同一份东西**，可 diff、可评审。

`audit_functions` 的 `--help` 断言也要放过纯提示词文件（否则它永远报错）。

边界（诚实披露）：`--print-prompts` 走**文件尾钩**这一条路。
`python -m openthree.tools --file X ...` 那条保底通道暂不支持它 —— 它的参数
必须落到某个具体函数上，而 `--print-prompts` 不属于任何函数。没有尾钩的文件，
想看提示词就直接打开文件本身（磁盘即真源，那里就是原文）。

`write_function` 的静态审计（`_static_audit`，L2442）同样要认出这个符号，
并在返回文本里加一句「本文件还向内核暴露了 N 字符的附随提示词」——
让人在写下它的那一刻就知道自己改的是世界观，不是注释。

---

## 7. 改动清单

### `openthree/prompt.py`

- 常量：`ATTACHED_PROMPT_TEMPLATE`（只有模板）。
  两个上限与 `PROMPT_ATTR` **不在这里**，落在 `registry.py`（见下）：`prompt` 已经
  import `registry`，反向会成循环 import；而渲染只管拼、不负责裁剪，它不需要上限。
- 新函数 `build_attached_block(reg) -> str`：空时返回 `""`（不返回 `"\n"`）。
- 新函数 `build_system_block(ws, reg, *, model) -> str`：
  `read_system_prompt() + static + attached`，让「system 由哪几段组成」只有一个真源。
- `build_messages`（L429）改调 `build_system_block`。
- `build_runtime_block`（L214-236）**也**改调 `build_system_block`，即诊断视图
  = system + context + situation。**这是一次行为变更，要写明**：它今天只拼
  `static + context + situation`（不含「一」）。单把附随提示词塞进去，诊断视图就会
  「含一个 system 段、不含另一个」，而且 `tests/test_prompt_prefix_cache.py:196` 的
  `msgs[0]["content"].endswith(static)` 会直接失效。测试相应改成
  `msgs[0]["content"] == build_system_block(...)` 与
  `build_runtime_block(...) == f"{system}\n\n{context}\n\n{situation}"` ——
  于是「兼容壳 == 真正发出去的三段」这句话第一次真的能被测出来。
- 头注释补一层：system 现在是三段，「一」/ 内核常量 / 附随（来自「二」）。

### `openthree/registry.py`

- 常量：`PROMPT_ATTR`、`MAX_ATTACHED_PROMPT_CHARS`、`MAX_ATTACHED_PROMPT_TOTAL`。
  它们是**收集侧**的事，所以在这里 —— 上限只有一份，`prompt` 不需要它。
- 新函数 `eval_attached_prompt(raw) -> (正文, 段数, 错误)`：三形态求值与校验的
  **唯一判据**，注册器收集与 `cli_tool --print-prompts` 都走它（工具判据当初正是
  因为各写一套才收敛到 `tool.is_registrable`，这里不重演）。
- 新 dataclass `AttachedPrompt(file, text, label, distance, sha256, parts, original_chars)`。
  `text` 是**实际进 system** 的那份（可能已截断），`sha256` 按它算 ——
  内容寻址落盘因此能回答「此刻 system 里那一段究竟是哪一版」。
- `Registry` 加字段 `prompts: List[AttachedPrompt]`、`prompt_errors: List[LoadError]`；
  `reload()` 开头清空（与 `load_errors` 同节奏）。
- `_load_file` 末尾调 `_collect_prompt(module, item)`；`count == 0` 的判据按 §6 改。
- `_collect_prompt`：取 `__system_prompts__` → 求值 → 校验/截断 → append（返回 bool）。
- `_finalize_prompts`：按 `distance` 升序排序 + 总量裁剪（近者优先）+ 落盘。
- `_persist_prompts`：`logs/attached-prompts/<sha8>.md`，append-only、天然去重。
  按**字节**写（`write_bytes`）而不是按文本写：Windows 上 `write_text` 会把 `\n`
  改写成 `\r\n`，文件名就不再等于内容，「内容寻址」成了假的。已存在但字节不同
  （上一版留下的 CRLF 文件 / 被手改过）就重写 —— 否则它永远是一份读不回来的假痕迹。

### `openthree/builtins.py`

- `_audit_module` / `_function_issues` / `_static_audit`：认出提示词（§6）。
- `reload_functions` / `list_functions` / `read_prompts`：新增输出节。

### `openthree/cli_tool.py`

- `run_tools` 无工具分支：有提示词 → 打印 + 退出 0；`--print-prompts`。
- `--print-prompts` 在**解析参数之前**先认，且它是**查询**：文件里没写
  `__system_prompts__` 时打出零字节、退出 0（否则同一句命令会变成「未识别的参数」，
  而那是在说「这个选项不存在」）；求值失败时退 1 并把原因打到 stderr ——
  与注册器同一个判据、同一个结论，不把「写坏了」误诊成「没有公开函数」。
- `--help` 在纯提示词文件上回答「怎么用」，不是把正文当帮助打出来。

### `openthree/runtime.py`

- 启动摘要加一行（§5.4）。

### `README.md` + `workspace.py` 的 `_SYSTEM_PROMPT_SEED`

- 「二」那一节补一段：function 文件可以用 `__system_prompts__` 带说明书；
  判据一句话（§2 的表）。**措辞必须写成「可选」** —— 不说「不写也照常工作」，
  读者会以为这是个新规矩、以为每个文件都该带一段（§0）。

`workspace.py` 本身**不需要**新路径、新文件 —— 提示词活在 function 文件里，
磁盘即真源，没有第二份拷贝。这是这个设计最大的好处。

---

## 8. 测试

守卫不变量优先，功能其次：

**`tests/test_attached_prompts.py`**

- 常量 / 零参函数 / 列表三种形态都能收集；
- 带参数的函数 → `prompt_errors` 有记录，且**该函数仍被注册为工具**；
- 抛异常 → 隔离，其他文件不受影响；
- 单文件与总量截断，且「近者优先」；
- 排序：project 段在 profile 段之前；
- **不变量**：文件里没有 `__system_prompts__` 时，
  `build_system_block(...)` 的输出与改动前**逐字节相等**（这是回归护栏，最重要的一条
  —— 它守的就是 §0 的「不写就是零」）；
- `__system_prompts__` 不出现在 `reg.names()` / `reg.schemas()` / `reg.digest()`。

**`tests/test_prompt_only_file.py`**

- 纯提示词文件：`reload` 后无 `load_error`、`audit_functions` 不报 issue、`--print-prompts` 退出 0。

**端到端**

- `write_function` 写一个带提示词的文件 → `reload_functions()` → 下一步
  `messages[0]["content"]` 含该段与来源标注。

---

## 9. 分阶段

| 阶段 | 内容 | 能独立交付吗 | 不做会怎样 |
|---|---|---|---|
| 〇（不启用） | 什么都不做 | ✅ 这是默认状态 | 一切照旧（§0） |
| 一（内核） | `registry` 收集 + `prompt.build_system_block` + 上限 + 不变量测试 | ✅ 最小可用 | 继续把说明书抄进 `system-prompts.md` |
| 二（可观测） | `reload_functions` / `list_functions` / `read_prompts` / runtime 摘要 / recorder | ✅ | 提示词被喂进 system 却看不见 |
| 三（生态） | 纯提示词文件合规 + `--print-prompts` + `_audit_module` / `_static_audit` | ✅ | 只有你打算写纯提示词文件时才有影响（§6） |
| 四（文档） | README「二」一节 + `prompt.py` 头注释 + 一个示例文件 | ✅ | 别人不知道有这条入口 —— 可选功能最怕这个 |

阶段一到四是**同一件事的几个可选增量**，不是必须一起过完的门：只做阶段一，常量 /
零参函数 / 列表三种写法都能生效，停在这里就是完整交付。

第三阶段与「用法」绑定、不与「功能」绑定：「纯提示词文件被审计判成坏文件」会让人以为
这个功能只能给工具当附属品，而它最好的用法恰恰是**独立承载一份规范**。所以判据是
——**打算写这种文件了，就把第三阶段一起做；不打算写，就别做**，别让一条没人走的路
提前占掉改动面。

---

## 10. 不做的事

- **不做开关**（`--no-attached-prompts` / 配置项 / 环境变量）。「不写那个符号」就是关，
  而且关得更干净：不是运行时跳过，是代码路径根本不进（§0）。多一个开关就多一个
  「为什么他看到的 system 和我看到的不一样」的排查入口，而它换不来任何东西。
- **不做条件注入**（`when=` / `tags=` / 按任务匹配）。要情境化就给返回文字的 function；
  一旦有了条件，system 的字节就取决于「此刻在干什么」，缓存前缀立刻失守。
- **不做热重载**（文件改动即生效）。`reload` 是显式动作，与既有节奏一致。
- **不做覆盖语义**。提示词是并列的说明，不是同名配置项。
- **不写进 `prompts/history/`**。那里的快照按「一」的时间轴存，混进 function 的文本会让
  `restore_system_prompt` 的语义含糊：「恢复上一版」到底恢复谁？所以不进。
- **但必须留下能读回来的痕迹**。`.openthree/` 被 `.gitignore` **整目录排除**，
  模型自造的 function 没有 git 历史；recorder 事件里的 sha256 也不能还原内容。
  所以 `reload` 时按内容寻址落一份 `.openthree/logs/attached-prompts/<sha8>.md`
  （append-only、天然去重、可 diff、可恢复），recorder 事件带 `sha8 + file + chars`。
  这不是可选项：**模型能改自己的 system，就必须能看见自己改过什么** ——
  与「一」的 `snapshot_system_prompt` 是同一个承诺。
- **不进 `digest()` / 工具清单**。它不是工具，模型不能调用它。

---

## 11. 例子

```python
"""pdf_kit —— 抽取 PDF 文本的几把工具，附带用法纪律。"""


def pdf_info(path: str) -> str:
    """先看页数与是不是扫描版。"""
    ...


def pdf_text(path: str) -> str:
    """按页抽文本，页之间用 \\f 分隔。"""
    ...


def __system_prompts__() -> str:
    """给内核的附随说明（不会注册成工具）。"""
    return """
## 用 pdf_kit 时的纪律

- 先 `pdf_info(path)` 看页数与 `scanned`；`scanned=True` 时不要硬抽，
  直接告诉用户需要 OCR —— 硬抽出来是一团乱码，却要花掉几万 token。
- `pdf_text` 的结果按 `\\f` 分页，**别自己删分隔符**，后续按页引用要用它。
"""
```

把它放进 `-profile` 指向的目录，另一个项目 `-profile` 一挂，纪律跟着工具一起到位 ——
而这正是 `system-prompts.md` 做不到的事。

---

## 12. 与既有决策的关系

`profile-functions-proposal.md` §1 说「`system-prompts.md` 不能进 profile」，理由是它写的是
**处境**（“你的工作目录是 X”“可用工具 38 个”），换一具躯体就全错。这条判断仍然成立，
本方案没有推翻它：

- 进不了 profile 的是**躯体描述**；
- 本文放行的是**能力说明书** —— 它描述的是「这份代码怎么用」，换到哪个项目都成立。

所以两份文档不冲突：一个划的是「什么不能共享」，一个补的是「能力自带的说明怎么共享」。
§2 那张表就是两套判据的交汇处 —— 也是以后有人想往 function 里塞提示词时，
唯一需要回答的问题。

### 另一条必须对账的判据：`prompt._profile_line` 的「绝不拼接别人的提示词」

`prompt.py:163-177` 的 docstring 写着：profile「只报来源与数量，**绝不去拼接别人的提示词或记忆**」。
本方案恰恰是拼接 profile 里的提示词 —— 所以它不是「没有冲突」，而是**与一条写在代码里的
判据正面冲突**，必须明说改了谁。

那条判据守的是「**身份不变**」，不是「system 里不许有别人的任何字节」——
反证在同一份文件里：`reg.digest()` 早就把 profile 的工具名与 docstring 首行拼进了 system。
所以真正的不变量是：

> **别人的世界观不进 system；别人的能力说明书可以。**

实施时把该 docstring 改成这条（`_profile_line` 守前半句，`build_attached_block` 走后半句），
保留它「不参与身份」的本意。只改文档不改注释，下一个人会把本方案当 bug 修掉。

而这张表**只有打算写的人才需要回答**：答不出「这是能力说明书」，就不写 ——
不写就是 §0 那个状态，什么都不会变。
