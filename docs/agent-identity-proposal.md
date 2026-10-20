# 方案：CLI 不再印「道生一…三生万物」，改为展示「演化出的智能体的名字」

> 状态：**已实现**（2026-10-14）。实现见 `docs/` 同目录代码 + `tests/test_identity.py`；
> 内核层 `workspace.read/write_identity` + `builtins.set_identity`/`whoami` + `prompt` 处境注入，
> 展示层 `cli._print_banner`(身份卡)/`runtime` 文案/`report` 标题 + `--name`。
> 差异：未命名时**不**自动触发命名（避免每次运行刷屏），改为处境里给提示 + `whoami` 可读。
> 目标一句话：把那几行仪式性文案，换成**这个智能体自己长出来的名字**——
> 名字不是内核常量，而是智能体在运行中自己命名、自己可改的。

---

## 一、现状：这些文案到底印在哪

| 位置 | 内容 |
|---|---|
| `openthree/cli.py:41-45` `_print_banner()` | `openthree —— 三生万物` / `道生一，一生二，二生三，三生万物` |
| `openthree/runtime.py:341-348`（每轮开头，`first_round` 才印前三行） | `道生一  工作区 …` / `一生二  N 个工具` / `二生三  ReAct 循环开始…` |
| `openthree/runtime.py:411-413`（每轮收尾） | `三生万物 第 N 轮完成：…` |
| `openthree/report.py:68-69 / 244 / 288` | `<h1>三生万物</h1>`、副标题、`<title>`、footer |
| `openthree/workspace.py:50 / 65` | 系统提示词**种子**里的「你是 openthree —— 一个由『道生一…』构成的智能体」 |
| `tests/test_cli_progress.py:52`、`self_test.py:193` | 断言里含「三生万物」 |

**结论**：文案散落在「banner / 每轮头 / 每轮尾 / 报告 / 提示词种子」五处。CLI 只是其中三处。

---

## 二、核心设计：给智能体一张「身份卡」

### 2.1 名字从哪来（优先级从高到低）

1. **人类种子** `--name "小问"`（只在初始化/首写时落盘，之后交还给智能体）
2. **智能体自命名**：`.openthree/identity.json` 里的 `name`（模型用 `set_identity` 写）
3. **兜底**：`openthree`，并把状态记为「未命名」——处境里提示模型「你还没有名字」。

### 2.2 新内置 function：`set_identity`

```python
def set_identity(name: str, motto: str = "") -> str:
    """给自己起名 / 改名。

    Args:
        name: 新名字（1~24 字符，去首尾空白、禁控制字符）
        motto: 可选的自我格言（一句话，会显示在身份卡上）
    """
```

- 落盘 `.openthree/identity.json`：
  ```json
  {"name": "小问", "motto": "先看再用，落盘为证",
   "born_at": "…", "updated_at": "…", "renames": 0}
  ```
  与既有的 `context.json` / `limits.json` **同构**：磁盘即状态、每步重读、改一下下一步生效。
- 每次改名把旧值记一条 `evolution` 事件 → 进 `logs/evolution.jsonl` 与 `three-report.html`。
  **名字的演化史与工具的演化史并列**，符合「三生万物」的本意，而不是把这层意义删掉。
- 可选再加一个只读的 `whoami()`，让模型随时看见自己叫什么。

### 2.3 名字必须「活」进自我认知

只在终端显示一个名字，模型自己是不知道的——那是装饰，不是身份。所以：

- `openthree/prompt.py`（`build_messages` 组装 system 处，约 248-256 行）在 **runtime block** 里注入一行：
  - 有名字：`你的名字：小问 —— 先看再用，落盘为证`
  - 没名字：`你还没有名字。想清楚自己是谁时，用 set_identity 给自己起一个。`
- 这样名字**每步重读、每步注入**：模型改名 → 下一步它自己就知道，与 `limits.json` 的机制一致。
- 同时把 `workspace.py` 的提示词**种子**里那句「你是 openthree —— 一个由『道生一…』构成的智能体」
  改成中性表述（如「你是 openthree 内核上的一个智能体」），命名交给 `set_identity`。

---

## 三、CLI 改成什么样（前后对比）

### 改之前（旧）

```
  openthree —— 三生万物
  道生一，一生二，二生三，三生万物

道生一  工作区 &lt;你的项目&gt;\.openthree
一生二  34 个工具（内置 31 / 自造 3）
二生三  ReAct 循环开始（每轮 ≤ 200 步 × 最多 10 轮，模型 deepseek-v4.1-flash）
       每 15s 打一次心跳，长任务不会看起来卡死
[step 3/200] …
三生万物 第 1 轮完成：4 步 / 4 工具调用 / 2 次演化
报告 .openthree/three-report.html
```

