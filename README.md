# openthree

一个**极简的 LLM Agent Harness**：`.openthree/functions/*.py` 里的每个 python 文件
都会变成模型可以调用的工具；模型可以自己写新文件、改自己的提示词、记自己的长期记忆、
管自己的上下文与运行边界——**能力因此可以生长**。

没有框架，没有 DSL，没有注册装饰器。**类型注解是 Schema，docstring 是描述，文件名是工具名。**

## 安装

```bash
pip install openthree           # 从 PyPI 安装
```

从源码安装（开发用）：把本仓库 clone/下载到本地后，在仓库根目录执行 `pip install -e .`。

`json_repair` 已随主依赖自动装上，用于模型返回非法 JSON 参数时自动修复。

在 WSL 里部署（git 跟随更新）：仓库内有 `docs/wsl-setup.md` 说明，
或一键 `bash scripts/wsl-setup.sh`（需先在仓库根目录）。

## 配置

优先级：**环境变量 < 用户级 < 项目级**。

```bash
# 方式 1：环境变量
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.deepseek.com
export OPENTHREE_MODEL=deepseek-v4.1-flash
```

```yaml
# 方式 2：~/.openthree/config.yaml  或  <项目>/.openthree/config.yaml
llm:
  api_key: sk-xxx
  base_url: https://api.deepseek.com
  model: deepseek-v4.1-flash
  max_tokens: 32768
  reasoning_effort: high          # 可选：minimal/low/medium/high；不写 = 请求里不带该字段
  reasoning_content_replay: auto  # 可选：auto/always/never —— 推理模式下 reasoning 的回传策略
  context_window_tokens: 128k   # 上下文窗口，决定自动折叠预算；可写 1M/128k/1000000，缺省 1M
```

## 使用

```bash
openthree "把 utils.py 里的重复代码抽出来"   # 就地初始化 .openthree/，写任务，开跑（单次）
openthree                                   # 不带参数 = 进交互模式（-i 是默认模式）
openthree --no-interactive                  # 不带参数时走单次：沿用当前任务（current-task.json 的干净原文）跑一次
openthree -i                                # 交互模式的显式写法（与不带参数等价）
openthree -i --auto 5                       # 长任务自主推进：计划有未完成项就自动接力，最多 5 轮
openthree --name 小问 "帮我把日志查清楚"      # 给它起个名字（只在它还没名字时作为种子）
openthree --resume 4                        # 显式把对话档案里最近 4 轮读回本会话（默认不读盘）
openthree --turns 8                         # 会话内保留最近 8 轮（默认 4，0 关闭）
openthree --init                            # 只建工作区
openthree --inspect                         # 看一眼自己当前的样子（不调模型）
openthree --functions                       # 列出所有工具
openthree --report                          # 重建 three-report.html
openthree -u                                # 升级模式：把本工作区追平本机已装内核（人确认后才落盘）
openthree -u --check                        # 只扫描、只打印清单就退出（不调模型、不改文件，适合 CI）
openthree -u --dry-run                      # 让模型出升级方案，但不落盘
openthree "这张报错什么意思" --image err.png # 多模态：随任务附一张图片
openthree "对比这两张图" --images a.png,b.png
```

**交互模式是默认模式。** 不带任务参数、不带图片、不开 `--auto`、不 `--resume`，且
stdin 连着终端时，`openthree` 直接进交互模式（等价于 `openthree -i`）—— 不必再记
那个 `-i`。三条边界：

- **stdin 不是终端**（管道 / 重定向 / CI / 脚本，`sys.stdin.isatty()` 为假）时**走单次**：
  磁盘上有当前任务就沿用并跑一次。交互模式需要人来敲字，无人应答的 REPL 只会立刻
  EOF、什么也不做就退出。
- **`--no-interactive`** 强制走单次语义；**显式 `-i`** 则任何时候都进交互。
- 给了任务 / 图片 / `--auto N` / `--resume N` 一律走单次路径。

## 多模态（看图）

openthree 能「看见」图片。图片走 OpenAI 协议的 content parts，**不改 ReAct 循环**：

```bash
openthree "这张报错截图是什么意思" --image ./err.png      # 本地路径
openthree "描述这张图" --image https://x/y.png            # http(s) 链接
openthree --images a.png,b.png "对比这两张图"             # 多张
openthree -i                                             # 交互模式里用 :img a.png 附图
```

三条图片进上下文的路，都通：

1. **用户输入**：`--image` / `:img` → 本轮 user 消息带上图片。
2. **工具返回**：模型调用内置 `view_image(path)`，或任何自造工具返回
   `openthree.ImageResult(text=..., images=[...])` → Agent 补一条带图片的 user 消息。
3. **会话记忆**：图片路径记进 `transcript.jsonl`，**同一会话**的下一轮播种时自动还原
   （单次任务之间不共享记忆；跨进程续读用 `--resume`）。

