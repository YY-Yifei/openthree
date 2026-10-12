# openthree

> 道生一，一生二，二生三，三生万物。

一个**极简的 LLM Agent Harness**：`.openthree/functions/*.py` 里的每个 python 文件
都会变成模型可以调用的工具；模型可以自己写新文件、改自己的提示词——
**能力因此可以生长**。

没有框架，没有 DSL，没有注册装饰器。**类型注解是 Schema，docstring 是描述，文件名是工具名。**

## 安装

```bash
pip install openthree
# 或从源码
git clone <repo> && cd openthree && pip install -e .
```

可选：`pip install json_repair`（模型返回非法 JSON 参数时自动修复）。

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
  max_tokens: 8192
  context_window_tokens: 128k   # 上下文窗口，决定自动折叠预算；可写 1M/128k/1000000，缺省 1M
```

## 使用

```bash
openthree "把 utils.py 里的重复代码抽出来"   # 就地初始化 .openthree/，写任务，开跑（单次）
openthree                                   # 沿用上次的 user-prompts.md 继续（单次）
openthree -i                                # 交互模式（进程内会话，多轮记忆）
openthree -i --auto 5                       # 长任务自主推进：计划未完成就自动接力，最多 5 轮
openthree --resume 4                        # 显式把对话档案里最近 4 轮读回本会话（默认不读盘）
openthree --turns 8                         # 会话内保留最近 8 轮（默认 4，0 关闭）
openthree --init                            # 只建工作区
openthree --inspect                         # 看一眼自己当前的样子（不调模型）
openthree --functions                       # 列出所有工具
openthree --report                          # 重建 three-report.html
openthree "这张报错什么意思" --image err.png # 多模态：随任务附一张图片
openthree "对比这两张图" --images a.png,b.png
```

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
道生一  工作区 <你的项目>\.openthree
一生二  16 个工具（内置 16 / 自造 0）
二生三  ReAct 循环开始（上限 200 步，模型 deepseek-v4.1-flash）
       每 15s 打一次心跳，长任务不会看起来卡死

[step 3/200]
  · 思考中（deepseek-v4.1-flash）…
  · … 等待模型返回，已等待 30s          ← 心跳：证明它还活着，不是卡死了
  · LLM 返回 (31.4s · in=20653 out=412)
    说：内置工具里没有斐波那契，我造一个 function 给自己用。
  · 调用 write_function(name=fib, code=(118 字符))
  · ↳ write_function 完成 (0.0s · 输出 107 字符)

三生万物 完成：4 步 / 4 工具调用 / 2 次演化
报告 .openthree/three-report.html
```

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

## 长任务：模型自己维护计划，自主推进

`system-prompts.md` 只是**第一稿骨架**，不是枷锁——种子提示词里就写明「你完全可以、
也应该在做事的过程中把它改成更像你的样子」。围绕这个思路，长任务有三件配套的事：

**1) 计划交给自己（`plan.md`）。** 模型用 `write_plan` 维护
`prompts/plan.md`，用 markdown 勾选框逐条推进（`- [ ]` / `- [x]`）。
这个计划**每一轮都自动注入上下文**（system 里的「你的计划」一节），
所以即使上下文被折叠、即使换了一轮新的 Agent，进度都在。

```markdown
- [x] 读完 utils.py，列出重复代码
- [ ] 抽出公共函数 read_config()
- [ ] 回归测试
```

**2) 自主多轮推进（`--auto N`）。** 一轮跑完后，运行期检查计划里是否还有
`- [ ]` 未完成项——有就自动开启下一轮，最多接 N 轮。轮与轮之间靠三样东西接续：
**会话内**的跨轮记忆、每轮重新组装的 system（含计划）、模型自己写进文件的进度。
于是「一个长任务」会自己跑到做完，而不是跑一轮就停。一次 `--auto` 接力 = 一个
进程内会话：接力过程中逐轮连贯，收尾后即弃，不会残留到下一次单次任务。

```bash
openthree -i --auto 8 "把项目里的重复代码抽干净"
# [auto] 计划还有 3 项未完成 → 自动开始第 2 轮
# ...
# [auto] 计划已无未完成项，收工（共 4 轮）。
```

停下的条件：计划已无未完成项 / 某轮出错或被中断 / 达到轮数上限。任一满足即停。

**3) 改提示词可后悔（版本 + 回滚）。** `write_system_prompt` 覆写前会把旧稿
自动存进 `prompts/versions/`；`list_prompt_versions` 看历史，`restore_system_prompt`
一键回退（回退动作本身也会先存一版现稿）。自省的前提是**可后悔**。

## 长任务的健壮性

openthree 的设计不变（仍然是那个极简的 ReAct 循环），但它已经能扛住长任务：

- **工具结果有界入上下文。** 单个工具输出超过 4000 字符时，完整内容落盘到
  `logs/tool-output/`，history 只保留预览 + 文件路径。同一份大输出不会被每轮重发。
