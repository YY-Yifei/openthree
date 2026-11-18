# A2A「真实停止」：让 `tasks/cancel` 真的停手

> 状态：**已实现**（阶段 1+2+3；819 条测试全绿）· 起草 2026-11-17 · 落地 2026-11-18
> 场景：外部调用方要能真正停下正在跑的任务 · 自评审：`docs/a2a-real-cancel-plan-self-review.md`
> 落地记录见 §10。

## 0. 结论先行

| 问题 | 答案 |
|---|---|
| 现在为什么停不了 | `_rpc_cancel` 只做一件事：`_finish(task, "canceled", …)`（`a2a.py:1783-1796`）。ReAct 循环里没有任何中断点，桥的工作线程照跑，结果撞上终态检查被丢弃（`a2a.py:1900`） |
| 「真实停止」的判据是什么 | 不是「状态变 canceled」，而是：**此后不再有新的 LLM 请求发出、不再有新的工具被调用**。这条可以数日志验证（`logs/<session>.json` 的 `llm` / `tool` 事件计数不再增长） |
| 要改哪几层 | 内核（`openthree/cancel.py` 新增，`agent.py` / `llm.py` / `runtime.py` / `recorder.py` 各改若干行）**+ 频道层**（`a2a.py` 一处 + `bridge.py` 一处）。`prompt.py` / `workspace.py` 零改动 |
| Python 能杀线程吗 | **不能**。`Thread.kill` 不存在；`PyThreadState_SetAsyncExc` 只在字节码边界生效，阻塞在 `socket.read` / `subprocess.wait` 里的线程压根收不到，还会留下没释放的锁。所以只能「让工作线程自己退出」 |
| 最坏停止延迟 | 模型在思考：**毫秒级**（关 socket）。工具里：**该工具自己的 timeout**。自造 function 死循环：**无上界**（诚实说明，见 §8） |
| 立刻能接新消息吗 | 能 —— 前提是 `rt.run()` 真的返回了。桥是串行的，`_handle` 一返回就取下一条（`bridge.py:138-148`）。取消的全部意义就是把 `rt.run()` 尽快放回来 |
| 会破坏现有契约吗 | 不破坏。`InboundMessage` 加一个默认 `None` 的字段；`Runtime.run` / `Agent.__init__` 加默认 `None` 的参数；LLM 传输层用全局 socket 注册表，**`LLM.chat` 签名不变**（`tests/` 里十几个 `_StubLLM` 因此零改动） |
| 最大的风险 | 把「取消了」当成「停住了」。取消是**请求**，不是既成事实 —— 所以 §2 要求所有对外文案都带上「停在第几步 / 是否确认停下」 |

## 1. 现状：cancel 只作废，不停手

七条事实（行号已对着源码核过）：

| # | 事实 | 位置 |
|---|---|---|
| 1 | cancel 的完整实现是：查 task → 查终态 → `_finish(canceled)`。没有别的 | `a2a.py:1783-1796` |
| 2 | 它自己在注释里承认：「只是**不再等**，不是停手 —— ReAct 每一步都在等模型返回，没有中断点」 | `a2a.py:1793-1794` |
| 3 | ReAct 主循环是 `while not self.finished and self.iteration < self.max_iterations`，循环体里没有任何「该不该继续」的检查 | `agent.py:640-746` |
| 4 | 一步里最长的两段阻塞：`self.llm.chat(...)`（默认 `timeout=600s`，`retries=3` ⇒ 最坏 4 次 => 40 分钟） | `agent.py:672-674`；`llm.py:72-79`、`:493-508` |
| 5 | 第二长：`tool.func(**call_args)`，纯阻塞调用，超时只由工具自己给 | `agent.py:1023-1024` |
| 6 | 桥只有**一条**工作线程，串行跑；`_handle` 返回才取下一条 | `bridge.py:69`、`:138-148` |
| 7 | 终态之后 `_finish` 一律忽略（这条不变量要保留），所以桥跑完的答案被静默丢弃 | `a2a.py:1897-1902` |

于是现状的准确描述是：**cancel 把这张卷子作废了，但考生还在写。** 卷子上的字（artifact）没了，token 照烧，工具照跑（可能还在写工作区）。

## 2. 目标：「真实停止」的可验证判据

一句话：**取消之后，`logs/<session>.json` 里的 `llm` 与 `tool` 事件不再增长。**

三条推论，写死在实现里：

1. **停止是「不再开始」**，不是「半途掐断」。已经在跑的 LLM 请求会被关掉（那是我们自己的 socket，可以关）；已经在跑的工具调用**等它自己返回**（Python 没有安全的中断手段，§5.1）。
2. **「已请求」与「已确认停下」必须分开说**。`tasks/cancel` 的响应里要能看出是哪一种（§4.6）。
3. **停下之后，这一轮的产物要如实入档**：`recorder` 落 `status="canceled"`，`answer` 写明停在第几步、最后一条动作是什么。报告里不能出现「done」。

## 3. 停止延迟上界（诚实表）

| 取消发生时，工作线程正在 | 上界 | 靠什么 |
|---|---|---|
| 等下一个检查点（步首、工具之间） | 毫秒级 | §4.2 的检查点 |
| 等 LLM 返回 | **毫秒级** | §4.3 关 socket，`read()` 立刻抛 |
| LLM 重试退避的 `sleep` 里 | 毫秒级 | §4.4 退避可被打断 |
| `run_shell` 之类的内置长工具里（阶段 1） | **`limit + 15s`，最长 3615s —— 等于没治** | §4.8.1 |
| 同上（阶段 2 落地后） | **毫秒级 + 清树耗时**（`proc.kill()` 唤醒；清树自带 10s 上限，实测 <100ms） | §4.8.3 killer 唤醒 + 后台 `taskkill /F /T` |
| 自造 function 的纯 Python 死循环里 | **无上界** | 没有任何手段（§5.1、§8） |
| 消息还在队列里、压根没开跑 | 立即（不执行） | §4.7 |

