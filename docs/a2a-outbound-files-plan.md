# A2A 出站文件支持方案

> 状态：**已实现（0.9.0 待发）** · 2026-11-12 · 场景：A2A 调用端需要收到文件
> 自评审：`docs/a2a-outbound-files-plan-self-review.md`

## 0. 结论先行

| 问题 | 答案 |
|---|---|
| 只挂一个工具能解决吗 | **不能**。工具的输出是文本，会被包成 `{"kind":"text"}`；`file` part 只能在频道层生成 |
| 要改哪些层 | **频道层**（`channels/base.py` / `bridge.py` / `a2a.py` / `config.py`）**+ 内核的工具注册**（新增 `openthree/attach.py`，并动 `builtins.py` / `runtime.py` / `cli.py` 各几行）。`agent / llm / prompt` 零改动 |
| 用户要装什么 | **什么都不用装**。`pip install -U openthree` 之后 `openthree -A2A` 起来就有 `attach_file`；不在 A2A 模式时它**不存在**（不是「存在但失败」） |
| 文件放正文还是引用 | **inline base64**（不依赖网络可达性 —— 对端不必能访问我们）；大文件另说 |
| 默认开不开 | **默认开**（2026-11-12 人已定）。卡片跟着配置声明 `application/octet-stream`；`outbound_roots: []` 是唯一关法 |
| 单文件多大 | **20 MiB**（2026-11-12 人已定）；单条合计 24 MiB、最多 4 个；任务表另有 256 MiB 字节预算 |
| 最大的风险 | 无鉴权端点 + 默认开 = **默认就有一条远程读文件通道** → 默认根只能是**工作区**、黑名单照旧；把 `-A2A-host` 改成 `0.0.0.0` 之前必须先把根收窄 |
| 工作量 | 9 个源文件约 300 行 + 17 条测试；发版后对用户零配置 |
| 边界变化 | 「不改内核一行代码」这条**已经不成立**：内置工具必须改 `builtins.py` / `runtime.py` / `cli.py`（§3.4.2 说清了为什么绕不开） |

**关于「改哪一层」，这条边界需要说准**：`a2a.py` 文件头第 5-6 行写着
「内核（agent / runtime / llm / prompt）一行都不用改 —— 一旦发现必须改它们，就说明选错了层」。
那条判据说的是**频道能力**（收 / 发 / 卡片），它至今成立：`agent` / `llm` / `prompt` 零改动。
但**工具**不属于频道层 —— 工具集是内核在注册期装配的产物（`builtins.py:2928`、`runtime.py:105`），
所以「A2A 启动即具备一个工具」这条要求**必然**要碰 `builtins.py` / `runtime.py` / `cli.py`。
这不是选错了层，是这条要求本身横跨两层：**能力在频道层，供给能力的手在内核**。
把工具硬塞进频道层反而会让「工具集在注册期一次装配」这条不变量失守。

## 1. 现状：为什么现在发不出去

五条事实（行号都已核对）：

| # | 事实 | 位置 |
|---|---|---|
| 1 | 写 artifact 的地方只产 `{"kind":"text"}` | `a2a.py:623-637`（`_set_terminal`）、`:1522-1543`（`_finish`） |
| 2 | 频道契约是纯文本：`send_text(msg, text)` | `base.py:84-86` |
| 3 | 桥只搬字符串：`reply(msg, answer)` | `bridge.py:105`、`:148-165`；`manager.py:121` |
| 4 | 卡片声明 `defaultOutputModes: ["text/plain"]`，且注释写明「回图不做」 | `a2a.py:542`、`:524-528` |
| 5 | 端点没有文件路由：GET 只有卡片，POST 只有 `/a2a` | `a2a.py:722-725`、`:727-731` |

所以「挂个发文件的工具」这条路上限是：**文件内容变成答案里的文本**，artifact 仍是 text part。
调用端能读到一段字符串，读不到一个 `file` part。

## 2. 目标形状

`message/send`（blocking）的响应会长这样：

```json
{
  "kind": "task",
  "id": "t-3f9c…",
  "status": {
    "state": "completed",
    "message": {"role": "agent",
                "parts": [{"kind": "text", "text": "报告生成好了，见附件。"}]}
  },
  "artifacts": [{
    "artifactId": "t-3f9c…-answer",
    "name": "answer",
    "parts": [
      {"kind": "text", "text": "报告生成好了，见附件。"},
      {"kind": "file",
       "file": {"name": "report.pdf", "mimeType": "application/pdf",
                "bytes": "JVBERi0xLjQK…"}}
    ]
  }]
}
```

三条不变量：

1. **文本 part 永远是第一位** —— 只从 `artifacts[].parts[].text` 取文本的老面板不会空白
   （`a2a.py:527-528` 那句「回 file part 会让它一片空白」要相应改写成「text + file 双 part」）。
2. **`status.message.parts` 只放文本** —— 同一份字节不在同一个响应里出现两次。
3. **`history` 只放占位** —— `[文件 1: report.pdf application/pdf]`，与入站
   「历史里不存 base64」（`a2a.py:1334-1341`）对称。

## 3. 设计：三段

### 3.1 谁决定附哪个文件 —— 模型

