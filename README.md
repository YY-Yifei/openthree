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
```

## 使用

```bash
openthree "把 utils.py 里的重复代码抽出来"   # 就地初始化 .openthree/，写任务，开跑
openthree                                   # 沿用上次的 user-prompts.md 继续
openthree -i                                # 交互模式（多轮对话）
openthree --turns 8                         # 多轮时保留最近 8 轮（默认 4，0 关闭）
openthree --init                            # 只建工作区
openthree --inspect                         # 看一眼自己当前的样子（不调模型）
openthree --functions                       # 列出所有工具
openthree --report                          # 重建 three-report.html
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

## 长任务的健壮性

openthree 的设计不变（仍然是那个极简的 ReAct 循环），但它已经能扛住长任务：

- **工具结果有界入上下文。** 单个工具输出超过 4000 字符时，完整内容落盘到
  `logs/tool-output/`，history 只保留预览 + 文件路径。同一份大输出不会被每轮重发。
- **上下文自动折叠。** 历史累计超过预算（默认 12 万字符）时，runtime 自动把最老的
  若干 step 折叠成摘要——不依赖模型自觉调用 `compact_context`。
- **LLM 调用会重试。** 网络抖动 / 限流 / 5xx 自动指数退避重试（默认 3 次）；
  参数错误、鉴权失败立即返回，不空转。
- **输出被截断会被告知。** `finish_reason=length` 时如实报错，而不是把半截
  `tool_calls` 当成正常回复。
- **`run_shell` 跨平台。** Windows 中文环境（GBK）下的输出不再乱码。
- **`finish` 之后不再执行多余的调用。** 同批剩余 tool_calls 一律跳过。
- **多轮对话不会失忆。** 每轮的 (用户原文, 结论) 追加进 append-only 的
  `prompts/history/transcript.jsonl`；下一轮开跑前读回最近 N 轮播种进上下文
  （`--turns N`，默认 4，`0` 关闭，`-1` 全部）。用户的**完整输入**永不丢失。
- **过程可观测。** 终端有 step / 思考 / 工具 / 心跳的实时回声，磁盘有完整日志——
  「它现在在干什么」和「它曾经干过什么」都有答案。
- **自查**：`write_function` 落盘前先做 AST 语法检查；工具重名会记入 `load_errors`；
  `compact_context` 的区间会先校验，折不动就如实告诉你，不会假报成功。

```bash
python self_test.py          # 端到端自测（桩模型，不联网）
python -m pytest             # 单元测试
```

## 多轮对话：用户的完整输入不会被丢掉

```bash
openthree -i
```

```
交互模式。工作区 <你的项目>\.openthree
输入 :q 退出，:r 重建报告，:i 自省；跨轮记忆：最近 4 轮
对话原文：<你的项目>\.openthree\prompts\history\transcript.jsonl

你 ▸ 我叫小明，请记住
...
你 ▸ 我叫什么？
...        ← 模型看得到「我叫小明，请记住」，答得出「你叫小明」
```

设计上只有一句话：**用户的原始输入 append 落盘，永不覆写。**

- `.openthree/prompts/user-prompts.md` 是「单槽位」，永远只装当前任务，会被下一轮覆写；
  它是模型能自己改写的工作台，不适合当档案。
- `.openthree/prompts/history/transcript.jsonl` 是 append-only 的档案：
  每轮把 `(用户原文, openthree 结论)` 追加成一行 JSON，一个字都不删、不改。
  用 JSONL 而非 markdown，是因为用户输入里可能包含任何字符（换行、`### 用户`、
  甚至一行看起来像 `## 2020-01-01T00:00:00` 的文本），JSON 编码保证无损往返。
- 每轮都是新的 `Agent`，历史不继承；但开跑前会从 transcript 读回最近 N 轮，
  以 `user` / `assistant` 消息播种进新 Agent 的历史——**记忆在消息层，不在 system 层**，
  所以「system 每轮重组装」的演化机制毫发无损。
- `--turns N` 控制播种多少轮：默认 4，`0` 关闭（回到纯单轮），`-1` 全部。

## 安全声明

**openthree 会以你的用户权限执行模型决定的一切操作。** 这不是夸张：

- `run_shell` 用 `shell=True` 执行任意命令，无沙箱、无审批；
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