这张表要原样进 A2A 的响应文案与 README —— 不写清楚，「真实停止」就会变成一句新的谎话。

## 4. 设计

### 4.1 取消令牌挂在**消息**上，不是挂在 Runtime 上

新增 `openthree/cancel.py`：

```python
class Cancelled(Exception):
    """这一轮被调用方取消 —— 不是错误，是「奉命收工」。"""

class CancelToken:
    """一次运行的取消请求。幂等、线程安全、不可重置（一轮一个）。"""
    def __init__(self, source: str = ""): ...
    @property
    def requested(self) -> bool: ...
    def request(self, reason: str = "") -> bool:
        """第一次调用返回 True（并关掉在飞的 socket），之后返回 False。"""
    def check(self) -> None:
        """已请求 → raise Cancelled(reason)。检查点只调这一句。"""
```

**为什么挂在消息上**：每个 A2A task 在 `_rpc_send` 里本来就有一个 `run_id`（`a2a.py:1701`、`:1720`），取消天然是「针对某一次运行」。令牌跟着 `InboundMessage` 走：

- `base.py` 的 `InboundMessage` 加 `cancel: Any = None`（默认 `None` ⇒ 微信 / 钉钉 / 既有测试零改动）。
- `a2a._rpc_send` 建一个 `CancelToken(task_id=…)`，同时放进 `msg.cancel` 与 `task["_cancel"]`（在复位 task 的**同一把锁**里 —— 和 `_run` 的所有权切换同源，`a2a.py:1707-1743`）。
- `a2a._rpc_cancel` 变成：找到**正在跑的那一个**令牌 → `request("调用方取消")` + 原有的 `_finish(canceled)`。频道不需要认识 bridge、Runtime、Agent —— 它只是置了自己 task 上的一个事件。**「频道只负责收与发」这条判据仍然成立。**
- `bridge._handle` 把 `msg.cancel` 交给 `rt.run(..., cancel=…)`。

follow up（同一 taskId 追问）时令牌**换新的**：旧 run 的取消不该杀掉新 run，反之亦然 —— 与 `run_id` 的所有权规则完全同源。

**但这里有一个必须处理的竞态**：follow up 到达时，上一轮可能还在桥的队里排队、或正在跑（桥是串行的）。`task["_cancel"]` 在 `_rpc_send` 复位时就被换成新的了，于是随后到达的 `tasks/cancel` 取消的是**还没开跑的那一轮**，而真正在跑的那一轮毫发无伤 —— 调用方以为停了，其实没停。

修法：令牌带 `task_id`，`cancel.py` 维护「进程内**正在跑**的那一个」：

```python
_CURRENT: Optional[CancelToken] = None     # 桥单线程串行 ⇒ 至多一个

def bind(tok): ...        # Agent.run() 起跑前绑定（同时写线程本地）
def unbind(tok): ...      # Runtime._run_round 的 finally；只解绑自己（防误清别人的）
def running_of(task_id: str) -> Optional[CancelToken]: ...
def current() -> Optional[CancelToken]: ...   # 线程本地，给 llm 层用
```

`_rpc_cancel` 先问 `running_of(task_id)`：命中就用它（真停止），不命中才用 `task["_cancel"]`（那一轮还没开跑 ⇒ 到它起跑时检查点会自己退出，见 §4.7）。

`current()` 是**线程本地**的 —— 这正是 §4.3/§4.4 能让 `LLM.chat` / `post_json` 签名**一个字节都不动**的原因：`Agent.run()` 把令牌绑到当前线程，`llm.py` 里 `cancel.current()` 就地取到它，不必一路作参数传下去。

### 4.2 检查点：六个位置

| # | 位置 | 语义 |
|---|---|---|
| 1 | `Agent.run()` 步首，`tick()` 之后（`agent.py:645` 后） | 不再发起新的一步 |
| 2 | `_run_tool_calls` 每个 call 之前（`agent.py:842` 循环头） | 一批 5 个工具，取消后剩下的一次都不调 |
| 3 | `LLM.chat` 返回后、写 history 之前（`agent.py:675` 后） | 认领 §4.4 带回来的取消信号 |
| 4 | `Agent._execute` 函数入口（`agent.py:1016`） | 与 #2 冗余但便宜，且覆盖 `finish` 之后的其他路径 |
| 5 | `Runtime.run()` 的轮循环头（`runtime.py:293`） | 取消后**不再自动续轮** |
| 6 | `run_auto()` 的轮循环头（`runtime.py:546`） | 同上 |

#5 / #6 很容易漏：一轮被取消后如果 `step_limit_hit` 为真，现有代码会**自动开下一轮**（`runtime.py:313-321`）—— 那等于「取消了，重启了，继续烧」。这是本方案最容易写漏的一处。

`Agent` 拿不到令牌时不改变任何行为：`cancel=None` ⇒ 走一个永不触发的 `NullToken`。单次任务（`openthree "…"`）、`-i`、所有既有测试因此零改动。

### 4.3 让 LLM 的阻塞 `read` 可中断（关键的一步）

只加检查点解决不了最长的那个阻塞：`llm.chat` 可能干等 600 秒。**必须能把在飞的 socket 关掉。**

做法：`cancel.py` 里一张进程级的 socket 注册表。