几条刻意的设计（都是为了让多模态不把系统搞坏）：

- **图片不按 base64 计费**。上下文估算里一张图按固定成本折算，
  否则一张 500KB 的图会让预算瞬间爆掉、触发无谓折叠。
- **日志不留 base64**。写进 `logs/` 的请求里，图片被替换成
  `data:image/png;base64,<省略 N 字节图片>`。
- **本地图归档去重**。本地文件复制进 `.openthree/assets/`（按内容 sha1 命名），
  transcript 记的是那里的路径，所以历史轮次的图片始终能回放。
- **坏图不致命**。单张图读不动只在消息里留一句「图片加载失败」，
  不会中断整轮任务。
- **依赖是可选的**。核心路径不依赖 Pillow —— 大小/类型靠字节头判断。
  只有你的自造工具需要缩放、转码时才装。
- 单张图默认上限 **10MB**，用环境变量 `OPENTHREE_MAX_IMAGE_BYTES` 调整（0 = 不限）。

自造一个「看图」工具的写法：

```python
from openthree import ImageResult

@tool
def screenshot(url: str) -> ImageResult:
    """截一张网页图。

    Args:
        url: 网页地址
    """
    path = _capture(url)                 # 你自己的截图逻辑
    return ImageResult(text=f"已截图 {url}", images=[path])
```

**跑起来不会沉默。** 每一步、每次模型调用、每次工具执行都会在终端留下回声：

```
  小问  ·  自造 3 个工具 · 114 次演化
  工作区 <你的项目>\.openthree

36 个工具（内置 33 / 自造 3）
ReAct 循环开始（每轮 ≤ 200 步 × 最多 10 轮，模型 deepseek-v4.1-flash）
       每 15s 打一次心跳，长任务不会看起来卡死

[step 3/200]
  · 思考中（deepseek-v4.1-flash）…
  · … 等待模型返回，已等待 30s          ← 心跳：证明它还活着，不是卡死了
  · LLM 返回 (31.4s · in=20653 out=412)
    说：内置工具里没有斐波那契，我造一个 function 给自己用。
  · 调用 write_function(name=fib, code=(118 字符))
  · ↳ write_function 完成 (0.0s · 输出 107 字符)

第 1 轮完成：4 步 / 4 工具调用 / 2 次演化
报告 .openthree/three-report.html
```

- Banner 是**身份卡**：左边是智能体自己长出来的名字，右边是它的演化证据。
  名字最初没有 —— 模型想清楚自己是谁时用 `set_identity(name, motto=…)` 起一个
  （写进 `.openthree/identity.json`，下一步起出现在终端、处境与报告里）；
  人类也可以在它还没名字时用 `--name 小问` 种一个，而**不会**覆盖它已有的名字。
- `--heartbeat N`：长等待（模型 / 工具）每隔多少秒打印一次心跳，`0` 关闭，默认 `15`。
- `-q / --quiet`：连进度回声也关掉。

## 自我演化长什么样

模型遇到内置工具搞不定的事，会自己造一个：

```python
# 模型调用 write_function(name="pdf_to_text", code="""...""")
# .openthree/functions/pdf_to_text.py 就此诞生：

from openthree import tool

@tool
def pdf_to_text(path: str, pages: str = "") -> str:
    """把 PDF 转成纯文本。

    Args:
        path: PDF 文件路径
        pages: 页码范围，如 "1-5"，留空表示全部
    """
    ...
```

然后 `reload_functions()` —— 从下一刻起，`pdf_to_text` 就在它的工具列表里了。
整个过程不需要人类改一行代码。

## 每个 function 文件也是一个命令行程序

模型造出来的 `.py` 文件，人和别的 agent 也能直接在命令行里跑 ——
CLI 的参数表与模型看到的工具 schema **同源**（都从签名 + docstring 推导），
不会出现「模型 7 个参数、命令行只有 5 个」。

文件末尾的尾钩让它自足：

```python
def __main__(argv=None) -> int:
    """命令行入口。"""
    from openthree.cli_tool import run_cli
    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
```

```bash
python .openthree/functions/safe_edit.py --help          # 看清它能干什么
python .openthree/functions/safe_edit.py replace_in_file --path a.py --old x --new y
python .openthree/functions/safe_edit.py replace_in_file --path a.py --old x --new y --json
```

入口名必须是 `__main__`：dunder 是「协议 / 入口」命名空间，**永远不会被注册成工具**；
若叫 `main` / `cli`，你就凭空多出一个空参数工具。

**没加尾钩的文件也能跑**，用保底通道（对存量和将来新造的都有用）：

```bash
python -m openthree.tools --list                      # 内置工具一览
python -m openthree.tools read_file --path README.md --limit 20
python -m openthree.tools audit_functions             # 体检本目录所有 function 文件
python -m openthree.tools --file .openthree/functions/safe_edit.py --list
python -m openthree.tools --file .openthree/functions/safe_edit.py \
    replace_in_file --path a.py --old x --new y
```

