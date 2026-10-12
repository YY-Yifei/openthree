# openthree 项目评审报告

> 评审对象：`/path/to/openthree`（v0.1.0，commit `47f6c8f`）
> 评审时间：2026-10-12
> 评审方式：通读全部源码（14 个模块，2,368 行）+ 运行自测 + 对项目自带真实运行日志（6 场会话 / 110 步 / 110 次模型调用）做量化分析

---

## 0. 结论速览

| 维度 | 评价 |
|---|---|
| 设计哲学 | ★★★★★ 极简、自洽、有辨识度 |
| 可读性 | ★★★★☆ 代码干净，注释到位 |
| 自我演化机制 | ★★★★☆ 真正可跑通，而非概念演示 |
| 鲁棒性 | ★★☆☆☆ 一处失败即全盘终止，无重试 |
| 上下文/成本控制 | ★☆☆☆☆ 无界增长，实测已到 9.5 万 token/次 |
| 安全 | ★★☆☆☆ 无沙箱、无路径约束、无审批 |
| 工程化 | ★★☆☆☆ 无 pytest、无 CI、无 lint 配置 |

**一句话结论**：这是一个**设计完成度远高于工程完成度**的作品。它的核心创意（目录即注册表 + 提示词可自写 + ReAct 即演化）是真成立的，自测 23/23 通过、真实会话也产出了高质量的自造工具 `code_outline.py`。但它的**长任务鲁棒性和上下文经济性存在硬伤**——项目自己那场 51 步的会话，累计烧掉 **333 万 prompt token**，且只要中途一次网络抖动，整场 46 步的工作就全部作废。

---

## 1. 值得肯定之处（先说好话）

这些优点不是客套，是被真实运行记录验证过的：

1. **哲学与实现完全一致。** README 声称「目录即注册表」「类型注解即 Schema」「提示词是文件」。核对 `registry.py`、`tool.py`、`prompt.py`，三者在代码层面确实成立，没有藏任何硬编码智能。`agent.py` 的循环里真的没有一句 `if 用户想干什么`。

2. **自我演化不是噱头。** 项目自带的 `.openthree/functions/code_outline.py` 是模型在真实任务中自己造的工具——187 行、纯 AST 解析、不导入不执行、带行号范围、能配合 `read_file` 精读。质量超过很多人手写的同类工具。这证明「模型自造工具」的闭环是通的。

3. **`tool.py` 的 Schema 推导很扎实。** 支持 `Optional`/`Union`/`List`/`Dict`/`Literal` → enum、`kernel` 参数自动隐藏、docstring 的 `Args:` 段落解析（中英文关键字都认）。这是整个项目最工程化的一段。

4. **记录「先落盘再返回」的契约确实兑现了。** `recorder.record()` 每条事件后立即重写 session json。虽然效率有问题（见 3.5），但抗 kill 的承诺是真的。

5. **报告自包含。** `report.py` 生成的 HTML 零外部依赖、暗色主题、按 kind 分色、时间线 + 演化史 + 长期记忆分栏。可观测性做到了「事后也能看」。

---

## 2. 真实运行数据（评审依据）

从 `.openthree/logs/` 提取（最后一场会话，51 步）：

```
status = done，events = 134，steps = 51
tool_calls = 76，llm_calls = 51，evolutions = 1
tool_time_ms  =  12,766
llm_time_ms   = 356,962      ← 模型耗时是工具耗时的 28 倍
errors = 0
```

单次请求的 prompt token 随步数增长：

| 步骤 | prompt_tokens | 消息数 | 请求体字节 |
|---|---|---|---|
| 1 | 4,325 | 1 | 11 KB |
| 10 | 40,851 | 28 | 116 KB |
| 20 | 66,517 | 55 | 196 KB |
| 30 | 76,786 | 79 | 229 KB |
| 40 | 85,580 | 102 | 259 KB |
| **51** | **94,871** | **126** | **285 KB** |

**全 51 次调用累计 `prompt_tokens = 3,338,219`，`completion_tokens = 30,720`。**

也就是说：**输入输出比 108 : 1**。这个项目的钱几乎全花在「反复重发同一段历史」上。这不是运维问题，是架构问题。

工作区磁盘占用：`logs/llm` **12.8 MB / 51 步**，`three-report.html` **545 KB**（单页内联全部事件）。

---

## 3. 问题清单

### 🔴 P0 — 会直接导致任务失败或成本失控

#### 3.1 上下文无界增长，token 开销随步数呈平方级