```python
_LIVE: Dict[Any, CancelToken] = {}   # 在飞 socket → 它的 owner 令牌
_ABORTED: set = set()                # 被我们自己关掉的（区分「取消」与「网络故障」）
_LOCK = threading.Lock()

def watch(sock, owner) -> None: ...   # owner=None 表示「不归任何令牌管」
def unwatch(sock) -> None: ...        # 同时 discards _ABORTED（否则长时间运行会缓慢涨）
def abort_owned_by(tok) -> int:       # 只 shutdown(SHUT_RDWR) **这个令牌的** socket
def was_aborted(sock) -> bool: ...
```

**只关自己的**：`watch` 时记住 owner，`abort_owned_by(tok)` 只掐匹配的那些。第一稿写的是 `abort_all()` —— 那一版会连**别的运行**的 socket 一起关掉（`-i` 控制台在跑、A2A 取消一个排队中的任务 ⇒ 控制台的 LLM 请求被掐断），是个 P0，已在自评审 §3 记下。

`llm.post_json` 里三行挂上去：

```python
conn.connect()
if conn.sock is not None:
    cancel.watch(conn.sock)        # ← 新增
    conn.sock.settimeout(timeout)
...
except (OSError, http.client.HTTPException) as exc:
    if conn.sock is not None and cancel.was_aborted(conn.sock):
        raise cancel.Cancelled("调用方要求停止")     # ← 新增：不是网络故障
    raise TransportError(f"{type(exc).__name__}: {exc}") from exc
finally:
    if conn.sock is not None:
        cancel.unwatch(conn.sock)  # ← 新增
    conn.close()
```

`CancelToken.request()` 里调 `abort_owned_by(self)`。**为什么注册表要带 owner**：`-i` 控制台的 ReAct 与 A2A 的 ReAct 共用 `_run_lock`，任一时刻进程内最多一个**在飞**的 LLM 请求，但**令牌**可以不止一个（排队中的消息各有一个）。不带 owner 的 `abort_all()` 会让「取消一个还没开跑的任务」掐断别人正在等的响应。这条前提（单线程串行）要写进 `cancel.py` 的模块 docstring —— 将来若真要并发跑两个 ReAct，它就是第一个要重审的地方。

**零签名变化**是刻意的：`LLM.chat` / `post_json` 的签名一个字节不动，`tests/` 里十几个 `_StubLLM`、`FakeConn`、`FakeSock` 因此不需要跟着改。

### 4.4 重试必须被取消打断（否则会再发一次）

`post_json` 抛的是 `Cancelled`，而 `LLM.chat` 的 `except Exception` 会去看 `_retryable(exc)`（`llm.py:593-606`）。好消息是 `Cancelled` 既不是 `TransportError`、名字里也没有 `timeout` / `connection` 这些 marker —— `_retryable` 会返回 `False`，不会重试。但这靠的是「碰巧」，不能靠碰巧：

- `LLM.chat` 的 catch 里**先**判 `isinstance(exc, Cancelled)` ⇒ 直接返回 `{"error": …, "error_kind": "cancelled"}`，连 `_retryable` 都不问。
- 重试退避的 `time.sleep(_backoff(attempt))`（`llm.py:606`）换成可被打断的等待：`cancelled.wait(_backoff(attempt))`，返回 True 就立刻收工。

`Agent` 侧（检查点 #3）看的正是 `result["error_kind"] == "cancelled"`，而不是去匹配错误文本 —— 与 `_is_truncation`（`agent.py:1254-1265`）同一套判据风格。

### 4.5 终态：`canceled` 进白名单 + 三处断链

| 位置 | 现状 | 改成 |
|---|---|---|
| `recorder._TERMINAL_STATUSES` | `("done","unfinished","interrupted","error")` | 加 `"canceled"`（`recorder.py:43`） |
| `Runtime.run` 的轮循环 | `if status in ("error","interrupted"): break` | 加 `"canceled"`（`runtime.py:311`） |
| `run_auto` 的轮循环 | 同上 | 加 `"canceled"`（`runtime.py:571`） |
| `Runtime._run_round` | `status = "done" if finished else "unfinished"` | `except Cancelled: status = "canceled"`，`answer` 写明停在第几步（`runtime.py:433-447`） |
| `cli.py` 退出码 | `status == "done"` 才 0 | 不动 —— canceled 自动非 0，正确 |

**两处 `except` 的顺序是判据，不是风格**（漏了就等于没做）：

```python
# runtime._run_round（runtime.py:433-447）
try:
    answer = self.agent.run()
except KeyboardInterrupt:            # 保留在最前：Ctrl-C 有它自己的语义
    ...
except Cancelled:                    # ← 必须新增，且必须在 except Exception 之前
    answer, status = self.agent.cancel_summary(), "canceled"
except Exception:                    # 现有那条：会把 Cancelled 记成 "error"
    ...
```

```python
# agent._execute（agent.py:1023-1033）
try:
    out = tool.func(**call_args)
except TypeError: ...
except Cancelled:                    # ← 必须新增，且必须在 except Exception 之前
    raise
except Exception:                    # 现有那条：会把 Cancelled 变成一条工具错误字符串
    ...
```

两处漏一处都会让「取消」降级成「这一轮出错了」：前者让 CI 判成故障、报告里写「运行时异常」；
后者更隐蔽 —— 它把取消写成一条 tool 结果，ReAct **继续跑到下一步**，等于取消被工具吞了。

`Agent` 里加 `self.cancelled: bool`，`run()` 的返回值前面拼一句如实的结论：
`"[已按调用方要求停止] 停在第 7 步（最后动作：run_shell）。已发出 6 次 LLM 请求、11 次工具调用。"`