退出码就是判据：`0` 成功、`1` 函数返回 `[错误] …`、`2` 抛异常。输出恒为 UTF-8
（Windows 控制台默认 GBK，会让按 UTF-8 解码的调用方直接崩）。`--json` 给其他 agent 用：
单行、纯 ASCII，`{"ok":…,"fn":…,"result":…}`，空格分隔的 shell 管道里不会被正文换行打断。

> `openthree/builtins.py` 在包内，顶层有相对导入，所以只能
> `python -m openthree.builtins --help`；`python openthree/builtins.py` 会如实报
> `ImportError`。用户 function（`.openthree/functions/*.py`）才是 `python xxx.py` 直跑。

CLI 下 `recorder` 与 `agent` 都是 `None`，所以 `finish` / `context_*` / `limits_*`
这类依赖 Agent 循环的函数会返回「未就绪」—— 这是划界不是坏了：
演化史记的是智能体的自我改造，人手动改的不该混进去。

## function 从哪来：发现 ≠ 授权

工具是**加载**出来的，而「去哪找」与「哪些被授权」是两件事 —— 分开之后，
「你在哪个目录」决定你能**看见**什么，能力集合只由显式决定改变。

**发现（`lookup_functions`）** 沿项目逐级向上找，再加上用户目录：

```
<本项目>/.openthree/functions        ← 自动加载（d=0）
<父目录>/.openthree/functions        ← 只发现（d=1，再上一层 d=2 …）
~/.openthree/functions               ← 只发现（d=1000000）
```

它**纯读**：不 import、不写盘、不改 `sys.modules`，所以可以随便调。用 ast 静态解析出
每个文件的工具名、签名与 docstring 首行，并标出每条工具此刻的状态（已注册 / 可注册 /
注册后会盖住谁 / 会被更近的盖住 / 同距裁决不了）。裸的 `<dir>/functions/` 一律不认：
在 python 项目里它是常见包名，误扫等于把别人的库当成工具加载。

**授权（`register_function`）** 是显式的一步：把某个绝对路径写进
`.openthree/functions.json` 登记簿，`reload` 时才真正执行它。

```bash
# 模型侧：先看有什么，再决定要不要
lookup_functions(query="pdf")
register_function(path="/home/u/.openthree/functions/pdf_to_text.py", dry_run=True)
register_function(path="/home/u/.openthree/functions/pdf_to_text.py")
unregister_function(path="/home/u/.openthree/functions/pdf_to_text.py")
```

几条边界是刻意这么定的：

- **只收 `.openthree/functions` 目录族内的 `.py`。** 不接受任意路径 —— 那等于给自己开一个
  静默的任意代码入口。`functions.json` 是**数据**不是可信清单：手写一条族外路径，
  `reload` 也会按同一套规矩复核并拒绝加载，记进 `load_errors`。
- **按引用，不复制。** 源文件改好，下次 `reload` 用的就是新内容；登记时记下 `sha256`，
  之后变了就**如实报告**（只报告，不拦截、不回滚）。
- **登记成功 ≠ 工具生效。** 同名按距离裁决。登记一份会顶掉**内置**工具时，回执里有
  ⚠ ——`read_file` / `run_shell` 这些是模型的感知通道，被顶掉就没了。
- **辅助模块按目录隔离。** 两个目录各有 `helper.py` 时，各自 `import helper` 拿到的是
  自己那份；`reload` 会连辅助模块一起重读，不会出现「改了没生效」。

## 长任务：模型自己维护计划，自主推进

`system-prompts.md` 只是**第一稿骨架**，不是枷锁——种子提示词里就写明「你完全可以、
也应该在做事的过程中把它改成更像你的样子」。围绕这个思路，长任务有四件配套的事：

**1) 计划交给自己（`plan.md`）。** 模型用 `write_plan` 维护
`prompts/plan.md`，用 markdown 勾选框逐条推进（`- [ ]` / `- [x]`）。
这个计划**每一步都自动注入上下文**（system 里的「你的计划」一节），
所以即使上下文被折叠、即使换了一轮新的 Agent，进度都在。

```markdown
- [x] 读完 utils.py，列出重复代码
- [ ] 抽出公共函数 read_config()
- [ ] 回归测试
```

**2) 一步走不完就**分轮**——步 / 轮 / 会话三层。** 先把三个词分清：

| 概念 | 含义 | 预算 |
|---|---|---|
| **步（step）** | ReAct 的一次「思 → 行」 | `max_iterations`（每轮） |
| **轮（round）** | 一次完整 ReAct 循环 = 一个 Agent 的一生 | `max_rounds`（整场） |
| **会话（session）** | 进程内的多轮容器（`-i` / `--auto`） | `--turns`（播几轮记忆） |