- **位置**：`agent.py:34`（`self.history` 只增不减）、`prompt.py:90`（每轮把全部 history 塞进 messages）
- **现象**：见上表。第 51 步单次输入 9.5 万 token。50 步任务累计 334 万 token。
- **后果**：① 成本随任务长度**平方增长**；② 迟早撞上模型上下文上限，任务在第 N 步硬性失败；③ 每轮都要传输几百 KB，延迟也水涨船高。
- **为什么自带的 `compact_context` 救不了**：它是「模型自愿调用」的。真实数据里 51 步只触发了 **0 次**压缩（`evolutions = 1`，那一次还是 `write_function`）。让模型自觉压缩自己的上下文，等于指望它主动省你的钱——设计上就不该依赖这个。

**建议（按性价比排序）**：

1. **窗口化 + 摘要自动触发**：给 `Agent` 加 `context_budget`，在 `build_messages` 前估算 token（`len(content)//2` 够用），超阈值时**自动**把最老的 N 步折叠成摘要，而不是等模型开口。摘要可用一次便宜的 LLM 调用生成，或退化为「工具名 + 首行结果」的机械摘要。
2. **工具结果落盘化**：大输出不塞进 history，只塞前 500 字符 + `[完整输出见 logs/tool-output/xxxx.txt]`，模型需要时用 `read_file` 取。这一条能立竿见影砍掉 60%+ 的 token（见 3.2 实证）。
3. **`compact_context` 从「工具」降级为「内部机制」**，保留对外接口但由 runtime 兜底自动调用。

#### 3.2 工具结果在 history 中完全不截断，且与落盘策略不一致

- **位置**：`agent.py:119-124` 直接 `output` 全量入 history；而 `agent.py:113` 记录事件时却做了 `_clip(output)`（默认 4000）。
- **实证**：末次请求里最大的 6 条 tool 消息：

  ```
  20,353 字符  run_shell
  20,348 字符  run_shell
  20,303 字符  run_shell
  18,831 字符  run_shell
  12,885 字符  run_shell
   9,400 字符  read_file
  ```

  75 条 tool 消息中有 **16 条 > 4000 字符**。这些 2 万字符的 shell 输出（`run_shell` 自身在 `builtins.py:127` 截到 20000）会**永久留在 history 里，每轮重发**。
- **后果**：直接放大了 3.1。同一份 2 万字符的输出被重发了数十次。

**建议**：统一截断口径，`history` 里工具结果也走 `_clip`，并把超长输出落盘 + 给出文件路径。落盘和入上下文用同一套策略，别再两套标准。

#### 3.3 LLM 调用零重试，一次抖动毁掉整场任务

- **位置**：`llm.py:146-155` 捕获异常后返回 error；`agent.py:61-64` 收到 error 立即 `break`，并把 `answer` 设为失败信息。
- **后果**：结合上面那场 51 步、已耗时 6 分钟的会话——**第 47 步若网络超时，前面 46 步、约 300 万 token 全部白烧**。
- **另一个问题**：`finish_reason` 被返回（`llm.py:173`）但从未被使用。若因 `max_tokens` 截断（`finish_reason == "length"`），当前代码会把它当成正常回复继续，而 tool_calls 可能已被截断成非法 JSON。

**建议**：在 `llm.chat` 内加指数退避重试（网络错误 / 429 / 5xx，3~5 次），检查 `finish_reason == "length"` 并给出明确提示或自动续写。

#### 3.4 `run_shell` 在 Windows 上输出乱码

- **位置**：`builtins.py:115-118`，用 `encoding="utf-8"` 强制解码子进程输出。
- **实证**：本机执行 `dir`，返回的 stderr 是 `'head' 不是内部或外部命令...` 之类，**中文全部变成 `ϵͳ�Ҳ���ָ�����ļ���`**。
- **原因**：Windows 中文环境下 `cmd.exe` 默认输出 GBK，用 UTF-8 解码必然乱码。而 README 明确说要在 Windows 上用（安装说明写的就是 `cd /path/to/openthree`）。
- **后果**：模型读到的是一堆乱码，无法判断命令是否成功——这对一个强调「观→思→行」的 Agent 是致命的感知污染。

**建议**：Windows 下用 `encoding=locale.getpreferredencoding()`，或干脆 `text=False` 拿 bytes 自行尝试 utf-8 → gbk → latin-1 逐级解码；也可先跑 `chcp 65001`。

---

### 🟠 P1 — 明显影响可用性/正确性

#### 3.5 每条事件都全量重写 session json（O(n²) 写放大）