### 4.6 A2A 侧：`tasks/cancel` 的返回语义

现状：立刻 `canceled` + 一句「模型可能仍会把这一步跑完」。

改成三段，顺序不能反：

1. `task["_cancel"].request(...)` —— **先**发信号，再等；
2. 等一个**有界**的 `cancel_grace`（默认 **2s** ＝ §9 决策 1；可配，`0` = 不等）：`task["_done"].wait(cancel_grace)`；
   **这一步必须在 `_tasks.lock` 之外**。现有 `_rpc_cancel` 的形状就是「锁里查状态 → 锁外 `_finish`」（`a2a.py:1790-1795`）——
   把 `wait()` 挪进 `with self._tasks.lock:` 就是自锁：桥收工时要调 `_finish`，而 `_finish` 第一件事就是拿那把锁，
   于是「取消」持锁等「它 set」、它等「取消放锁」，两边都等到天荒地老。锁里只取 `Event` 对象（`a2a.py:1910-1913`
   有同一个先例：`done = task["_done"]` 在锁里取，`done.set()` 在锁外调）；
3. 按等到什么写终态：
   - 工作线程在这段时间里收工了 ⇒ 它已经调过 `send_text`，终态由它落（见下条），这里只 `_view` 返回；
   - 没等到 ⇒ `_finish(task, "canceled", "已请求停止；当前这一步（`run_shell`）会跑完它自己的超时，之后不再继续")`，并附 §3 上界表里对应的那一行。

**`send_text` 必须跟着改**，否则第一步就是错的：桥返回的答案非空，现在会被记成 `completed`（`a2a.py:1883-1884`），于是「已按调用方要求停止」这句话躺在一个 `completed` 的 task 里。判据是**带外标记**（与 `notice` 同源）：`msg.cancel` 已请求 ⇒ `_finish(task, "canceled", body)`，而不是 `completed`。

**为什么不做「终态之后补写 artifact」**：那要在 `_finish` 的「终态一律忽略」（`a2a.py:1900`）上开一条窄缝，而这条不变量是防撕裂的最后一道闸（`_view` 的注释解释了为什么）。用一次有界的 2 秒等待换掉那条窄缝，划算得多 —— 而且 2 秒之后调用方拿到的仍然是诚实的「已请求，未确认」。

**竞态要如实处理**：取消到达之前模型正好答完 ⇒ `_done` 已 set、状态是 `completed`。这时 `_rpc_cancel` 不该把它改写成 `canceled`（那才是撒谎：它确实答完了）。等到之后先看状态，已是 `completed` / `failed` 就原样返回。这也正好是「取消是请求不是命令」的一个可观察后果。

### 4.7 队列里还没开跑的消息

`_handle` 开头（`bridge.py:150` 之后）先看一眼 `msg.cancel`：已请求 → 回一句「这条消息在开跑前已被取消，未执行」，不进 ReAct。别让「取消」在排队场景下变成「取消了个寂寞，它还是烧了 200 步」。

### 4.8 已经卡死的命令：把「等它自己超时」变成「可以杀」

**这是本方案最要紧的一节。** 前三节的前提都是「工作线程还能走到下一个检查点」——
而一条卡死的命令恰好破坏了那个前提。

#### 4.8.1 现状：阶段 1 对正在跑的命令**完全无效**

`run_shell`（`builtins.py:556`）等子进程只有一行：

```python
out_b, err_b = proc.communicate(timeout=limit)      # builtins.py:591
```

`limit` 被夹在 `1..3600`（`builtins.py:580`、`_SHELL_TIMEOUT_MAX` 见 `:502`）。这一行
**阻塞到子进程退出或超时为止，期间没有任何检查点**。所以：

- 检查点 #2（工具之间）、#4（`_execute` 入口）都只在**调工具之前**生效；
- 一旦进了这条命令，取消请求只能干等 —— 等到它自己的 timeout（可能是 3600 秒），
  再加强杀宽限 15 秒（`builtins.py:596`、`_KILL_GRACE` 见 `:501`）。

**结论要原样写进 §3 与 README：阶段 1 的取消，对「已经卡死的命令」上界是
`limit + 15s`，等于没治。这不是实现缺陷，是那 20 行没有覆盖到。**

#### 4.8.2 判据：叫不醒线程，但可以拆掉它在等的东西

阻塞线程不能被「叫醒」，但它等的资源能被**从外边拆掉**：
`socket` 用 `shutdown(SHUT_RDWR)`（§4.3 已做），子进程用 `taskkill /F /T`
（`_kill_tree`，`builtins.py:528`，内部 `taskkill` 自带 `timeout=10`，`:536`）。

所以治法是：**把「一次等到底」拆成「短切片轮询 + 令牌随时可杀」**。

#### 4.8.3 `run_shell` 的改法（约 25 行，**不加轮询**）

第一稿写的是「0.2s 分片轮询」，那是没验证前的推断。实测后可以更省：
**另一个线程 `proc.kill()` 就能把阻塞中的 `communicate()` 唤醒。**

```
.openthree/tmp/probe_kill_wakes.py：communicate(timeout=30) 阻塞着，
1.0s 时另一线程调 proc.kill() ⇒ communicate 在 1.000s 返回，rc=1，
kill() 自身 0.0000s，已经吐出的 'start' 完整保留。
```

也就是说 `builtins.py:591` 那行 `communicate(timeout=limit)` **一个字节都不用改**，
只在它前面挂一个 killer。

**但 killer 里的顺序被实测纠正过一次**（`tests/test_cancel.py::
test_run_shell_can_be_killed_while_blocked` 就是这条纠正的守卫）：