于是「200 步」是**一轮**的预算，不是整场任务的硬墙：一轮撞到步数上限还没做完，
运行期会**自动开下一轮接着干**（每轮拿一份独立的步预算），整场由 `max_rounds` 兜住成本
（默认 10 轮 ≈ 2000 步）。模型也可以主动 `next_round(summary=…)` 说「这一轮先到这，
剩下的下一轮做」。这条路径**单次任务就生效**，不必先学 `--auto`。**处境里写的
「本场最多 M 轮」就是运行期真正会执行的轮数**（`--auto N` 时 M=N，显式 `--max-rounds`
更低时取小者）—— 只有一份数字：

```bash
openthree "把项目里的重复代码抽干净"
# [step 200/200] …… （本轮没跑完）
# [轮] 第 1 轮未跑完（10 轮预算内）→ 自动开始第 2 轮
# ...
```

**3) 自主接力（`--auto N`）。** 在分轮之上再加一层「看计划办事」：每轮收尾后检查
计划里是否还有 `- [ ]` 未完成项——有就自动接下一轮，最多 N 轮。**未完成项只以
接力期间写过的计划为准**：`plan.md` 是单槽位，磁盘上残留的 `- [ ]` 未必属于这次接力，
不该逼着接力空跑满轮数，所以未完成项只有在接力确实写过计划后才算数（撞步数上限 /
主动 `next_round` 照旧驱动接力）。`--auto N` 直接决定
本场的 `max_rounds`；若同时显式给了 `--max-rounds M`，取二者较小者（两个旋钮说的
是同一件事：整场最多几轮）。轮与轮之间靠三样东西
接续：**会话内**的跨轮记忆、每步重新组装的上下文（system 常量 + 计划/记忆/演化史 + 此刻处境）、
模型自己写进文件的进度。
于是「一个长任务」会自己跑到做完，而不是跑一轮就停。一次 `--auto` 接力 = 一个进程内会话：
接力过程中逐轮连贯，收尾后即弃，不会残留到下一次单次任务。

```bash
openthree -i --auto 8 "把项目里的重复代码抽干净"
# [auto] 计划还有 3 项未完成 → 自动开始第 2 轮
# ...
# [auto] 计划已无未完成项，收工（共 4 轮）。
```

停下的条件：计划已无未完成项 / 某轮出错或被中断 / 达到轮数上限。任一满足即停。
若因**触到轮数上限而仍有活**（计划没勾完 / 撞步数上限），`--auto` 的退出码非 0 ——
CI 与脚本据此判得出「这个长任务其实没做完」，而不必去读报告。

**4) 改提示词可后悔（版本 + 回看 + 回滚）。** `write_system_prompt` 覆写前会把旧稿
自动存进 `prompts/history/`；`list_prompt_versions` 列历史、`read_prompt_version("-5")`
回看最近 5 版（概览）或 `read_prompt_version("1")` 读全文、`restore_system_prompt`
一键回退（回退动作本身也会先存一版现稿）。自省的前提是**可后悔**。
`read_prompts` 还会在开头先给出系统提示词的**章节索引**，一眼看清结构再决定改哪节。

## 上下文：模型自己管

设计哲学是**模型驱动**：提示词能自改（`write_system_prompt`）、能力能自造
（`write_function`）——上下文当然也该是模型手里的一只手，而不是内核单方面的决定。

于是上下文管理有一对「读写工具」，磁盘即状态、每步重读：

- **看得见**：`context_usage` 给出「已用 / 预算 / 占比 / 可折叠范围 / 自动折叠开关 /
  已折次数」。你的**处境**里每步也自动带一行 `上下文：…` 摘要，不必专门去调。
- **改得动**：`context_budget(budget_chars=..., auto_fold=...)` 写下策略到
  `.openthree/context.json`（`0` = 关闭自动折叠）。**改一下，下一步就生效**。
- **折得掉**：`compact_context(start, end, summary)` 把一段 step 折叠成摘要；
  **`summary` 留空就直接丢弃**（早期无用的步骤，最省）。它**立即**生效，
  并如实告诉你省了多少字符。可折范围由 `context_usage` 给出，最后一步（你正在走的）不可折。

内核的**自动折叠是「安全网」而非「主控」**：默认开启，好让第一次跑长任务不至于撞上限；
但它随时可被模型关掉（`auto_fold=false`）或调预算。真正兜底的是**环境变量**
`OPENTHREE_CONTEXT_AUTOFOLD=0`（CLI 里是 `--no-auto-fold`）——模型改不了环境，
所以想「我自己来，内核别插手」时它才是那道硬闸。

```bash
openthree --no-auto-fold "这个任务我自己管上下文"   # 彻底冻结内核兜底折叠
```

## 运行边界：模型自己定

同样的道理再往前推一步：「上下文」管的是**还能装多少**，「运行边界」管的是
**还能走多远、一次生成多长、单条工具结果留多宽**——本质同构，所以也该是模型的一只手。
于是运行边界也有一对「读写工具」，写进 `.openthree/limits.json`、每步重读：