- **位置**：`recorder.py:148`，`record()` 末尾无条件调用 `_flush_session()`，而 `_flush_session` 把**全部 events + steps** 序列化后覆盖写。
- **实证**：最后一场会话 session json = **554 KB**，写了 **134 次** 且每次都越来越大 → 累计写入约 **30~40 MB**。51 步的 `llm/` 目录已 12.8 MB。
- **后果**：任务越往后每一步越慢（磁盘 IO 而非模型成了瓶颈之一）；SSD 写入放大。

**建议**：改为节流——每 N 条事件或每步结束时刷一次，`finalize` 时再全量刷一次；或 `events.jsonl` 已经是增量流，session json 只在 `finalize` 时生成完整版即可。

#### 3.6 `three-report.html` 全量重渲染，报告越看越卡

- **位置**：`report.py` 每次 `write_report` 都 `_load_sessions` 读所有会话、内联所有事件的原始输出。
- **实证**：**545 KB** 的 HTML，且会随会话累积线性增长。浏览器打开必然卡顿。
- **建议**：默认只渲染最近若干会话的事件详情，更早的只给摘要；事件详情（`_render_event`）目前是**无条件全量内联**的 `<li>`，可改为外层 `<details>` 折叠，并让 `pre` 的截断上限随会话数下降（如仅最近 3 场保留完整输出）。

#### 3.7 报告的 HTML 拼装缺乏系统性防护（当前不可利用，但脆弱）

- **位置**：`report.py` 大量手写 f-string 拼 HTML。
- **核查结论**：**当前不构成可利用的 XSS**。逐处核对：`_esc()`（`report.py:41`）用的是 `html.escape`，默认 `quote=True`，会同时转义 `<>&"'`；`status`（`:115`）、`kind`（`:178`）、`label`（`:132`）等插入点都过了 `_esc`；`--c:{color}`（`:137`）来自 `KIND_STYLE` 白名单。所以模型/工具产出的任意文本**确实**被转义了。
- **但真正的隐患是模式**：一是未定义 kind 会走 `KIND_STYLE.get(kind, ("#64748b", kind))` 并把 `kind` 拼进 **class 属性**（`:178`），一旦未来某处忘了 `_esc` 就是注入点；二是「把模型写的任意文本内联进 HTML」这个模式随着功能增长极易退化。
- **建议**：定义统一的安全插值函数（如 `_attr()` 对属性值另做白名单），未知 kind 一律归入 `default` 且不拼入 class 名。属于加固，不是救火。

#### 3.8 `compact_context` 的「步骤号」口径可能是错的

- **位置**：`agent.py:215` `_index_steps` 按「assistant 含 tool_calls」从 1 编号；`prompt.py:27` 告诉模型「本轮已是第 {iteration} 步」，而 `iteration` 在 `agent.py:44` 每轮 `+1`（**在 LLM 调用前**）。
- **问题**：模型看到的「第 N 步」与 `compact_context` 实际使用的索引**是两套编号**。它们恰好都从 1 开始、大多数情况能对上，但一旦某轮 assistant 返回了 `content` 而无 `tool_calls`（`agent.py:79` 直接 break），或某轮有多条 tool_call，编号就会错位。模型按它理解的编号去折叠，可能折错区间。
- **另一个问题**：`compact_context` 对 `start/end` 无任何边界校验（`builtins.py:330`），越界时 `_apply_pending_compactions` 静默 `continue`（`agent.py:157`），模型却收到 `[ok] 已折叠`。**报告成功但实际什么也没发生**，这会误导模型以为自己省了 token。
- **建议**：统一编号口径（建议 runtime 维护 `step_no` 并在 prompt 里明确「可折叠的 step 范围」）；折叠失败时返回真实结果而非无条件 `[ok]`。

#### 3.9 `events.jsonl` 跨会话混写，缺 `session_id`

- **位置**：`recorder.py:84` 所有会话共用 `logs/events/events.jsonl`，追加写；而 `Event.to_dict()`（`recorder.py:54`）**不含 `session_id`**。
- **后果**：跑过 6 场会话后，这个文件（**518 KB**）里所有事件混在一起，只有 `seq` 从 1 重新开始——**无法判断某条事件属于哪场会话**，「便于 tail / 流式消费」的设计目标落空。
- **建议**：`recorder` 初始化时写一条带 `session_id` 的 header 事件，或给每条事件加 `session` 字段。

---

### 🟡 P2 — 安全、健壮性与工程化

#### 3.10 安全模型缺失（作为「能自己写代码执行」的 Agent，这点权重很高）