- 第一版是「先 `proc.kill()` 唤醒 → 再 `_kill_tree` 清树」。它**不行**：
  `shell=True` 起的是 `cmd.exe`，`python` / `pytest` 是**孙进程**。`proc.kill()`
  只死 cmd，孙进程攥着继承来的 stdout/stderr 继续跑，`communicate()` 仍要等它
  自己结束（实测 30s 的命令一秒都没提前返回）；而此时再调 `_kill_tree()`，
  它首行的 `poll()` 判定「早已退出」，直接返回 `already-exited` —— 整棵树没杀。
- 正确顺序是**一次杀整棵树**：`_kill_tree(proc)` 自己做「`taskkill /F /T` →
  失败再 `proc.kill()`」的兜底，它同时结束 cmd 与孙进程，管道随之 EOF。

```python
tok = cancel.current()                       # 线程本地，§4.1 / §4.3
tree: List[str] = []
fired: List[bool] = []
def _killer():
    fired.append(True)
    tree.append(_kill_tree(proc))            # 整树；内部已含 proc.kill() 兜底
hook = tok.attach_killer(_killer) if tok is not None else None
try:
    out_b, err_b = proc.communicate(timeout=limit)   # 这行原样不动
finally:
    if tok is not None and hook is not None:
        tok.detach_killer(hook)
if fired:                                    # 只有 killer 真动过手才敢说「已取消」
    hook.done.wait(2.0)                      # 有界地等清树结果，不猜
    parts.append(f"[已取消] …（进程树：{tree[0] if tree else '清理仍在后台进行'}）")
```

四个要点：

- **`proc.kill()` 只管直接子进程**（Windows 上就是 `cmd.exe`），孙进程要 `taskkill /F /T`
  才死。所以 killer 是「先 kill 唤醒 → 再后台清树」。**真正该担心的不是「取消不返回」，
  而是「取消了但命令还在后台跑」** —— 清树这一步不能省。
- 返回前给清树线程一个 2s 的 grace 并 join，结果如实写进工具返回值：
  `已取消（进程树已清理）` / `已取消（清理仍在后台进行）`。不写清，调用方会以为它真停了。
- 取消比 attach 更早到达也无妨：`proc.kill()` 对已死进程是空操作，`_kill_tree` 首行就
  `poll()` 判过（`builtins.py:530`），`communicate` 立刻返回。
- 若孙进程**脱离**了进程组并攥着管道（`start /b` 那类），`communicate` 仍要等满 `limit`
  —— 落进既有的兜底（`builtins.py:597-607`）。这是本方案承认的一小块残留。

**为什么最终不用轮询**：轮询会给 `run_shell` 多一条热路径分支，而它是内核里最热的工具之一；
killer 唤醒则是**零新增热路径**（`communicate` 那行原样不动）。判据是「不写就是零」优于
「运行时跳过」。轮询实测（`.openthree/tmp/probe_communicate.py`：9 片 / 1.75s / 三段输出
完整）留作**备选**：只有当某个平台上「kill 唤不醒 communicate」时才启用它。

**killer 不能同步跑在取消请求里**：`_kill_tree` 走 `taskkill /F /T`，自带 `timeout=10`
（`builtins.py:536`）。若 `CancelToken.request()` 直接调它，`tasks/cancel` 的 HTTP 响应
就要等最多 10 秒才回 —— 而调用方要的正是「立刻」。修法：killer **先 `proc.kill()`**
（毫秒级，直接子进程立刻死），再把 `taskkill /F /T` 清树交给一个**后台 daemon 线程**。
代价是 `_drain` 可能撞上「孙进程攥着管道」，落进 `builtins.py:597-607` 已有的兜底
（放弃读取、报「输出管道未能收尾」）—— 那是既有行为，不是新风险；**取消时输出了不完整，
比晚 10 秒才回「已取消」好**。

**令牌怎么进来而不改签名**：`cancel.current()` 是线程本地的，`Agent._execute`
（`agent.py:1024`）调 `tool.func` 之前令牌已经绑好（§4.1）——`run_shell` 里就地取到，
**工具签名与 schema 一个字节不动**，§5.4 的判据因此没有被破坏。

**效果**：`run_shell` 的停止上界从 `limit + 15s`（最长 3615s）降到
**毫秒级**（`proc.kill()` 唤醒阻塞中的 `communicate`）+ 清树耗时（`taskkill` 实测通常
<100ms，函数自带 10s 上限；超过 2s grace 就在返回值里写「清理仍在后台进行」）。

**覆盖谁**：`ocr review`、`pytest`、`python -m build`、`git`、`pip`… 我干的**长活全部**
走 `run_shell`。这就是这一节价值最大的原因 —— 它把「大部分卡死」一次性收进几十毫秒。

#### 4.8.4 任意自造 function：契约，不是参数

不走到 `run_shell` 的自造工具（自己开 socket、自己 `urlopen`、自己 `join`）要能被停，
只能靠**工具作者主动交出它的可杀资源**：

```python
from openthree import cancel
tok = cancel.current()
if tok is not None:
    tok.on_cancel(lambda: sock.close())    # 取消时拆掉我在等的资源
```

这条要写进「造 function 的约定」（系统提示词那一节）与 `tool_timeout_guide()`：
**会长时间阻塞的工具，应当注册它的可杀资源**。不注册的工具，取消对它的上界是
「它自己结束」—— 这是**契约**，不是缺陷。`run_shell`（§4.8.3）就是范例。

#### 4.8.5 无覆盖的残留：只有硬退出

纯 Python 死循环、C 扩展里的长调用、无 `timeout` 的 `urlopen` —— 三者都**不可杀**
（§5.1）。剩下的唯一手段是把**整个进程**杀掉（§6 阶段 3）。