- **看得见**：`limits_usage` 给出「每轮步数上限 / 最多轮数 / `max_tokens` / 工具结果字符
  上限 / `temperature`」，以及每个值**谁定的**（内核默认 / 模型自管 / 人类锁定）。处境里
  每步也自动带一行 `运行边界：…` 摘要。
- **改得动**：`set_limits(max_iterations=…, max_rounds=…, max_tokens=…, tool_result_limit=…, temperature=…)`
  写下边界。**改一下，下一步就生效** —— 一轮的步数不够就调 `max_iterations`、整场轮数不够就调
  `max_rounds`（它们对应「一轮」与「整场」两个层级）；输出被 `max_tokens` 截断就自己调大，
  不必等谁来改配置文件。
- **内核默认只是起点**：`max_iterations=200`、`max_rounds=10`、`max_tokens=32768`、
  `tool_result_limit=4000` 都是**默认值**，不是天花板。模型改了 `limits.json`，
  下一步 `run()` 开头就应用。

与上下文一样，模型绕不过两层：**启动参数显式锁定**（`--max-iterations` / `--max-rounds` /
`--max-tokens` / `--tool-result-limit` / `--temperature` 一旦给出，该旋钮模型就改不动）与**环境硬闸**
`OPENTHREE_LIMITS_LOCK`（非空真值 = 整体冻结，模型怎么写 `limits.json` 都无效）。
「人类把边界收回去了」这件事，对模型是**可读**的（`limits_usage` 会如实标出「显式（锁定）」
或「被环境冻结，改不动」）。

```bash
openthree --max-iterations 500 "这个长任务我允许它多走几步"   # 显式锁定每轮步数上限
openthree --max-rounds 8 "允许它一路续到 8 轮"               # 显式锁定整场轮数上限
OPENTHREE_LIMITS_LOCK=1 openthree "运行边界谁也不许动"        # 整体冻结
```

## 身份：名字是它自己长出来的

同一个模式再延伸一层。CLI 的横幅不印固定文案，而是一张**身份卡**：
左边是这个智能体自己的名字，右边是它的演化证据（自造工具数 · 演化次数）。

名字不是内核常量，最初是**空的**——模型想清楚自己是谁时，用
`set_identity(name, motto=…)` 给自己起一个，写进 `.openthree/identity.json`
（与 `limits.json` / `context.json` 同源：磁盘即状态、每步重读）：

- **它会进自我认知**：处境里每步都有一行 `名字：小问 —— 格言（改名 N 次）`，
  所以模型改名后**下一步自己就知道**；还没名字时如实写「（还没有）」并把命名权交还给它。
- **它进演化史**：每次起名 / 改名都记一条 `evolution` 事件，于是「名字的演化史」与
  「工具的演化史」并列出现在 `three-report.html` 里。
- **它进报告与自省**：`--inspect` 多一行 `智能体 小问`，报告的标题 / hero / footer 也都用这个名字。
- **人类只做种子**：`--name 小问` 只在它**还没有名字**时落盘一次，之后绝不覆盖 ——
  名字归智能体自己。`whoami()` 让它随时能看见自己现在叫什么。

```bash
openthree --name 小问 "帮我把日志查清楚"   # 人类投喂一个初始名字（不覆盖已有名字）
```

## 长任务的健壮性

openthree 的设计不变（仍然是那个极简的 ReAct 循环），但它已经能扛住长任务：

- **工具结果有界入上下文。** 单个工具输出超过 `tool_result_limit`（默认 4000）字符时，
  完整内容落盘到 `logs/tool-output/`，history 只保留预览 + 文件路径。同一份大输出不会被
  每步重发。这个上限也不是死的——模型可用 `set_limits(tool_result_limit=…)` 自己调。
- **一轮跑不完不会「直接死」。** 每轮的步预算是有限的，但一轮撞到上限还没做完时，运行期会
  自动开下一轮（整场由 `max_rounds` 兜住），所以步数上限是**一轮**的节奏，不是整场任务的硬墙。
- **上下文自己能管。** 历史累计超过**预算**时，内核会把最老的若干 step 折叠/丢弃；
  但预算与「要不要内核兜底自动折叠」现在都由**模型自己**决定（`context_budget` 工具写
  `.openthree/context.json`，每步重读、改一下下一步生效；`context_usage` 是它的读数）。
  预算默认按**模型上下文窗口**推导（窗口 × 50% × 2 字符/token）：默认窗口
  **1M token → 预算 100 万字符**。按你的模型调整窗口：`config.yaml` 里写
  `llm.context_window_tokens: 4m`（支持 `1M` / `128k` / `1000000`），或用
  `--context-window` / `--context-budget` 覆盖（显式参数是模型的起点）。
  想让内核**彻底不插手**：`--no-auto-fold` 或环境变量 `OPENTHREE_CONTEXT_AUTOFOLD=0`
  （模型改不了环境，这是真·硬闸）。详见「上下文：模型自己管」一节。