- **上下文自动折叠。** 历史累计超过**预算**时，runtime 自动把最老的若干 step 折叠成
  摘要——不依赖模型自觉调用 `compact_context`。预算不再写死：默认按**模型上下文窗口**
  推导（窗口 × 50% × 2 字符/token）。默认窗口 **1M token → 预算 100 万字符**。
  按你的模型调整窗口：`config.yaml` 里写 `llm.context_window_tokens: 4m`
  （支持 `1M` / `128k` / `1000000`），或用 `--context-window` / `--context-budget` 覆盖；
  `--context-budget 0` 关闭自动折叠。启动时会打印「窗口 → 预算」一行。
- **LLM 调用会重试。** 网络抖动 / 限流 / 5xx 自动指数退避重试（默认 3 次）；
  参数错误、鉴权失败立即返回，不空转。
- **输出被截断会被告知。** `finish_reason=length` 时如实报错，而不是把半截
  `tool_calls` 当成正常回复。
- **`run_shell` 跨平台，且不会挂死。** Windows 中文环境（GBK）下的输出不再乱码；
  命令超时后**连同子孙进程一起强杀**（Windows `taskkill /T`、POSIX `killpg`），
  再给 5s 管道收尾宽限，仍收不回就直接放弃读取返回——所以绝不会因为某个后台
  子进程攥着输出管道而永久卡住。`stdin` 接 `DEVNULL`，交互式命令立刻 EOF 退出。
  `timeout` 被截到 `1~3600` 秒。
- **LLM 调用有上界。** 单次请求默认 600s 超时、最多重试 3 次、退避上限 30s，
  最坏约 3×(600+30)≈32 分钟——**这也会表现为「很久没结果」**，
  可用 `config.yaml: llm.timeout / llm.retries` 调小。
- **`finish` 之后不再执行多余的调用。** 同批剩余 tool_calls 一律跳过。
- **多轮记忆只活在会话里。** 每轮的 (用户原文, 结论) 追加进 append-only 的
  `prompts/history/transcript.jsonl` 作档案；但**记忆本身在进程内的会话里**
  ——`-i` / `--auto` 逐轮累积、跨轮可见（`--turns N` 限制轮数，默认 4，`0` 关闭）。
  **单次任务从零开跑，绝不读上一次的对话**；想续读旧档案得用 `--resume N` 显式要求。
  用户的**完整输入**永不丢失（档案只增不改）。
- **长任务自己会往下走。** 计划（`prompts/plan.md`）每轮注入上下文；
  `--auto N` 下若计划仍有未完成项，收尾后自动接力下一轮，直到勾完 / 出错 /
  触到上限。改提示词会先存档，能回滚。
- **过程可观测。** 终端有 step / 思考 / 工具 / 心跳的实时回声，磁盘有完整日志——
  「它现在在干什么」和「它曾经干过什么」都有答案。
- **自查**：`write_function` 落盘前先做 AST 语法检查；工具重名会记入 `load_errors`；
  `compact_context` 的区间会先校验，折不动就如实告诉你，不会假报成功。

```bash
python self_test.py          # 端到端自测（桩模型，不联网）
python -m pytest             # 单元测试
```

## 记忆：会话之内跨轮，磁盘之上只读不写

```bash
openthree -i            # 开一个会话；会话内的轮次互相可见
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
  `.openthree/prompts/user-prompts.md` 则是「单槽位」，只装当前任务、会被覆写，
  它是模型能自己改写的工作台，不当档案。
- **读（记忆，只在进程内会话）**：每轮都是新 `Agent`，历史不继承；运行期把
  **本会话累积的轮次**以 `user` / `assistant` 消息播种进它的历史——**记忆在消息层，
  不在 system 层**，所以「system 每轮重组装」的演化机制毫发无损。
  - **单次任务**（`openthree "任务"`）：没有会话，**从零开跑，绝不读盘** ——
    不会莫名背上上一次的对话。
  - **`-i` / `--auto N`**：各开一个进程内会话，逐轮累积、跨轮可见。
  - **`--resume N`**：唯一的读盘入口，显式把档案里最近 N 轮载入为会话起点。
  - `--turns N` 控制会话内播种多少轮：默认 4，`0` 关闭（回到纯单轮），`-1` 全部。

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
    steps/step-0001.json 逐步拆解
    events/events.jsonl  追加式事件流，一行一事件
    llm/llm-0001.json    每次模型调用的请求与响应原文
    evolution.jsonl      自我演化史（写了什么 function、改了什么提示词）
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
├── builtins.py    种子 functions（看世界 / 改自己 / 收尾）
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
- **每轮重装上下文。** 自我修改要立刻生效，就不能缓存。
- **记录先落盘再返回。** 进程被 kill 也不丢已发生的思考。
- **进度成回声。** 展示层与决策层分开：Agent 不认识终端，Progress 不认识 ReAct。

## License

MIT