链条是闭的：桥只有**一条**工作线程（`bridge.py:138-148`），一条无上界的工具会把它扣住
⇒ 队列里的新消息**永远排不上** ⇒ 取消得再准也没用。所以阶段 3 不是装饰，
它是「无上界卡死」这一格唯一的出口（§9 决策 3）。

`cancel_hard_grace` **默认开启（30 秒）**，`0` 才关 —— 这是人类在落地时的裁定
（§9 决策 3 是默认关；材料变了，判断跟着变）：

- JEV 那 0.77 的前提是「不假定外部守护存在」。而 **A2A 常驻部署就是有守护**的形态：
  服务被拉起来才叫常驻，重启是它的正常动作，不是事故。
- 不开启的代价落在**服务本身**：桥只有一条工作线程，卡在死循环里的那个任务把队列
  彻底堵死 —— 此后**任何**新消息都排不上。那不是「一个任务停不下来」，是
  「这个服务从此不接活」。
- 所以判据翻过来了：**「服务能接活」比「进程不重启」重要**。默认值只对
  「不读文档也安全」负责这一层仍然成立 —— 硬退出是 `os._exit(137)`，它不写半截
  状态、不等 flush，代价是「这一轮的工作区可能停在半截」，这一点在 §8 里写明。

阶段 1+2 已把实际会卡住的长活（`run_shell` 一族：`ocr review` / `pytest` /
`build` / `pip`）收进毫秒级，剩下的无上界残留只有自造 function 里的纯 Python
死循环 —— 它是这一格里唯一的出口，而不是日常路径。

#### 4.8.6 还要一条观测：卡住了得说得清

`_execute` 记下 `tool_started_at`；`tasks/cancel` 的 artifact 在停止过程超过
`cancel_grace` 时写明「卡在 `<工具名>` 已 N 秒，正在尝试强杀（手段：taskkill）」。
否则调用方分不清「真的停了」与「还在卡着」—— 而上界表里那一行本来就承认它会卡。

## 5. 不做的事（以及为什么）

### 5.1 不杀线程

Python 没有安全的 `Thread.kill`。`ctypes.pythonapi.PyThreadState_SetAsyncExc` 能往目标线程注入异常，但它**只在字节码边界生效**：线程阻塞在 `socket.read()` 或 `subprocess.wait()` 里时收不到；等它回到字节码层，注入点可能正在某个 `with` 块中间 —— 锁不释放、文件写一半、工作区处于撕裂态。**宁可慢一点地停，也不要停在一个半截状态上。**

代价是诚实的：卡在纯 Python 死循环里的自造 function 停不下来。§3 与 README 都要写明。

### 5.2 不把 ReAct 搬到子进程

那是唯一能 `SIGKILL` 任意工具的手段，但它要求工作区、会话、`KERNEL.state`、附件槽、报告全部跨进程 —— 等于重写 `runtime.py` 的数据流。当前所有 A2A 复杂度都建立在「同进程、同线程、磁盘即真源」上。**收益（停住死循环工具）与代价（整个内核的结构）不成比例**，留作 §6 阶段 3 之前的否决项。

### 5.3 不做「工具超时就丢结果」

把工具扔进子线程、超时就放弃等待，能让 ReAct 立刻前进 —— 但那个线程仍在写工作区，与下一轮并发写同一批文件。内核不知道一个工具是不是幂等、是不是只读。**用一个偶发的、难以复现的数据损坏换一点延迟，不划算。**

### 5.4 不给所有工具加 `cancel` 参数

会污染每一个工具签名与 schema（`tool.build_tool` 按注解生成参数表，`cancel` 会出现在模型的工具描述里）。阶段 2 的做法是**线程本地**：`cancel.current()` 在工具内部就地取令牌（§4.8.3），签名一个字节不动，连 `needs_kernel` 都不必加。

## 6. 分阶段落地

| 阶段 | 内容 | 工作量 | 停下来的是什么 |
|---|---|---|---|
| **1（必须）** | `cancel.py` + 六个检查点 + socket 中断 + 三处终态 + A2A 令牌与文案 | 7 个文件约 200 行 + 10 条测试 | 模型思考中的 600s、步首、轮间、队列中 |
| **2（必须）** | `run_shell`（`builtins.py:556`）挂 `attach_killer`：取消时 `proc.kill()` 唤醒阻塞中的 `communicate`（那行原样不动），后台清进程树 | 1 个文件约 25 行 + 2 条测试 | **已经卡死的命令**（`ocr review`、`build`、`pytest` 全走它）—— 从 3615s 降到毫秒级 |
| **3（A2A 下必须）** | `cancel_hard_grace`：N 秒内没停下就 `os._exit(137)`，交给守护重启；artifact 先落「卡在哪个工具」 | 2 个文件约 40 行 | 整个进程。**这是「停机器」不是「停一轮」**；**默认开（30s）**，`cancel_hard_grace: 0` 关掉 —— 见 §9 决策 3 与本轮人类裁定 |

## 7. 验收（可跑的测试清单）

全部用现有的 stub LLM 模式，不碰网络：