**一个内置工具** —— `openthree/attach.py` 里的 `attach_file(path, name="")`，随包发布、
只在 A2A 进程注册（§3.4）。选哪个文件、要不要附，是模型的判断（道）；工具只负责
「把它记下来」（二）。**不做**「自动附上本轮生成的所有文件」那种魔法 —— 那是替模型做判断。

两条实现细节：

- **路径基准**：相对路径按**进程 cwd** 解析（与 `multimodal.resolve_path` 的默认一致，`multimodal.py:109-114`），
  所以模型得给工作区绝对路径，或者用 `write_file` 时就用绝对路径。工具的回执里回显解析后的绝对路径，
  让模型一眼看出跑到哪去了。
- **提示词额度**：内置工具**没有** `__system_prompts__` 通道（那是 function 文件的机制），
  纪律走 docstring → 工具 description。它和 `__system_prompts__` 的差别不是「有没有额度」，
  而是**模型什么时候看到**：description 每步都在（要调它的那一刻正好需要），
  附随提示词进 system 尾部。代价是它计入工具 schema，不再有独立的 4000 字符配额。
  写三条就够：① 只在对方要文件时附；② 路径写绝对路径；③ 不要在答复正文里贴本机路径。

### 3.2 怎么传 —— 回程已经现成，只多一个字段

`send_text(msg, text)` 里的 `msg` 就是入站构造的那一条（`a2a.py:1405-1420` 构造，
`:1501` 靠 `reply_token` 找回 task）。所以附件**挂在 msg 上**，回程签名一字不改。

`InboundMessage` 加一个字段 —— 与 `images` 完全同构（`base.py:34-37` 的注释就是这么写的：
有默认值 ⇒ 微信 / 钉钉既有构造点零改动）：

```python
attachments: List[Dict[str, Any]] = field(default_factory=list)  # 本轮要回给对方的文件：{"path": str, "name": str}
```

「当前回合是哪一条 msg」由桥发布到 `KERNEL.state`；桥与工具在同一进程、同一线程里跑，
所以工具拿到的就是桥放进去的那个 `msg` 对象，直接往它身上挂附件。
桥侧 8 行、工具约 35 行、降级表见 **§3.4.6**；调度的完整落地见 **§3.4**。

> **诚实标注**：`KERNEL.state` 现有含义是「给用户 function 的草稿纸」，桥往里写是**新增一条隐式契约**
> （桥宣布「当前回合是谁」）。它成立的前提是「工具与 `rt.run` 在同一线程」—— 现有实现如此
> （ReAct 同步、工具在调用线程里跑）。若将来工具有一天被派到线程池，这条契约必须跟着改。

### 3.3 怎么发 —— `_set_terminal` 加一条参数

```python
# a2a.py:623  现在
def _set_terminal(task, state, text):
    task["status"] = {..., "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]}}
    task["artifacts"] = [{..., "parts": [{"kind": "text", "text": text}]}]
    task["history"].append({"role": "agent", "parts": [{"kind": "text", "text": text}]})

# a2a.py:623  之后
def _set_terminal(task, state, text, files=()):
    task["status"] = {..., "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]}}
    task["artifacts"] = [{..., "parts": [{"kind": "text", "text": text}, *files]}]
    task["history"].append({"role": "agent",
                            "parts": [{"kind": "text", "text": text}, *[_stamp(f) for f in files]]})
```

三个入口共用这一处（正常回话、服务退出、任务表挤出）。`files=()` 默认 ⇒ 后两个入口零改动。

`send_text` 里：

```python
files, notes = self._outbound_file_parts(msg.attachments)   # 校验 + base64 + mime + name
if notes:
    body = (body + "\n\n" + "\n".join(notes)).strip()
if is_notice:                       # 桥的通知不是结论，不带附件
    files = []
```

`_outbound_file_parts()` 是新方法（放在 `save_inbound_image` 之后，`a2a.py:1254` 附近），
**不抛异常**：每个被拒的文件各留一句原因，与入站「一张图没收到不该让整条消息失败」
（`a2a.py:1310-1311`）同一判据。

### 3.4 `attach_file` 是怎么挂上去的

三个问题要分开：**谁把它放进工具集**（随包 + 门控）、**它怎么和频道接头**（共享点）、**缺了它会怎样**（降级）。

#### 3.4.1 住哪：`openthree/attach.py`，随发行版发布

**它不是一个要用户自己装的文件。** 它是包里的一个模块，`pip install -U openthree`
之后就在，发版即得。`pyproject.toml:52` 的 `include = ["openthree*"]` 让包内新模块
自动进 wheel 与 sdist，不需要额外登记。

先例是 `decide`：它住在 `openthree/decide.py`，由 `builtins.installed_builtins()`
（`builtins.py:2928-2940`）并进内置工具集。`attach_file` 走同一条路 —— 两个工具的区别
只在门控条件：`decide` 看环境变量，`attach_file` 看「本进程会不会起 A2A 频道」。

#### 3.4.2 什么时候注册：注册期一次，条件 = 本进程会起 A2A

`installed_builtins()` 现在是「常驻集 + 条件集」。加一个参数即可：