- **LLM 调用会重试。** 网络抖动 / 限流 / 5xx 自动指数退避重试（默认 3 次）；
  参数错误、鉴权失败、4xx 立即返回，不空转。**重试只在 openthree 这一层发生**
  ——构造 SDK 时显式钉死 `max_retries=0`，所以 `retries=3` 就是「最多 4 次调用」，
  不会和 SDK 自带的传输层重试叠加相乘。真正发出的次数会显示在进度行
  （「共 N 次尝试」），并记进 `logs/llm-*.json` 与 `trace.jsonl` 的 `attempts` 字段。
- **超时是分层的。** `llm.timeout`（默认 600s）管 read/write/pool——等模型慢慢
  生成是合理的；`llm.connect_timeout`（默认 5s）单独管「连不上主机」，快速失败
  后交给重试。两层不分开的话，「主机不可达」这一种失败也会干等满 600s。
- **推理模式下 reasoning 会原样回传。** provider 用哪个字段名吐（`reasoning_content` /
  `reasoning` / `reasoning_text`），就用哪个名字放回下一轮历史——改名等于端点认不出来。
  端点要求「每条 assistant 消息都带 `reasoning_content`」时，**没有 reasoning 的那一轮补空串**
  （校验的是键存在，不是内容非空）：漏了这一步，模型某轮不吐 reasoning 会让**下一次**请求 400。
  策略由 `llm.reasoning_content_replay` 决定：`auto`（默认，端点像 DeepSeek 家族且推理开着才补）/
  `always`（代理 URL 判不出厂商时的逃生口）/ `never`；`--inspect` 会显示当前判定。
  **三档管的都只是「占位」**：已经收到的 reasoning 仍照原样回放，所以 `never` 不等于
  「完全不回传」——它只是不再给没有 reasoning 的那一轮补空串。
- **输出被截断可恢复。** `finish_reason=length` 时如实报错，**并要求模型重发**那条消息
  （更精简），最多 2 次——而不是把半截 `tool_calls` 当成正常回复、或白白杀死整场任务。
  连续重发仍截断才失败，并提示模型用 `set_limits(max_tokens=…)` 自己调大生成上限
  （若该旋钮已被启动参数或环境变量锁定，才需要人类改 `config.yaml`）。
- **`run_shell` 跨平台，且不会挂死。** Windows 中文环境（GBK）下输出不乱码；
  命令超时后**连同子孙进程一起强杀**（Windows `taskkill /T`、POSIX `killpg`），
  再给 5s 管道收尾宽限，仍收不回就直接放弃读取返回——所以绝不会因为某个后台
  子进程攥着输出管道而永久卡住。`stdin` 接 `DEVNULL`，交互式命令立刻 EOF 退出。
  `timeout` 被截到 `1~3600` 秒。
- **LLM 调用有上界。** 一次调用最多 `1+retries` 次（默认 4 次）；每次请求
  `timeout` 上限 600s、每次失败后退避 ≤30s，故最坏约 `4×(600+30)≈42 分钟`
  ——**这也会表现为「很久没结果」**，可用
  `config.yaml: llm.timeout / llm.retries / llm.connect_timeout` 调小。
- **`finish` 之后不执行多余的调用。** 同批剩余 tool_calls 一律跳过。
- **多轮记忆只活在会话里。** 每轮的 (用户原文, 结论) 追加进 append-only 的
  `prompts/history/transcript.jsonl` 作档案；但**记忆本身在进程内的会话里**
  ——`-i` / `--auto` 逐轮累积、跨轮可见（`--turns N` 限制轮数，默认 4，`0` 关闭）。
  **单次任务从零开跑，绝不读上一次的对话**；想续读旧档案得用 `--resume N` 显式要求。
  用户的**完整输入**永不丢失（档案只增不改）。
- **长任务自己会往下走。** 计划（`prompts/plan.md`）每一步注入上下文；
  一轮撞到步数上限未做完会**自动续下一轮**（整场由 `max_rounds` 兜住），
  `--auto N` 下还会在**接力期间写过的**计划仍有未完成项时继续接力，
  直到勾完 / 出错 / 触到上限；没做完时退出码非 0。
  改提示词会先存档，能回滚。
- **过程可观测。** 终端有 step / 思考 / 工具 / 心跳的实时回声，磁盘有完整日志——
  「它现在在干什么」和「它曾经干过什么」都有答案。