1. `test_cancel_stops_before_next_step`：`_StubLLM` 每次返回 tool_call；第 2 步后置令牌 ⇒ `llm` 事件恰好 2 条、`tool` 事件 2 条、`status == "canceled"`。
2. `test_cancel_skips_remaining_tool_calls_in_a_batch`：一步 3 个 tool_call，跑完第 1 个时置令牌 ⇒ 只有 1 条 `tool` 事件。
3. `test_cancel_aborts_inflight_llm_request`：`FakeConn` 的 `getresponse()` 阻塞直到 socket 被 shutdown ⇒ `chat()` 在 <1s 内返回、`error_kind == "cancelled"`、`attempts == 1`（**证明没重试**）。
4. `test_cancel_does_not_open_next_round`：首轮 `step_limit_hit=True` 且已取消 ⇒ `rounds_run == 1`。
5. `test_canceled_is_a_terminal_status_in_recorder`：session json 的 `status == "canceled"`（而不是被压成 `done`）。
6. `test_a2a_cancel_sets_the_token_on_that_task`：`tasks/cancel` 后 `task["_cancel"].requested is True`。
7. `test_a2a_cancel_artifact_says_where_it_stopped`：桥随后返回 ⇒ artifact 里含「停在第 N 步」。
8. `test_cancel_before_start_does_not_run`：队列里的消息被取消 ⇒ ReAct 一次都没跑。
9. `test_follow_up_gets_a_fresh_token`：同一 taskId 追问后，旧令牌不影响新 run。
10. `test_no_cancel_means_zero_behavior_change`：全量既有测试（当前 800+ 条）必须全绿 —— 这是「默认无副作用」的硬证据。

## 8. 已知边界（要原样进 README 的「限制」段）

- 已成事实：取消是**请求**。它保证「不再开始新的一步」，不保证「立刻掐断正在跑的那一步」。
- **一条已经卡死的命令，阶段 1 停不下来**：`run_shell` 阻塞在 `communicate(timeout=limit)`（`builtins.py:591`），上界是它自己的 `limit + 15s`（最长 3615s）。阶段 2 落地后降到毫秒级 + 清树耗时（§4.8）。
- 自造 function 里的纯 Python 死循环**停不下来**（§5.1）。想要能停，工具自己轮询 `KERNEL` 上的令牌（阶段 2 的 `run_shell` 就是这个模式的范例）。
- 工具已经产生的副作用**不回滚**（它可能已经写了文件、发了请求）。取消停的是「继续做」，不是「做过的事」。
- 进程退出（Ctrl-C / `mgr.stop()`）走的是另一条路：`_fail_all` 把任务置 `failed`（`a2a.py:1323-1327`），那条路径**不**经过取消令牌 —— 两者不要混。
- `cancel_hard_grace` **默认 30 秒**（§4.8.5、§9 决策 3 之后的裁定）：取消后到点仍未停下就 `os._exit(137)`。它保证「服务还能接新活」，代价是那一轮的工作区可能停在半截（硬退出不等 flush）。`cancel_hard_grace: 0` 可关 —— 关掉之后，卡在纯 Python 死循环里的任务会永久堵住队列。

## 9. 决策记录（JEV 裁决，2026-11-18）

四个悬而未决的默认值交给独立决策模型（`decide`，服务端快照 `typesafe/jev-1.13-20260917`，
`id=gen-dec-1794967478-Rfigp53ATXNtALzqFEu3`，`elapsed_ms=574`）。概率照抄，不做阈值、不四舍五入：

| # | 问题 | 裁决 | 概率 | confidence |
|---|---|---|---|---|
| 1 | `cancel_grace` 默认值 | **2s** | 2s **0.89** / 0 0.11 | 0.78 |
| 2 | 阶段 2 做不做 | **完整做**（`kill` 唤醒 + 后台 `taskkill /F /T` 清树） | 完整做 **0.99** / 不做 0.01 / 只做一半 0.00 | 0.98 |
| 3 | 阶段 3 默认开关 | **默认关** | 默认关 **0.77** / 默认开 0.22 / 不实现 0.01 | 0.66 |
| 4 | 终态取值 | **`canceled`** | canceled **0.89** / interrupted 0.11 | 0.78 |

**决策 3 与本文档早先的写法相反**（那里写的是「常驻形态建议默认开」）。JEV 的 0.77 暴露了
那段论证漏掉的前提：硬退出的收益依赖「外部守护一定会把它拉起来」。把守护当成未验证的假设时，
默认开 = 取消一个任务把服务整个杀掉，比「暂时不能接新活」更彻底地违背了初衷。所以
**默认值归「不读文档也安全」，「怎么部署」归 §8 的部署清单**。它同时是全表 confidence 最低的
一条（0.66）：若将来 A2A 常驻成为默认形态、守护成为交付物的一部分，这一条应当重判。

**决策 4 成立与否是可核对的事实，不是偏好**：`canceled` 要站得住，必须确认
(a) `recorder.py:43` 白名单加值后，`Runtime.run`（`runtime.py:311`）与 `run_auto`（`runtime.py:571`）
两处 `status in ("error","interrupted")` 都补上 `"canceled"`；(b) 下游所有拿 terminal 状态做分支的地方
（`a2a.py` 的 `_view` / `_finish`、`cli.py` 退出码、报告渲染）对 `canceled` 是「认识并如实显示」，
而不是「落进 else 当异常」。这两条不成立，`canceled` 就是一个让报告撒谎的新词 —— 那时改用 `interrupted`。

**2026-11-18 人类裁定：默认开启。** 上面那句重判条件兑现了 —— A2A 常驻部署里，
守护是交付物的一部分（服务被拉起来才叫常驻）。落地值为 `cancel_hard_grace: 30`，
理由见 §4.8.5。JEV 的 0.77 没有被推翻：它问的是「在守护未被保证时该不该默认开」，
而当下的部署形态把那个前提补上了。**材料变了，判断跟着变；概率本身照抄不改。**
**决策 1 的兑现条件**：2s 只在 `wait()` 位于 `_tasks.lock` 之外时才成立（§4.6）。
挪进锁里，这 2 秒就变成自锁 —— 上界从「2 秒」变成「永不返回」。