```python
# builtins.py:2928
def installed_builtins(*, a2a: bool = False) -> Dict[str, Any]:
    out = dict(BUILTIN_FUNCTIONS)
    out.update(optional_builtins())        # decide（看环境变量）
    if a2a:
        out["attach_file"] = attach_file   # 随包发布，只在 A2A 进程出现
    return out
```

两处调用点，一处要改：

| 调用点 | 传什么 | 为什么 |
|---|---|---|
| `runtime.py:105`（`Runtime.__init__`） | `a2a=self.a2a_enabled` | 真进程，**唯一需要算的地方** |
| `cli_tool.py:110`（离线内核） | 不传（默认 `False`） | 命令行工具没有频道，不该有它 |

`builtins.py` 顶部加一行 `from .attach import attach_file`（与 `:42` 的
`from .decide import optional_builtins` 并列）。`attach.py` 只 import `kernel`，
`kernel.py` 不 import builtins —— 不成环。

`Runtime.__init__` 加一个关键字参数 `a2a: Optional[bool] = None`（三态，见下），
存成 `self.a2a_enabled`，在 `runtime.py:104-108` 那次注册之前就位。

**谁算这个布尔**：`Runtime.__init__` 自己判，`cli` 只补一个开关

```python
# runtime.py:46  —— None = 「自己看磁盘」
def __init__(self, ..., a2a: Optional[bool] = None) -> None:
    ...
    self.ws = Workspace(...).init(...)          # :85 此刻 channels.yaml 已可读
    if a2a is None:
        from .channels.manager import declares_a2a   # 延迟 import，见下
        a2a = declares_a2a(self.ws)                  # ① channels.yaml 里有 enabled 的 a2a
    self.a2a_enabled = bool(a2a)
    ...
    for name, fn in installed_builtins(a2a=self.a2a_enabled).items():   # :105
```

`cli.py:332` 补 ② 那个来源，并且要盖住「本进程根本不起频道」的三种情形：

```python
# 本进程会不会真的起 A2A 频道？三态，None = 交给 Runtime 读 channels.yaml
if args.a2a:
    a2a_flag = True          # -A2A：必然起（cli 随后还会校验它真起来了）
elif (not args.no_channels and not args.upgrade
      and (args.daemon or _wants_interactive(args))):
    a2a_flag = None          # -i / -d：会不会起取决于 channels.yaml
else:
    a2a_flag = False         # 单次任务 / -u / --no-channels：频道一个都不起
rt = Runtime(..., a2a=a2a_flag)
```

三态缺一不可，每一态都有真实的错法：

| 漏了哪一态 | 后果 |
|---|---|
| 只判 `args.a2a` | `-i` / `-d` 里配了 a2a 的人拿不到工具（最该有的场景） |
| 不判 `args.no_channels` | `-i --no-channels` + 磁盘声明了 a2a → 工具在，但一个频道都不起（`--no-channels` 只与 `-A2A` / `-d` 互斥，`:307`、`:391`，**不**与 `-i` 互斥） |
| 不判单次任务 | **最常见的那条路**：`openthree "任务"` 压根不加载频道（`cli.py:433-468` 全程没有 manager），却在有 a2a 声明的工作区里注册工具 —— 每步 system 前缀都多一个永远用不上的工具 |
| 不判 `args.upgrade` | `-u` 走 `_interactive(..., no_channels=True, ...)`（`cli.py:419-422`），**在调用点硬编码关频道**，`args.no_channels` 却是 `False` —— 只看 `args.no_channels` 会漏掉它 |

第三种最容易漏，也最贵：单次任务是 CLI 的主力用法，错在这里等于给绝大多数运行加了噪音。
第四种是「同一件事有两个真源」的现成教训：关频道的决定散在 `args.no_channels` 与
`_interactive` 的调用点两处，门控只看其中一处就会漂。

**`declares_a2a()` 放哪**：与 `has_configured_channels`（`manager.py:251-257`）并列。
那函数已经是“读 channels.yaml 回答一个关于频道的问题”的现成形状，但它的判据
（`cfg.ready()`）在这里用不上 —— a2a 一条 required 都没有，`is_ready()` 恒为真
（`channels/config.py:100-101` 的注释明说了），拿它判等于“有就真”。所以另写一个
按 `type == "a2a"` 且 `enabled` 判的。

**为什么延迟 import，而不是放 `runtime.py` 顶层**：不是为了绕循环 —— 已核实
`openthree/channels/` 下没有任何文件 import `runtime`（全目录 0 命中），成不了环。
理由是守住那条既有的层界（`a2a.py:5-6`「内核一行都不用改」）。这里已经动了一点，
但至少别让**模块加载期**就建立「内核 → 频道层」的依赖：`runtime.py` 顶层 import
`channels.manager` 会把 yaml / bridge / 线程一并拉进**每一个** `from openthree.runtime
import Runtime` 的地方（本项目测试里就有几十处）。函数内 import 只在真要判的那一刻
付这份成本，且模块缓存让它只付一次。

**嵌入者要自己传**：`channels/__init__.py:12-20` 那段文档演示的是
`Runtime(project_dir)` + `ChannelManager.from_workspace(...)`。走这条路的嵌入者必须
显式 `Runtime(project_dir, a2a=True)` —— 因为 `None` 只读磁盘上的 channels.yaml，
读不到他们内存里那份配置。这是“内置 + 门控”的代价，写在 docstring 里。