- **自查（写完先验收，再上岗）。** `write_function` 落盘时先做**纯静态审计**：
  语法、有没有可注册的公开函数、是否**覆盖了内置工具**（那会砍掉你自己的感知通道）、
  有没有高危调用（`os.system`/`eval`/`subprocess`…）。通过后**必须**再
  `reload_functions()` 并用 `list_functions()` 确认它真在清单里——文件写成功 ≠ 工具注册成功。
  想更稳还可先 `verify_tool(name)`：它在**子进程**里真正 import 一次并按注册器的同一路径
  构造工具，把「语法没错但一 import 就炸」提前暴露，且**零污染**（子进程死掉不影响内核）。
  工具重名**不是故障**：同名按距离裁决（近者生效），记进 `overrides`；只有「距离
  解释不了的冲突」（同一来源目录内两个文件同名）才进 `load_errors` —— 分开记账，
  免得把自己的正常覆盖当成异常、又对真故障视而不见。`compact_context` 的区间会先校验，
  折不动就如实告诉你，不会假报成功。
- **看代码有感知带宽。** `search_files`（grep 式，先定位后精读）与
  `code_outline`（ast 大纲，只解析不执行，对大文件安全且快）——先看结构、先搜再读，
  别硬啃整份文件。
- **失败信息一步到位。** 工具参数不匹配时，错误里直接回给你**正确签名**与必填项，
  不必再查一次工具清单。

```bash
python self_test.py          # 端到端自测（桩模型，不联网）
python -m pytest             # 单元测试
```

## 会话记忆：跨轮可见，退出即忘

```bash
openthree               # 不带参数直接进交互模式（= openthree -i，默认模式）
openthree -i            # 等价写法；会话内的轮次互相可见
openthree --resume 4    # 或：显式把档案里最近 4 轮读回，作为会话起点
```

```
交互模式。工作区 <你的项目>\.openthree
输入 :q 退出，:r 重建报告，:i 自省，:img 附图片；会话记忆：最近 4 轮
对话档案：<你的项目>\.openthree\prompts\history\transcript.jsonl（只增不改；记忆只在本会话内）

你 ▸ 我叫小明，请记住
...
你 ▸ 我叫什么？
...        ← 模型看得到「我叫小明，请记住」，答得出「你叫小明」
```

两条线，一条**写**、一条**读**，刻意分开：

- **写（档案，永远只增不改）**：`.openthree/prompts/history/transcript.jsonl`
  每轮把 `(用户原文, openthree 结论)` 追加成一行 JSON，一个字都不删、不改。
  用 JSONL 而非 markdown，是因为用户输入里可能包含任何字符（换行、`### 用户`、
  甚至一行看起来像 `## 2020-01-01T00:00:00` 的文本），JSON 编码保证无损往返。
  `.openthree/prompts/user-prompts.md` 则是**渲染层**（单槽位、会被覆写，含
  `# 当前任务` 头 / `_提交于 …_` 尾 / append 的 `---` 累积块），给人看、模型可改，
  **不当输入读、也不当档案**。机器读的「当前任务」是它的结构化真源
  `.openthree/prompts/current-task.json`（`{text, images, ts}`，`text` 永远是干净原文）；
  两者由 `submit` 一起写。于是「读用户输入」读到的绝不会是文件的存储格式。
- **读（记忆，只在进程内会话）**：每轮都是新 `Agent`，历史不继承；运行期把
  **本会话累积的轮次**以 `user` / `assistant` 消息播种进它的历史——**记忆在消息层，
  不在 system 层**，所以「system 每步重组装」的演化机制毫发无损。
  - **单次任务**（`openthree "任务"`，或 stdin 非终端时不带参数的 `openthree`）：
    没有会话，**从零开跑，绝不读盘** —— 不会莫名背上上一次的对话。
  - **`-i` / `--auto N`**：各开一个进程内会话，逐轮累积、跨轮可见；
    其中 `-i` 正是**不带参数时的默认入口**。
  - **`--resume N`**：唯一的读盘入口，显式把档案里最近 N 轮载入为会话起点。
  - `--turns N` 控制会话内播种多少轮：默认 4，`0` 关闭（回到纯单轮），`-1` 全部。

## 检索与长期记忆：让模型「找得到、记得住」

前一条讲的是**会话内**的轮次记忆（进程内、退出即忘）。这一条讲**跨会话**的两种持久能力：

**检索（`search_files`）—— 感知带宽。** 内置的 grep：给出「文件:行号: 内容」，
自动跳过 `.git` / `node_modules` / `__pycache__` / `logs` 等噪声目录，跳过二进制与
超大文件；而 `.openthree` 里的 `functions/`、`prompts/`、`memory/` **照样能搜**
（那正是 agent 最该翻的地方）。模型不必再硬读整份文件，而是
**先 grep 定位 → 再 `read_file(offset=行号)` 精读**。

```python
search_files("def handle_", path="src", glob="*.py", regex=True, ignore_case=True)
# src/app.py:42: def handle_request(req):
# src/worker.py:17: def handle_task(task):
```