## 10. 落地记录（2026-11-18）

| 文件 | 改了什么 |
|---|---|
| `openthree/cancel.py`（新增） | `CancelToken` / `Cancelled` / `NULL` / `bind`·`unbind`·`running_of` / socket 注册表（带 owner）/ `attach_killer` / `in_flight` |
| `openthree/llm.py` | `post_json` 挂 watch/unwatch、`was_aborted` ⇒ `Cancelled`；`chat` 先判 `Cancelled`、退避用 `token.wait()` |
| `openthree/agent.py` | `cancel_token` 参数 + 四个检查点 + 两处 `except` 顺序 + `cancel_summary()` + `llm_calls`/`tool_calls` |
| `openthree/runtime.py` | `run`/`run_auto` 加 `cancel`、`_run_round` 加 `cancel_token`、`except Cancelled → canceled`、两处断链补值 |
| `openthree/recorder.py` | `_TERMINAL_STATUSES` 加 `"canceled"` |
| `openthree/builtins.py` | `run_shell` 挂 killer（**整树一次杀**）、返回值写明「已取消 / 进程树手段」 |
| `openthree/channels/base.py` | `InboundMessage.cancel`（默认 None） |
| `openthree/channels/a2a.py` | `_rpc_send` 建令牌、`_rpc_cancel` 三段式、`send_text` 走 `canceled`、`_arm_hard_exit`、两个现读 property |
| `openthree/channels/bridge.py` | 队列未开跑检查、`ask(..., cancel=)` |
| `openthree/channels/config.py` | `cancel_grace`（2）/ `cancel_hard_grace`（30）两个 Field |
| `tests/test_cancel.py`（新增 10 条） | §7 的 1–9 + 阶段 2 的「卡死命令可杀」 |
| `tests/test_a2a_channel.py` | 旧用例改名并加断言：取消**真的置位了令牌** |
| `README.md` · `a2a.py` 模块头 · 本文件 | 「只是不再等」的说法全部改口 |

**验收**：`python -m pytest` ⇒ **819 passed**（含既有全部用例）。

### 与方案的三处偏差（都已回写正文）

1. **killer 的顺序**：方案写「先 `proc.kill()` 唤醒 → 再清树」，实测不成立（孙进程攥管道；`_kill_tree` 被 `poll()` 短路）。改成**一次杀整树**，§4.8.3 已改。
2. **阶段 3 的旋钮**：方案写 `--cancel-hard-grace` CLI flag；落地成**频道配置** `cancel_hard_grace`。理由：所有 A2A 旋钮（`block_wait` / `allow_images` / `outbound_roots`）都在这条缝上，再加一条 CLI 通路等于同一件事有两份真源。
3. **§4.8.6 的观测**：方案说 `_execute` 记 `tool_started_at`；落地成令牌上的 `in_flight(name)` / `in_flight_text()` —— 令牌本来就在 `_execute` 的调用栈上，不必把时间戳再传一路。

### 第三轮 ocr（12 条 → 9 修 / 2 记录 / 1 删死字段）

- **修**：`_kill_tree` 在 leader 已退出时仍要整组杀（POSIX `killpg`）；`attach_killer` 加守卫（`Thread.start()` 会抛，漏不得一个已起的子进程）；`send_text` 把「requested + 桥 notice」也当取消；`cancel_hard_grace` 的坏值/布尔否定一律落 **0**（否则 `false` 会被读成「开」）；检查点②移到 `if finished` 之后（finish 提交后不再抛 `Cancelled`）；`last_action` 在执行中先写；`post_json` 在 `read()` 之后也查一次 aborted（干净 EOF 不能冒充成功）；已请求取消时连 `connect()` 都不做；终端不把取消印成「模型调用失败」。
- **删**：`CancelToken.source` —— 写了从不读，死元数据。
- **记**（不改，理由如下）：见「仍未覆盖」的前两条。

### 仍未覆盖（诚实清单）

- **同一 task 连发两条 follow up 时只掐最新那条**：`task["_cancel"]` 只有一个槽位，`_rpc_cancel` 只发 `running` 与最新 `queued` 两个令牌；更早排队的那条的令牌不会被 request，它会跑完一整轮（答案随后被 `_run` 所有权检查丢掉）。改成 per-task 令牌队列要动数据结构，而该场景（同一 task 同时排两条）本身罕见 —— 记录在此，不在这一版扩。
- **取消路径的 `communicate` 仍可能等满 `limit`**：killer 杀的是进程树；若某个孙进程**脱离了进程组**还攥着继承的 stdout/stderr，EOF 不会来，`run_shell` 要等它自己的 timeout（最长 3600s）才落进既有的 `stream_cut` 兜底。要治它得把 `communicate` 改成短切片轮询 —— 那会给内核最热的工具加一条常驻分支，判据是「不写就是零」优于「运行时跳过」，所以这一版不做。
- 自造 function 里的**纯 Python 死循环**：检查点进不去、资源没登记 ⇒ 只能靠 `cancel_hard_grace` 硬退出。
- 工具**已经产生的副作用不回滚**。
- 取消时**不交付附件**：半成品不随答复发出去（`send_text` 的 `canceled` 分支不带 `files`）。

### 一条既有 flaky（与本改动无关，记录以免重复排查）

`tests/test_llm.py::test_post_chat_error_body_reaches_the_caller` 偶发
`ConnectionAbortedError`（`status=0` 而非 401）。曾疑为 `cancel.watch/unwatch` 引入，
用决定性实验推翻：同进程连打 20 次 401，`with watch` **0/20**、`without watch` **0/20**。
它是既有的进程级 flake，不修测试。