**为什么是注册期，不是每步判**：工具集是注册期产物，会话中途增减会让 system
前缀每步都变、缓存前缀失守（判据写在 `builtins.py:2935-2936` 与
`decide.py:126-138`）。这条与 `decide` 完全同构，不引入新机制。

**一处诚实的偏差**：门控判的是「**会起** A2A」而不是「**已起**」。`--only wechat`
时 A2A 频道不会启动，但 `declares_a2a` 仍为真 → 工具在、却没有承载者。它只导致
「工具调用时老实说这次发不出去」（§3.4.6 第一行），不会假装成功。要做精确同步，
就得把注册推迟到 manager 装配之后 —— 那违反上一条，不值得。

#### 3.4.3 接头：同进程 + `KERNEL.state`（每一步都有代码位置）

1. 收到消息 → `manager._make_handler` → `self.bridge.submit(msg, channel.send_text)`（`manager.py:115-122`）
2. bridge 工作线程 `_handle` → `ask()` → `with self._run_lock: self.rt.run(...)`（`bridge.py:148-165`、`:114-124`）
3. ReAct 循环调工具 → `if tool.needs_kernel: call_args["kernel"] = KERNEL`（`agent.py:1021-1022`）
4. 工具拿到的 `KERNEL` 与桥看到的是**同一个进程单例**（`kernel.py:29`），而它的 `.state`
   就是「给用户 function 自由使用的草稿纸」（`kernel.py:20`）

工具**不需要** import 频道、不需要知道端口、不需要网络 —— 它只往「当前回合」上挂一项。

#### 3.4.4 桥侧（新增约 8 行，`bridge.py`）

```python
from ..kernel import KERNEL

# _handle 里
slot = ("attach-turn", threading.get_ident())
channel = getattr(reply, "__self__", None)          # 绑定方法自带频道实例
carrier = bool(getattr(channel, "supports_files", False))
if carrier:
    KERNEL.state[slot] = msg
try:
    answer = self.ask(compose_input(msg), images=msg.images)
finally:
    if carrier:
        KERNEL.state.pop(slot, None)
```

两个刻意的选择：

- **`getattr(reply, "__self__", None)`**：`reply` 就是 `channel.send_text` 这个绑定方法
  （`manager.py:121`），`__self__` 带回频道实例 —— 于是 `submit()` 的签名一个字不用改。
- **只有 `supports_files` 为真的频道才发布槽**。`A2aChannel` 加一行类属性 `supports_files = True`，
  「能不能发文件」由**频道自己说**。在微信 / 钉钉回合里调到这个工具时，槽不存在 →
  工具老实回「这次答复发不出文件」，而不是假装附上了。

#### 3.4.5 工具本体：`openthree/attach.py`（随包发布，约 35 行）

```python
# openthree/attach.py —— 与 openthree/decide.py 同构：独立模块，由
# builtins.installed_builtins(a2a=…) 条件注册。不落 .openthree/functions/。
"""A2A 回合里给答复附文件：attach_file 只负责记下，频道在回程取走并校验。"""
from __future__ import annotations

import threading
from pathlib import Path

from .kernel import KERNEL

SLOT = "attach-turn"


def attach_file(path: str, name: str = "") -> str:
    """把一个本地文件附到本次 A2A 答复上（对方会收到一个 file part）。

    只在对方明确要文件时用；对方只问一句结论就别附。`path` 给绝对路径
    （相对路径按进程 cwd 解析，你多半猜不对）。回执说「已记下」只是记下了：
    真正发不发得出去由频道层校验，被拒的原因会写进答复正文。回执说
    「这次答复发不出文件」就**别**在正文里写「见附件」——那是骗人。

    Args:
        path: 文件路径（建议绝对路径）。
        name: 对方看到的文件名；留空用原文件名。
    """
    turn = KERNEL.state.get((SLOT, threading.get_ident()))
    if turn is None:
        return ("这次答复发不出文件：当前不是一个支持出站文件的频道回合"
                "（没有频道在跑，或该频道不支持）。请在正文里说明文件在哪，别写「见附件」。")
    p = Path(path).expanduser()
    try:
        if not p.is_file():
            return f"附不上：{path} 不是文件（或不存在）。"
        size = p.stat().st_size
    except OSError as exc:
        return f"附不上：{path} 读不到（{type(exc).__name__}: {exc.strerror or exc}）。"
    turn.attachments.append({"path": str(p), "name": name})
    return (f"已记下 {p.name}（{size} 字节），会随本次答复发出。"
            "不在允许目录内的话会被拒，原因会写进正文。")
```

四条设计判断：

- **不要 `kernel` 首参**。该机制（`tool.py:165-168`）是给**用户态 function** 用的；
  内置工具与 `KERNEL` 在同一个包里，直接 `from .kernel import KERNEL` 更省，
  也少一处「注入没生效」的可能。`builtins.py:43` 本来就是这么 import 的。