**长期记忆（`remember` 写 / `recall_memory` 读）。** `.openthree/memory/memory.jsonl`
是 append-only 的经验笔记：`remember` 写一条，跨会话、跨上下文折叠都还在。
**最近的记忆每一轮都会自动注入上下文里的「你的长期记忆」一节**（它与计划、演化史
同在一条自维护段里，排在 system 之后 —— 那段是模型自己会改的文件，放在 system
里会让写一次记忆就重算整份工具 schema），要翻更旧的用
`recall_memory(query=...)` 检索（或直接 `search_files` 搜这个 jsonl）。
注入有预算护栏（单条截到 300 字符、整节至多 2000 字符），记忆只是提示，不会挤爆上下文。

```bash
# 模型侧（在 ReAct 循环里自己调用）：
remember("这个项目的测试要用 python -m pytest，别用 pwd", tags="test,坑")
recall_memory(query="pytest")     # → 把写下的经验读回来
```

设计上这是补上「能力可生长」闭环里断掉的一环：**写下的经验，只有被读回才有价值。**
记忆与检索都不含任何硬编码智能——它们只是给模型的手，怎么用由模型决定。

## 安全声明

**openthree 会以你的用户权限执行模型决定的一切操作。** 这不是夸张：

- `run_shell` 用 `shell=True` 执行任意命令，无沙箱、无审批；
  它只保证「超时后一定返回」（整树强杀），**不保证命令不产生副作用**——
  被 kill 前它已经做过的事不会撤销。
- `read_file` / `write_file` 可读写进程权限范围内的任意路径；
- `write_function` 写出的 `.py` 会被直接导入执行；
- `write_system_prompt` 可以改写它自己的提示词。

因此：**在受信任的环境中运行，别把 `api_key` 交给你不信任的模型与任务。**
安全边界目前是「你的操作系统」，不是 openthree。

## 可观测

模型的一切工作都被记录，分门别类：

```
.openthree/logs/
    session-<id>.json   整场会话（元信息 + 全部事件 + 统计）
    sessions/<id>/steps/step-0001.json  逐步拆解（按会话分目录）
    sessions/<id>/llm/llm-0001.json     每次模型调用的请求与响应原文
    events/events.jsonl  追加式事件流，一行一事件
    evolution.jsonl      自我演化史（写了什么 function、改了什么提示词、调了什么上下文策略）
```

```bash
openthree --report        # 或直接双击
.open three-report.html
```

`three-report.html` 是自包含的（无 CDN、无外部资源），离线可看：
任务、每一步的思考、每次工具调用的参数与结果、每次演化前后的完整代码、token 消耗。

## 目录

```
openthree/
├── kernel.py      运行期全局把手（KERNEL）
├── workspace.py   .openthree/ 的读写与初始化
├── tool.py        函数 → Tool（类型注解 + docstring → JSON Schema）
├── registry.py    扫描 functions/*.py → 工具注册表
├── builtins.py    种子 functions（看世界 / 改自己 / 记忆 / 上下文 / 边界 / 收尾）
├── prompt.py      上下文组装（一 + 运行期处境 + 三）
├── llm.py         极薄 LLM 客户端
├── agent.py       ReAct 循环
├── progress.py    进度回声（纯展示层，终端不再沉默）
├── recorder.py    可观测：一切工作记录
├── report.py      three-report.html
├── runtime.py     装配
└── cli.py         命令行
tests/             pytest 单元测试
self_test.py       端到端自测（桩模型）
```

## 设计上的取舍

- **没有编排层。** 控制流由模型决定。硬编码的 `if 用户想做什么` 是智能的敌人。
- **提示词是文件不是常量。** 模型能读到它，所以也能改写它。
- **目录即注册表。** 不引入注册代码、不引入装饰器参数、不引入配置文件。
- **每步重装上下文。** 自我修改要立刻生效，就不能缓存——提示词、工具、**上下文策略**
  都是每步重读磁盘。模型改一下，下一步就是新的自己。
- **上下文也是模型的一只手。** 预算与自动折叠不是内核的特权，而是可读（`context_usage`）、
  可写（`context_budget`）、可折（`compact_context`）的磁盘状态；内核的自动折叠只是
  一张**可被模型关掉**的安全网，环境变量才是它绕不过的硬闸。
- **运行边界也是模型的一只手。** 每轮步数 / 最多轮数 / 生成长度 / 工具结果宽度同样可读
  （`limits_usage`）、可写（`set_limits`）、每步重读；默认值只是起点，启动参数显式锁定与
  环境变量 `OPENTHREE_LIMITS_LOCK` 才是收回去的那两层。「还能走多远」与「还能装多少」
  是同一类问题，所以给它们同一套机制。
- **步与轮分层。** 一次 ReAct「思 → 行」是**步**，一次完整 ReAct 循环是**轮**（每轮各有一份
  步预算）；一轮没跑完就自动开下一轮，整场由轮预算兜住——于是步数上限是**节奏**而非硬墙。
- **记录先落盘再返回。** 进程被 kill 也不丢已发生的思考。
- **进度成回声。** 展示层与决策层分开：Agent 不认识终端，Progress 不认识 ReAct。

## License

MIT
