# openthree

> **三生万物**

```
道生一，一生二，二生三，三生万物。
```

一个极简主义的 LLM Agent Harness。没有工作流引擎、没有插件市场、没有编排 DSL。
只有模型本身，以及让它自己长出一切的三层结构。

---

## 它是什么

| | 是什么 | 在哪 |
|---|---|---|
| **道** | 模型本身。**唯一的智能来源** | 你的 LLM |
| **一** | 系统提示词，模型的自我认知 | `.openthree/prompts/system-prompts.md` |
| **二** | functions，模型的手 | `.openthree/functions/*.py` |
| **三** | ReAct 循环：观 → 思 → 行 → 观 | runtime |
| **万物** | 模型自己写出的新能力 | 又回到「二」 |

极简到什么程度？

- **写一个工具 = 写一个 python 函数。** 类型注解就是 JSON Schema，docstring 就是工具描述。
- **注册工具 = 存一个 `.py` 文件。** 目录即注册表。
- **不需要重启。** 模型调 `reload_functions`，新工具立即上手。
- **提示词是文件，模型可以自己改。** 改完下一轮即刻生效——这是它的自省。

## 三层内置律

1. **道生一、一生二、二生三、三生万物** —— 一切能力从模型而来，从循环中长出万物。
2. **`.openthree/functions/` 下的 python 文件会被注册为 functions。**
3. **`.openthree/prompts/system-prompts.md` 是系统提示词，`.openthree/prompts/user-prompts.md` 是任务。**

## 安装

```bash
cd /path/to/openthree
pip install -e .
```

配置模型（任选一层即可，后者覆盖前者）：

```bash
# 方式 1：环境变量
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.deepseek.com
export OPENTHREE_MODEL=deepseek-chat
```

```yaml
# 方式 2：~/.openthree/config.yaml  或  <项目>/.openthree/config.yaml
llm:
  api_key: sk-xxx
  base_url: https://api.deepseek.com
  model: deepseek-chat
  max_tokens: 8192
```

## 使用

```bash
openthree "把 utils.py 里的重复代码抽出来"   # 就地初始化 .openthree/，写任务，开跑
openthree                                   # 沿用上次的 user-prompts.md 继续
openthree -i                                # 交互模式
openthree --init                            # 只建工作区
openthree --inspect                         # 看一眼自己当前的样子（不调模型）
openthree --functions                       # 列出所有工具
openthree --report                          # 重建 three-report.html
```

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
├── recorder.py    可观测：一切工作记录
├── report.py      three-report.html
├── runtime.py     装配
└── cli.py         命令行
```

## 设计上的取舍

- **没有编排层。** 控制流由模型决定。硬编码的 `if 用户想做什么` 是智能的敌人。
- **提示词是文件不是常量。** 模型能读到它，所以也能改写它。
- **目录即注册表。** 不引入注册代码、不引入装饰器参数、不引入配置文件。
- **每轮重装上下文。** 自我修改要立刻生效，就不能缓存。
- **记录先落盘再返回。** 进程被 kill 也不丢已发生的思考。

## License

MIT