- **纪律写在 docstring 里，不能用 `__system_prompts__`**。后者是 function 文件的机制
  （`registry.py:36-39`、`eval_attached_prompt()` 在 `:47-86`），内置模块拿不到。
  好在 docstring 全文会进工具描述（`tool.py:151`、`:179-186`），模型每步都看得见 ——
  这四条恰好都该在**要调用它的那一刻**看到，放进 description 比放进 system 更贴切。
  代价要说清：它占用的是工具描述的字数，不再有「附随提示词」那 4000 字符的独立配额。
- **工具只做预检（存在 / 是文件 / 报大小），不做安全判定**。安全闸只有一份，在频道层（§4）。
  两份校验迟早发散，而安全判定不能被子系统里的任何一层绕过。
- **`attachments` 每项是 `{"path", "name"}`**：`name` 只有模型知道（源文件名常常不是对方
  想要的），但它绕不过消毒 —— 频道层照样只取 `basename`、滤控制字符（§4 闸 4）。

**内置之后仍可被覆盖**：`Registry.register_builtin` 把内置的 `source_distance` 设成
最远（`registry.py:181` 的注释：「任何同名的外部工具都顶得掉它」）。所以装了同名
function 的人会拿到自己那份 —— 这是既有裁决规则，不需要为 `attach_file` 开例外。

#### 3.4.6 门控判错时会怎样（降级是诚实的）

内置 + 门控之后，「工具在不在」与「这次发不发得出去」是两件事。判错只会落在第二件上：

| 情况 | 结果 |
|---|---|
| 非 A2A 进程（`openthree "任务"` / `-i` 无频道 / `--no-channels`） | 工具**不在工具集里** → 模型手上没有它，只能写「文件在 X」。**不会**自动附任何东西（§10） |
| A2A 进程，但这次是**控制台**回合（`-A2A` 在终端 = 交互 + A2A） | 工具在，但控制台回合没有 msg → 槽不存在 → 回「这次答复发不出文件」 |
| A2A 进程，但这次是微信 / 钉钉回合 | 同上（`supports_files` 为假） |
| 用户自己写了同名 `attach_file` | 用户的赢（`registry.py:181` 内置距离最远）—— 那是他明确的选择 |
| 频道层是旧版、工具是新的 | 不可能：两者在**同一个 wheel** 里，不存在版本错配 |

所以这次没有「部署顺序」问题 —— 一次发版解决。**唯一的错配来源是用户的旧安装**，
而旧安装里工具也不存在（`installed_builtins` 还没这个 key），所以仍然一致。

**为什么门控不判「已起」而判「会起」**：注册在 `Runtime.__init__`（`runtime.py:105`），
频道装配在那之后（`cli.py:431` 才把 `channels_extra` 交出去）。要在注册期知道
「将来会不会有 a2a 频道」，只能读声明（`-A2A` 开关 + channels.yaml）。精确到「已起」
就得把注册推迟到 manager 装配之后 —— 那会让工具集在进程中途变化，system 前缀每步都变，
违反 `builtins.py:2935-2936` 那条判据。两害相权，取「会起」。

## 4. 安全边界（P0，不能省）

**先摆事实：A2A 端点没有鉴权。** `Channel.allowed()`（`base.py:113-116`）只被微信
（`wechat.py:358`）与钉钉（`dingtalk.py:255`）调用，A2A 从头到尾没调它；`handle_rpc()`
也不校验 `caller`（`_caller()` 只是记来源 IP 当 `user_id`）。所以
**「谁能让模型发文件」= 「谁连得上这个端口」**。

四道闸之外，第一道是总开关 —— **默认开着**（2026-11-12 人已定），所以默认根必须选得保守：

0. **默认开启，默认根只有工作区**：`outbound_roots` 默认 `["."]`（启动时 resolve 成工作区绝对路径）。
   「能发文件」默认就成立，「能发到哪」由根决定。关掉它只写 `outbound_roots: []`。
   **卡片跟着它走**：`build_card` 在 `outbound_roots` 非空时把 `application/octet-stream`
   加进 `defaultOutputModes`，为空时仍是 `["text/plain"]` —— 这是「卡片不撒谎」
   （`a2a.py:524-526`）：没开却说能回文件，客户端会等一个永远不来的 file part；开了却不说，
   严格客户端根本不会来要。
   **默认是「能发」，不是「随便发」**：默认监听 `127.0.0.1`（`a2a.py:65` 的 `DEFAULT_HOST`），
   跨机时人才会显式改成 `-A2A-host 0.0.0.0`。从那一刻起，**任何连得上这个端口的人都能让你
   把工作区里的文件发出去** —— 这句话必须放在 `-A2A-host` 的 help 文本旁边，不能只躺在安全附录里。
1. **根白名单** `outbound_roots`：`Path.resolve()` 之后再判 `is_relative_to(root)` ——
   resolve 会解开符号链接，软链逃逸也拦得住。
2. **硬黑名单**：`.openthree/config.yaml`、`api_key`、`identity.json`、`limits.json`、
   `memory/`、`sessions/`、`.env`。即使人把根放宽到项目根，这些也不发 ——
   它们一变，整个工作区（含模型密钥）就跟着走。