### 现在（已实现）

```
  小问  ·  自造 3 个工具 · 114 次演化
  工作区 &lt;你的项目&gt;\.openthree

34 个工具（内置 31 / 自造 3）
ReAct 循环开始（每轮 ≤ 200 步 × 最多 10 轮，模型 deepseek-v4.1-flash）
       每 15s 打一次心跳，长任务不会看起来卡死
[step 3/200] …
第 1 轮完成：4 步 / 4 工具调用 / 2 次演化
报告 .openthree/three-report.html
```

要点：

- **名字只出现一次，但出现在最醒目的 banner**；过程行去掉「道/一/二/三」标签，变中性（不再有仪式文案）。
- banner 右上角的 **「自造工具数 · 演化次数」** 就是「它是演化出来的」最直接证据——
  名字与演化数据**同框**，一句话说清「这是谁、它长成什么样了」。
- 未命名时显示 `openthree（未命名）`——诚实，而且给 `set_identity` 留了入口。
- `-i` 提示符 `你 ▸` **不改**（那是用户输入，不该换成 agent 名）；可在会话头加一行 `智能体：小问`。
- `-q/--quiet` 行为完全不变（静默只在 quiet 时）。

---

## 四、其余触点

| 位置 | 改动 |
|---|---|
| `runtime.self_inspect()`（`--inspect` / `--init` / `:i`） | 加一行 `智能体  小问（set_identity 维护；改名 3 次）` |
| `report.py` hero / `<title>` / footer | 标题换成名字（可选，但**建议同步**，否则报告里还是旧文案，出现「两套身份」） |
| `README.md:114-127` 示例输出 | 同步成新样例 |
| `tests/test_cli_progress.py:52`、`self_test.py:193` | 断言从 `"三生万物"` 改为按名字/中性文案 |
| 新增 `tests/test_identity.py` | 覆盖：默认名 / set_identity 落盘 / 改名记演化 / 注入 system / `--name` 优先级 |

---

## 五、改动清单（预计 ~200 行，含测试）

| 文件 | 改动 |
|---|---|
| `openthree/workspace.py` | `identity.json` 种子 + `read_identity()` / `write_identity()`；提示词种子去文案 |
| `openthree/builtins.py` | 新增 `set_identity`（+ 可选 `whoami`）并注册进 `BUILTIN_FUNCTIONS` |
| `openthree/prompt.py` | runtime block 注入「你的名字」 |
| `openthree/runtime.py` | 341-348 / 411 换文案；`self_inspect` 加名字行 |
| `openthree/cli.py` | `_print_banner()` → 身份卡；新增 `--name` 参数 |
| `openthree/report.py` | hero / title / footer（可选） |
| `tests/*`、`README.md` | 同步 |

---

## 六、备选方案与取舍

- **A. 纯展示名**：只能 `--name` 人工设、模型改不了。简单，但「只能看不能长」，不符合「**演化出的**名字」。→ 否。
- **B. 名字由演化指纹派生**（如 `hash(functions+prompt)` 前 4 位）：确定、无需模型配合，
  但那是指纹不是名字，读不出「谁」。→ 可作 banner 副标题的补充，不作主方案。
- **C. 保留「道生一…」只在其后补名字**：没解决诉求，文案照旧。→ 否。
- **D. 只改 CLI，报告与提示词不动**：改动最小，但会出现「终端一套身份、报告/脑子里另一套」。→ 不推荐，可分批。

---

## 七、建议落地顺序

1. 内核层：`workspace` 身份读写 + `builtins.set_identity` + `prompt` 注入
2. 展示层：`runtime` / `cli` 文案替换 + banner 身份卡 + `--name`
3. 一致性：`report` / `README` / 测试同步
4. 验收：`pytest` 全绿、`self_test.py` 全绿、手动跑一次 `--init` 与一次真实任务看身份卡

## 八、验收标准

- 终端不再出现「道生一 / 一生二 / 二生三 / 三生万物」。
- banner 显示 `.openthree/identity.json` 里的名字（缺省 `openthree（未命名）`）。
- 模型调用 `set_identity` 后，**下一次运行**（甚至同一会话下一步，因每步重读）banner 即变新名。
- 未命名时模型的处境里能看到「你还没有名字」的提示。
- 现有测试全绿；安静模式不受影响。