- `run_shell`（`builtins.py:105`）无沙箱、无命令白名单、无审批，`shell=True`。
- `read_file` / `write_file` / `list_dir` 用 `Path.cwd()` 为基准，**无路径约束**，模型可读写项目外任意路径（取决于进程权限）。
- `write_function` 写出的 `.py` 会被 `registry` **直接 `exec_module` 执行**（`registry.py:101`），无 AST 审查、无危险 import 拦截。
- 无「人类确认」环节：高风险工具（shell / 写文件 / 删文件）执行前没有 approval gate。
- **建议**：至少提供 `--sandbox`（限制 cwd 到项目目录）、危险工具清单 + 确认开关、`write_function` 落盘后先 AST 静态检查（禁止 `subprocess`/`os.system` 或仅告警）。README 里应明确「本工具会以你的权限执行任意代码」的安全声明——目前**完全没有**。

#### 3.11 `finish` 之后仍会执行同批剩余 tool_calls

- **位置**：`agent.py:97` `_run_tool_calls` 顺序执行整批，`finish` 只设 `self.finished=True`，不中断循环。
- **后果**：模型一轮里返回 `[finish, run_shell("rm -rf ...")]` 时，后者照样执行。
- **建议**：`finish` 被调用后置位，`_run_tool_calls` 每轮检查并跳过剩余调用。

#### 3.12 工具名冲突静默覆盖，且注释与实现不符

- **位置**：`registry.py:128` `self.tools[t.name] = t` 直接覆盖；模块注释声称「覆盖情况记入 load_errors 供自省」，但代码**并未记录**。
- 用户工具与内置工具同名时同样静默覆盖（`reload` 先放 `builtins` 再放用户工具）。
- **建议**：覆盖时写 `LoadError`，或在 `digest` 里标注被覆盖项。

#### 3.13 工具函数的模块级状态在 reload 时会丢

- `registry._purge_modules`（`registry.py:83`）每次 reload 清空 `sys.modules`。用户函数若用模块级全局缓存（很自然的写法），reload 后即失效。
- `KERNEL.state`（`kernel.py:20`）是官方推荐的草稿纸，但**提示词和文档都没提**这个机制。模型造工具时不知道有它，就会踩坑。

#### 3.14 错误重试/中断的收尾不完整

- `KeyboardInterrupt` 由 `runtime.py:99` 捕获，但此时 history 里最后一条 assistant 的 `tool_calls` 没有对应的 tool 结果——若把 history 持久化续跑，会得到非法消息序列。当前不持久化，所以只是「续跑能力缺失」而非 bug。
- 没有 `--resume`：51 步的会话无法从第 47 步续跑，必须从头再来（成本直接翻倍）。

#### 3.15 工程化缺口

- **无 pytest 测试**：`self_test.py` 是自带 `print` 的脚本（23 个断言全过，质量不错），但没有 `tests/` 目录、无 `pytest` 收集、无 CI 配置。
- **无 lint 配置**：无 `.ruff.toml` / `setup.cfg`，本地也装不了 pyflakes/flake8（未列入依赖）。
- **依赖未锁**：`openai>=1.0` 无上限，`json_repair` 只在 `dev` extras 里但 `agent.py:193` 运行时 try-import——**生产环境缺这个可选依赖时，非法 JSON 参数会静默退化成 `{"value": raw}`**，模型会收到莫名其妙的参数错误。
- **`pyproject.toml` 未配置打包 `self_test.py` / `.openthree` 种子**，`--init` 才能铺工作区，但没有模板资源清单。
- **无 CHANGELOG**（`_commit_msg.txt` 是临时文件，`.gitignore` 也未忽略它）。
- `pyproject.toml` 里 `requires-python = ">=3.10"`，但代码用到 `X | Y` 语法？——实际全程用了 `from __future__ import annotations` + `Optional`，3.10 兼容，**这点没问题**，值得肯定。

#### 3.16 细节

- `runtime.py:115` 报告生成失败被 `except Exception: pass` 静默吞掉——可观测层自身的错误也成了不可观测。
- `cli.py:97` 判断 `rt.ws.user_prompt_file.exists()`，但 `Runtime.__init__` 已经 `init()` 过、这个文件**必然存在**，所以这个「没有任务」的分支几乎永远不走，用户会直接拿种子文案「（等待用户输入……）」去跑一次真实的 LLM 调用。
- `agent.py:89` 的 `if not self.finished and self.answer is None` 与循环内的 `break` 组合，逻辑能对但绕，建议简化。
- `llm.py:177 _redact` 只删顶层 `api_key`，但 payload 里本来就没有 api_key（它在 client 里），这个函数实际是**安全剧场**。
- `workspace.py:192 submit` 的 `append` 会无限追加到同一个 user-prompts.md，多轮交互后这个文件会变成一大坨历史任务，没有归档机制。