3. **三道上限 + 一条预算**：单文件 `MAX_OUTBOUND_FILE_BYTES = 20 MiB`（人已定）、
   单条最多 `MAX_OUTBOUND_FILES = 4` 个、单条合计 `MAX_OUTBOUND_TOTAL_BYTES = 24 MiB`
   —— 「一个 20 MiB 大文件」够用，「四个大文件」不过。另有任务表字节预算
   `MAX_TASK_FILE_BYTES = 256 MiB`（见 §5）。
4. **名字消毒**：只取 `basename`，滤掉控制字符与路径分隔符，长度 ≤ 128；
   `mimeType` 由本机 `guess_mime()` 决定（`multimodal.py:132-137`，认不出回
   `application/octet-stream`），**不采信任何外部输入**（与入站「类型只认 magic bytes」同源）。

## 5. 体积账（20 MiB 是四笔账，不是一笔）

20 MiB 原始文件 → base64 后 **26.7 MB**，JSON 化后**一个响应体 27 MB 上下**。它会被复制四次：

1. **`_public()` 深拷贝**（`a2a.py:640-653`：`json.loads(json.dumps(task))`）—— 每做一次快照，
   26.7 MB 字符串就再拷一份。blocking `message/send` 里至少一次，之后每次 `tasks/get` 一次。
   峰值不是 27 MB，而是「相比原文件 ×3～4」≈ **80–110 MB**。
2. **blocking 的 `message/send` 响应体**就是这个 Task —— 27 MB 走一趟。
3. **回调整整份 Task POST 出去**（`a2a.py:1631`），而 `PUSH_TIMEOUT = 5.0`（`:108`）、
   `PUSH_TRIES = 4`（`:109`）、退避 `0.5/1/2` 秒（`:110`）。20 MiB 在**千兆局域网**约 0.3 秒、
   **百兆**约 2.3 秒、**公网 20 Mbps 上行**约 11 秒 —— 公网必然超时，然后重试 4 次，
   投递线程被占 ≈ 4×5 + 1.5 ≈ **22 秒**，而 `PUSH_STOP_GRACE = 5.0`（`:112`）意味着停机时
   这条投递会被放弃。
4. **任务表**：`TASK_LIMIT = 200`（`:102`）条 task 常驻内存。若每条都可能带着 27 MB，
   上限是 **5.4 GB** —— 这不是理论值，是不装闸时 OOM 的算术。

**所以 20 MiB 单文件必须配三件事，否则只是把「发不出去」换成「发出去然后内存炸」：**

| 闸 | 值 | 作用 |
|---|---|---|
| `MAX_OUTBOUND_FILE_BYTES` | **20 MiB** | 人已定的单文件上限 |
| `MAX_OUTBOUND_TOTAL_BYTES` | **24 MiB** | 单条消息合计（一个 20 MiB 大文件 + 零头，或几个小文件） |
| `MAX_TASK_FILE_BYTES` | **256 MiB** | 任务表里文件字节的**总**预算：超了就把**最旧** task 的 file part 换成 `{"kind":"text","text":"[文件已过期：name]"}` —— 降级写在协议里，不是静默丢 |

还有两条与「对端是谁」有关：

- **对端也是 openthree** → 它的入站硬闸是 `MAX_BODY_BYTES = 8 MiB`（`a2a.py:87`、`:744-746`），
  27 MB 的 POST 会**413 直接拒**。要么把对端的闸调大（那是另一台机器上的另一个常量），
  要么走阶段 2 的 URI。
- **对端是普通 HTTP 客户端** → 它得自己扛 27 MB 响应体（`§8` 的代码是流式读还是全量读由它决定）。

结论：**20 MiB 属于「够用的上限」，不是「随便用」的上限**。真要在公网传 20 MiB，
inline 是最后手段，阶段 2 的一次性 URI 才是正解。

## 6. 改动清单

| 文件 | 位置 | 改什么 | 行数 |
|---|---|---|---|
| `channels/base.py` | `InboundMessage`，`images` 之后（34-38） | `attachments: List[Dict]` 字段 + 注释 | +5 |
| `channels/bridge.py` | `_handle`（148-165） | 发布/清理当前回合槽（**仅当频道 `supports_files`**） | +8 |
| `channels/a2a.py` | 常量区（101 之后） | `MAX_OUTBOUND_FILE_BYTES = 20 MiB`、`MAX_OUTBOUND_FILES = 4`、`MAX_OUTBOUND_TOTAL_BYTES = 24 MiB`、`MAX_TASK_FILE_BYTES = 256 MiB`、`DEFAULT_OUTBOUND_ROOTS` | +10 |
| 同上 | 类体（`input_modes` 附近） | 类属性 `supports_files = True` | +1 |
| 同上 | `_set_terminal`（623-637） | 加 `files` 参数 | +6/−2 |
| 同上 | `_finish`（1522-1543） | 透传 `files` | +2 |
| 同上 | `send_text`（1499-1520） | 构造 file parts + 通知判据 | +8 |
| 同上 | 新方法 `_outbound_file_parts`（1254 附近） | 根白名单 / 黑名单 / 三闸 / base64 / mime / name + 任务表预算降级 | +60 |
| 同上 | `build_card`（542）+ 注释（524-528） | 默认声明 `application/octet-stream`；「回 file part 会让面板空白」那句改写成「text + file 双 part」 | +6/−4 |
| `channels/config.py` | a2a spec（`allow_images` 附近，127） | `Field("outbound_roots", …)`，help 里带暴露警告 | +4 |
| `.openthree/functions/a2a_attach.py` | — | **不建这个文件**：能力随包发布（见下一行） | 0 |
| `openthree/attach.py` | 新文件 | `attach_file` + docstring 纪律 + `__main__` 尾钩（§3.4.5 全文） | +40 |
| `openthree/builtins.py` | `:42` 附近 import / `:2928` `installed_builtins` | `from .attach import attach_file`；签名加 `*, a2a: bool = False`，条件并入 | +6 |
| `openthree/runtime.py` | `__init__` 签名（46-61）/ `:85` 之后 / `:105` | `a2a: Optional[bool] = None`；延迟 import 判 `declares_a2a`；传给 `installed_builtins` | +8 |
| `openthree/cli.py` | `:332` 构造 `Runtime` 之前 | 三态 `a2a_flag`（`-A2A` / `-i`·`-d` / 其余） | +9 |
| `channels/manager.py` | `:251` 附近（`has_configured_channels` 旁） | `declares_a2a(ws)`：按 `type == "a2a"` 且 `enabled` 判 | +10 |
| `tests/test_a2a_channel.py` | 新用例 + 改 `:1846` | 17 条（含两条门控） | +290 |

## 7. 测试清单

1. `send_text` 带一个附件 → artifact `parts` = `[text, file]`，`file.bytes` 解码后与源文件字节一致
2. `status.message.parts` 只有文本（不重复字节）
3. `history` 只有占位，不含 base64、不含本机绝对路径
4. 单文件超 20 MiB → 该文件被拒，文本里多一句原因，整条仍是 `completed`
5. 第 5 个文件 / 合计超 24 MiB → 同上
6. 根外路径 → 拒
7. 不存在的文件 / 目录 / 空文件 → 拒
8. 指向根外的符号链接 → 拒（resolve 后判定）
9. 黑名单路径（`config.yaml`）→ 拒
10. 通知类回复（`msg.raw["notice"]`）→ 不带附件
11. follow up 复位后旧附件不残留（`a2a.py:1372` 已清 `artifacts`）
12. 老客户端只读 text part → 仍拿到完整答案；卡片 `defaultOutputModes` 断言改为**默认含** `application/octet-stream`（`tests/test_a2a_channel.py:1846`）
13. **任务表预算**：把 `MAX_TASK_FILE_BYTES` 调小后连发几轮 → 最旧 task 的 file part 变成 `{"kind":"text","text":"[文件已过期：…]"}`，其余 task 不受影响
14. **降级不撒谎**：`reply.__self__` 没有 `supports_files` 的频道（微信 / 钉钉的桩）→ `KERNEL.state` 里**没有** `("attach-turn", tid)`，且 `attach_file` 回「这次答复发不出文件」
15. **有附件时的回调策略**：登记了 `pushNotification` 的 blocking send 仍回 `completed`；按选定策略（见 §11）断言日志里有一句「附件不走回调」或「超时已延长」
16. **门控本身**：`installed_builtins(a2a=False)` 的结果里**没有** `attach_file`，`a2a=True` 里有；
    `decide` 的常驻行为不受影响（`a2a` 是关键字参数，默认 `False`）
17. **非 A2A 进程里它不存在**（最容易错的一条）：工作区 `channels.yaml` 声明了 a2a，但
    a) 单次任务 `openthree "任务"`、b) `-i --no-channels`、c) `-d` 且 a2a 是 disabled ——
    三种情况下 `list_functions()` 都**不含** `attach_file`

## 8. 调用端怎么收

```python
import base64, pathlib

task = resp["result"]
for art in task.get("artifacts", []):
    for part in art.get("parts", []):
        f = part.get("file") if isinstance(part, dict) else None
        if not f:
            continue
        data = base64.b64decode(f["bytes"])
        name = pathlib.Path(f.get("name") or "download.bin").name
        pathlib.Path("recv").mkdir(exist_ok=True)
        pathlib.Path("recv", name).write_bytes(data)
        print("收到", name, f.get("mimeType"), len(data), "字节")
```

只用 `tasks/get` 拉也行；如果登记了回调，整份 Task（含文件）会 POST 到那个地址。

## 9. 阶段

**一次发版解决，没有「用户还要做什么」这一步。**

- **阶段 1（P0，一次发版即完成）**：3.2 + 3.3 + §3.4（含内置 `openthree/attach.py` 与三态门控）
  + §4 五道闸 + §5 四笔体积账 + §7 十七条测试。
  发版后：`pip install -U openthree` → `openthree -A2A` 起来就有 `attach_file`；
  不在 A2A 模式时它**不存在**。用户不装文件、不改 profile、不写配置。
- **阶段 2（可选，大文件）**：`GET /files/<token>` URI 路由 —— file part 写 `uri` 而不是 `bytes`，
  需要 token 表 + TTL + 撤销（`a2a_file_link` 里已有可复用的实现）