---

## 4. 架构层面的两点观察

1. **「提示词可自写」缺少版本与回滚。** `write_system_prompt`（`builtins.py:258`）直接覆写，只在 `evolution.jsonl` 留日志。模型若把自己提示词改坏，**没有任何自动回滚**，恢复只能靠 git。建议每次覆写前将旧版本存入 `prompts/history/system-<ts>.md`，并保留 `write_system_prompt(..., restore=True)` 之类的回退入口——「自省」的前提是「可后悔」。

2. **演化产物的质量没有闭环验证。** `write_function` 落盘即完事，没有「写出后先干跑一次语法/冒烟检查」的步骤。当前靠模型自觉 `reload_functions` 看结果，但 `write_function` 返回的 `[ok]` 只代表**文件写成功**，不代表代码能 import。建议 `write_function` 内部先 `ast.parse` + 试加载，把错误提前到写入时返回。

---

## 5. 改进路线图（建议顺序）

**第一周（止血，成本与稳定性）**
1. 工具结果入 history 前统一截断 + 超长落盘（P0-3.2）
2. LLM 调用指数退避重试 + `finish_reason` 处理（P0-3.3）
3. `run_shell` 编码修复（P0-3.4）
4. session json 节流落盘（P1-3.5）

**第二周（上下文治理）**
5. 自动上下文预算 + 窗口化摘要（P0-3.1）
6. 统一 step 编号、折叠失败如实返回（P1-3.8）
7. events.jsonl 加 session_id（P1-3.9）

**第三周（安全与工程）**
8. `--sandbox` / 危险工具确认 / 安全声明（P2-3.10）
9. `finish` 后中止剩余工具调用（P2-3.11）
10. 引入 pytest + CI + ruff 配置（P2-3.15）

**第四周（体验）**
11. 报告分页渲染（P1-3.6）、XSS 收敛（P1-3.7）
12. `--resume` 断点续跑（P2-3.14）
13. 提示词版本历史 + 回滚（架构-4.1）

---

## 6. 附：可直接落地的两个最小补丁

**补丁 A — 工具结果入 history 统一截断 + 落盘（改 `agent.py`）**

```python
# _run_tool_calls 内，替换 history.append 那一段
def _spill(self, name: str, call_id: str, output: str) -> str:
    """超长工具输出落盘，history 只留预览 + 路径。"""
    if len(output) <= 4000:
        return output
    p = self.ws.logs_dir / "tool-output"
    p.mkdir(parents=True, exist_ok=True)
    fp = p / f"{call_id}.txt"
    fp.write_text(output, encoding="utf-8")
    return output[:4000] + f"\n... [已截断，完整输出共 {len(output)} 字符见 {fp}]"

self.history.append({
    "role": "tool",
    "tool_call_id": call_id,
    "name": name,
    "content": self._spill(name, call_id, output) if output else (error or "[无输出]"),
})
```

**补丁 B — LLM 重试（改 `llm.py` 的 `chat`）**

```python
import time as _time

last_exc = None
for attempt in range(4):                      # 1 + 3 次重试
    try:
        resp = client.chat.completions.create(**payload)
        break
    except Exception as exc:
        last_exc = exc
        name = type(exc).__name__
        retryable = any(k in name.lower() for k in
                        ("timeout", "connection", "ratelimit", "internalserver"))
        if not retryable or attempt == 3:
            elapsed = (_time.time() - started) * 1000
            return {"message": {"role": "assistant",
                                "content": f"[LLM 调用失败] {name}: {exc}"},
                    "usage": {}, "elapsed_ms": elapsed,
                    "raw_request": _redact(payload),
                    "raw_response": {"error": f"{name}: {exc}"},
                    "error": f"{name}: {exc}"}
        _time.sleep(2 ** attempt)             # 1s / 2s / 4s 退避
```

---

## 7. 总评

openthree 的问题**几乎都不是「写错了」，而是「还没写到」**。它的骨架是干净的、创意是成立的、真实产出是拿得出手的。真正拦住它走向「能长时间自主干活」的，是三件事：

1. **上下文经济学**——不解决，任务长度一上去成本和失败率就双爆；
2. **失败恢复**——不解决，agent 越自主，代价越大；
3. **安全边界**——不解决，它越能干，你越不敢放手。

好在，这三件事都有成熟解法，且不破坏它「极简」的哲学。补齐它们，openthree 会从「一个漂亮的概念验证」变成「一个真的能托付长任务的 harness」。

---

*本报告基于源码静态审查 + `self_test.py`（23/23 通过）+ 项目自带 6 场真实会话日志的数据分析。*