- **给微信 / 钉钉也开**（未来，若要）：它们只需实现 file part 的发送并加一行类属性
  `supports_files = True` —— 工具本身一个字不用改（它只认「当前回合有没有承载者」）
- **发版动作**（与既有发布流程一致）：版本号 bump → 全量 pytest → 提交 → `python -m build`
  → `twine check` → `twine upload` → 按 `docs/RELEASING.md` 核对 sdist 与 sha256。
  注意 `MANIFEST.in` 按**磁盘**收 sdist（`:16`），新文件 `openthree/attach.py` 会随包走；
  但也意味着未跟踪的草稿同样会进包，发布前先看一眼 `git status`。

## 10. 不做什么

- 不做流式（既有事实：`message/stream` 一律 `-32601`）
- 不下载远端 URI（入站既有判据：SSRF）
- 不在 `status.message` 放文件字节
- 不自动附「本轮生成的所有文件」（替模型做判断）
- 不压缩、不打包

## 11. 待你拍板

**已定（2026-11-12）**：

- **默认开启**：`outbound_roots` 默认 `["."]`（工作区根），卡片默认声明能回文件。
  唯一关法：显式写 `outbound_roots: []`。
- **单文件 20 MiB**：配 `MAX_OUTBOUND_FILES = 4`、`MAX_OUTBOUND_TOTAL_BYTES = 24 MiB`、
  `MAX_TASK_FILE_BYTES = 256 MiB`（任务表字节预算，超了最旧的降级成文本占位）。
- **随包发布、A2A 时自动注册**（第三轮人已定，原话「我们发新的包后，别人 A2A 模式启，
  自然具备这个能力，应该还是 A2A 时才加」）：`attach_file` 是 `openthree/attach.py`，
  不走 `.openthree/functions/`、不走 profile。发版即得，非 A2A 进程里它不存在。
  代价说清：这条要求**改动了内核的三个文件**（`builtins.py` / `runtime.py` / `cli.py`），
  与第一轮「不改内核一行代码」的边界冲突 —— 按第三轮的要求执行。

**已定（2026-11-12 第四轮）**：

1. **20 MiB + 回调**：`PUSH_TIMEOUT` 从 5 秒拉到 **60 秒**（人已定）。
   实施时按审查意见追加了 `PUSH_TOTAL_BUDGET = 150 秒` 的总预算 —— 60 × 4 次重试
   = 4 分钟，而投递是单线程 FIFO，一个收下连接就不说话的对面会把后面所有回调堵死。
2. **开始实施**（人已定）：阶段 1 已落地，见 §12。

## 12. 实施记录（2026-11-12）

与上面方案的差异，逐条都在这份代码里（每条都有一句「为什么」写在代码注释里）：

| # | 方案 | 落地 | 为什么 |
|---|---|---|---|
| 1 | 只有 `PUSH_TIMEOUT = 60` | 追加 `PUSH_TOTAL_BUDGET = 150` | 60 × 4 次 = 4 分钟堵死单线程 FIFO（`ocr review` 第 7 条） |
| 2 | 黑名单是枚举（`.openthree/config.yaml` 等） | **反向白名单**：`.openthree/` 下除 `assets/` 外全拒 | 升级模式把旧文件 `copy2` 到 `.openthree/upgrade/<ts>/files/config.yaml`，那是同一份带 `llm.api_key` 的副本，枚举会漏（review 第二轮第 3 条） |
| 3 | `MAX_OUTBOUND_FILES` 在 `a2a.py` | 真源搬到 `attach.py`，`a2a.py` import 它 | 工具要用它决定「还收不收」；两处各写一个 4，回执与行为会发散（review 第 2 条） |
| 4 | `supports_files = True` 类属性 | 改成 property，跟随 `outbound_roots` | `outbound_roots: []` 时桥不该发布回合，否则工具先说「已记下」再被拒（review 第 5 条） |
| 5 | cli 里内联三态 | 抽成 `cli._a2a_flag(args)` | 四种状态都要能一条条测；内联代码测不到就会漂（§3.4.2 的四种态已各有断言） |
| 6 | 17 条测试 | **21 条**（`test_a2a_channel.py` 新增 486 行） | 两轮 review 各补了守漏的用例 |

验证数字（本机，2026-11-12）：

* 全量 `pytest`：**808 passed, 1 skipped**（89s）。
* 端到端（真 `A2aChannel` + 真 HTTP，不 fake 任何一层）：20 MiB 文件 →
  `['text', 'file']`，base64 27,962,028 字符，解码一致，`tasks/get` 响应体 26.7 MB、
  0.36s；`history` 不含 base64；`status.message` 只有文本。
* 21 MiB → 只拒这一个（「超过单文件上限」），整条仍 `completed`；根外路径 →
  「不在允许发送的目录内」；`.openthree/config.yaml` → 「命中密钥黑名单」。
* `ocr review` 两轮：第一轮 10 条、第二轮 3 条，**全部成立、全部已修**（含
  `.openthree/channels.yaml` 与 `channels/` 的渠道凭证外带、`workspace_root()` 未
  resolve 导致黑名单被静默跳过、TOCTOU、空答案 + 全被拒仍记 `completed`、
  `ValueError` 逃逸让 task 永远 `working`）。
