# 方案：`-A2A` —— 让 openthree 成为可被别的智能体调用的一个频道

> 状态：**已实现** —— `openthree/channels/a2a.py`（服务端 / 卡片 / 任务表 / 注册器）
> 与 `tests/test_a2a_channel.py`（47 项）。实现与本文的偏差记在 §十五。
> 目标一句话：
> `openthree -A2A` 起一个 A2A 服务端 + 把自己注册到 registry（默认本机 OACP），
> 别的智能体通过 `message/send` 提交任务，任务走 `Runtime.run()`，答案回到 A2A 的 task 里。
> **形态与钉钉频道完全同构**：频道只负责「收」与「发」，思考仍然是 `Runtime.run()` 的事。
>
> **2026-11-06 裁决**：A2A 只是一个特殊的频道，**不动内核** ——
> `agent.py` / `runtime.py` / `llm.py` / `prompt.py` / `builtins.py` 零改动。
> 因此「每个 task 隔离会话」不做（那需要给 `Runtime.run()` 加参数）：
> 共享会话是**设计**，不是待修的缺陷；它的代价原样写进 README（§9）。
>
> **2026-11-06 裁决（第二轮）**：
> ① `-A2A` **永远是开关**，地址只走 `--a2a-registry`（§5.1）；
> ② 监听端口**自动探测 9000–9999**，不再固定 9100（§5.4）——
>    代价是「端口会漂 ⇒ registry 里 `id` 跟着变」，用「优先复用上次端口」
>    + 「旧条目自动收尸」两件事偿还（§4 末「注册条目的寿命」），不接受这笔债就只剩改协议；
> ③ **不做卡片签名**（不是 v1 不做、v2 再说，而是**不做**），
>    只保留诚实条款「注册过 ≠ 可信」（§8.5）。
>
> **2026-11-06 裁决（第三轮）**：`--a2a-port` 指定了就**用指定的**（§5.4）。
> 它的语义不是「候选序列的第一位」，而是**另一种模式**：不构建候选序列、不查
> `last_port`、不降级，被占就报错退出。自动模式（9000–9999）是「你没指定」时的
> 行为，两者互斥。这条的收益是 `card_url` 跨重启不变 ⇒ registry 里的 `id` 不变，
> 是**唯一**能让身份长期稳定的手段（自动模式只能靠 `last_port` 尽量不漂）。

依据两份外部事实（都已对着源码核过）：

| 来源 | 它定的事 | 关键位置 |
|---|---|---|
| `A2A Auto Discover`（ADP Core v1） | registry 的 5 个操作、AgentCard 必填项、`id` 由 URL 派生、限流、SSRF | `spec/adp-core.md` §4.1 / §5 / §6.3 / §7 |
| `open-agent-control-panel`（OACP） | 参考 registry 的默认地址与本地放宽策略、A2A 两种方言的客户端、任务台账 | `README.md`、`src/oacp/a2a/client.py`、`src/oacp/adp/card.py` |

---

## 一、先把形状说清：谁在跟谁说话

A2A 里有两张网，**不要混**：

```
        ① 发现（HTTP GET 卡片，registry 主动抓）
   ┌───────────────┐   GET /.well-known/agent-card.json   ┌──────────────┐
   │   registry    │ ───────────────────────────────────> │  openthree   │
   │  (OACP/ADP)   │ <─────────────────────────────────── │  A2A 频道    │
   └───────────────┘   POST /v1/agents {"url": 卡片地址}   └──────────────┘
            ▲                                                  ▲    │
            │ ② 检索（GET /v1/agents?q=…）                       │    │ ③ 任务
            │                                                    │    ▼
   ┌───────────────┐   POST /a2a {"message/send"}  ─────────────┘  （JSON-RPC）
   │  调用方 agent │ ───────────────────────────────────────────────────
   │ / 面板 UI     │   ←───────────────────────────────────────────────
   └───────────────┘   {"result": {task…}} / tasks/get 轮询
```

**第 ① 步是 registry 抓我们，不是我们推卡片。** ADP 的立身之本就是这一条：
`POST /v1/agents` 只交一个 URL，registry 自己去 `GET`（`adp-core.md` §5.2、
OACP `README.md:78-80`「The agent never sends a card body」）。因此：

> **openthree 必须自己跑一个 HTTP 服务**：ADP 明确规定「Registry 从不发 A2A 请求」
> （`adp-core.md` §6.4、I1），OACP 也明说不代理 A2A 流量。指望面板替我们收任务是不成立的。

这也是「为什么必须新增一个服务端，而不能只写一个 function」的答案：
function 是**步级**的（一次调用、一次返回），而这里要的是**进程级**的常驻服务
（有人连进来就得在）。它属于「二」的机械层入口，与微信 / 钉钉频道同一类。

---

## 二、定位：不是新内核，是第 5 类频道

`channels/base.py:8-9` 已经把这道判据写死了：

> 加一个新渠道 = 写一个 `Channel` 子类 + 在 `config._SPECS` 里登记它的配置项，
> 不用碰 agent / runtime / llm 任何一行。

A2A 完全落在这条判据之内：

| 频道契约（`base.py:72-82`） | A2A 频道怎么实现 |
|---|---|
| `run()` 阻塞收消息 | 起 `http.server.ThreadingHTTPServer`，`serve_forever()` 到 `stop()` |
| `send_text(msg, text)` 把回答送回去 | 把 `text` 写进 `msg.reply_token` 指向的那个 task，状态置 `completed` |
| `stop()` 几秒内退出 | `server.shutdown()` + 停注册线程 |

「先 ack 再干活」这条判据在钉钉那里是 3 秒内回 ack 帧（`dingtalk.py:220-224`），
在 A2A 这里是**立刻返回 `working` 的 task、跑完再让客户端 `tasks/get` 取结果** ——
同一件事的两次翻译，理由都是「智能体可能要跑几分钟，而对方等不了」。

---

## 三、我们对外承诺什么（线协议）

### 3.1 两个端点

| 路径 | 方法 | 作用 |
|---|---|---|
| `/.well-known/agent-card.json` | GET | 返回 AgentCard（registry 探测抓的就是它，§6.3 校验要过） |
| `/a2a` | POST | JSON-RPC 2.0：`message/send`、`tasks/get`、`tasks/cancel` |

只监听 GET 这两个路径 + POST `/a2a`，其余一律 404。不需要 CORS、不需要静态文件。

### 3.2 AgentCard

必须同时喂饱两拨人：**ADP 的严格校验**（`adp-core.md` §6.3 要求
`supportedInterfaces` 非空、`capabilities` 为对象、`defaultInputModes/OutputModes` 非空、
`skills[].id+name`）与 **OACP 的方言判定**（`adp/card.py:216-225`：看
`protocolVersion` 前缀，`0.` → v0.3，`1.` → v1）。

```json
{
  "name": "<工作区 identity.json 里的名字，没有则 openthree>",
  "description": "<工作区 identity.json 里的格言 motto；没写则回退成固定说明>",
  "version": "<openthree.__version__>",
  "protocolVersion": "0.3.0",
  "url": "http://127.0.0.1:9000/a2a",
  "supportedInterfaces": [
    {"url": "http://127.0.0.1:9000/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "0.3.0"}
  ],
  "capabilities": {"streaming": false, "pushNotifications": true},
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {"id": "openthree.task", "name": "执行任务",
     "description": "把一句话交给这个智能体，它跑完 ReAct 循环后把结论写进 artifact。",
     "tags": ["openthree", "agent", "task"]}
  ]
}
```

三处细节都不是装饰：

1. **`url` 与 `supportedInterfaces` 同时写。** ADP 严格模式只认后者（§6.3），
   而老 A2A 客户端只认前者。两边都写、都指向同一地址，`adp/card.py:151-195`
   的 `interfaces()` 会各取一次、`pick_endpoint` 选到 `JSONRPC` 那个，不产生歧义。
2. **`protocolVersion` 钉在 `0.3.0`。** 决定 OACP 用哪套方法名与 message 形状
   （`client.py:33-36`、`293-316`）：0.3 发 `message/send` + `role:"user"` +
   `parts:[{"kind":"text"}]`。选 0.3 而不是 1.x，是因为 1.x 的 proto 形状
   在野环境里更少、且我们的响应一律回 0.3 形状（见 §3.4）。
3. **`name` 与 `description` 每次 GET 现读 `identity.json`。** 名字取 `name`，
   `description` 取 `motto`（格言）—— 那是智能体对自己的定义；没写格言时回退
   固定说明（`DEFAULT_CARD_DESCRIPTION`），卡片上不出现空 description。两者共用
   一条读取路径，所以改格言与改名一样，下一轮 registry 探测就会看到 `cardHash`
   变化（ADP 的 `changedAt` / `prevHash` 正是为这种漂移准备的）。一次小文件读，
   换「它被别人看见的名字就是它自己的名字」。
4. **`skills` 写死一条，是有意的。** 卡片暴露的是**对外的接口**（"你能让它干什么"），
   不是**实现里的工具清单**。把自造 functions 的清单塞进卡片，等于把本机的
   提示词与工具名广播到公网 registry，还会随每次 `write_function` 抖一次 `cardHash`。
   一句话：**卡片说「我接任务」，不说「我有哪些工具」。**

**`capabilities.streaming: false` 要当真**：`message/stream`、
`tasks/pushNotificationConfig/*` 一律回 `-32601`。OACP 自己也不做流式
（`README.md:251`「No streaming (`message/stream`, `tasks/resubscribe`)」），
先别为没人用的东西写 SSE。

### 3.3 `message/send` 的两种走法（`blocking` 语义）

`MessageSendConfiguration.blocking` 在 A2A 0.3 里是「**是否阻塞到终态**」的显式意愿位，
规范缺省 `false`。我们的缺省是 `not push`：**没登记回调地址才等，登记了就按规范立刻 ack**。

判据只有一条：**这个调用方拿得到答案吗？**

```
收到 message/send
  ├─ 建 task（内联回调地址与任务一起登记），塞进 AgentBridge 队列
  ├─ configuration.blocking 显式给了 → 照办（含「登记了回调但仍然要等」）
  ├─ 缺省 + 登记了回调地址           → 立刻回 state=working
  │     答案稍后由我们 POST 到那个地址（§3.6）
  └─ 缺省 + 没登记                   → 最多等 N 秒（--a2a-block-wait，默认 45）
        ├─ 期间跑完 → 回 state=completed（带 artifact）
        └─ 超时     → 回 state=working（客户端自己 tasks/get 来取）
```

这两个分支是同一个决定的两面，不是两条规则：等的唯一理由是「回得早就等于把结论丢进
没人读的地方」，而回调地址正是让这个理由消失的东西。

**依据（2026-11-09 实测，真 OACP 0.2.1）**：OACP 的客户端不传 `configuration`
（`client.py:106-113` 只放 `message` 与 `metadata`），而它的面板**从不轮询**：

- `web/app.js:1124` 的 `quietTasksRefresh()` 只重读 `GET /api/panel/tasks` —— 那是它自己
  SQLite 里的账本，不是 agent。
- `tasks.py:123` 的 `refresh()`（真的发 `GetTask`）只在用户点「从智能体刷新」时被调用。
- 服务端没有任务级的后台同步（只有 agent 的探活循环）。

于是「缺省回 working」在这条链上的结果是：任务 5 秒就跑完，面板上永远「执行中」——
结论就在我们自己这边的 `tasks/get` 里，但没有任何人会去取。缺省改成「等」之后，答案
直接落在 `message/send` 的响应里，面板一次 POST 就显示完成（实测 2.9 秒）。

代价有界、也有出口：最多等 `--a2a-block-wait` 秒（**参数**而非常量 —— 对面把超时改成
300 的人，这边调到 240 就对了），且它必须**小于**对面客户端超时（OACP 默认 60），
否则会把一个一定能返回的 `working` 拖成对方眼里的连接超时。有能力轮询的调用方写
`blocking: false`，就立刻拿到 ack、自己 `tasks/get` 去取。

### 3.4 返回的 task 对象（一律 0.3 形状）

```json
{"kind": "task", "id": "t-…", "contextId": "c-…",
 "status": {"state": "completed", "timestamp": "2026-11-06T16:00:00Z",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": "<答案>"}]}},
 "artifacts": [{"artifactId": "…", "name": "answer",
                "parts": [{"kind": "text", "text": "<答案>"}]}],
 "history": [ {"role": "user", "parts": [...]}, {"role": "agent", "parts": [...]} ]}
```

形状是按**对方的读法**倒推的，不是按规范抄的：

- `adp/card.py:317-353`：判 `"status" in result and "id" in result` 才当 task，
  文本只从 `artifacts[].parts[].text` 取 —— 所以答案**必须**进 artifacts，
  `status.message` 只是给人看的补充；没有 artifact 的 task 在面板里是一片空白。
- `adp/card.py:257-275`：`state` 会被归一（`TASK_STATE_COMPLETED` → `completed`），
  所以回 `completed`（小写连字符）对两种方言都安全。
- `tasks.py:40-87`：面板先记一条 `state=submitted` 的本地任务，再用我们的返回覆盖；
  非终态也存得下，所以「回 working」不会让它出错，只会等用户在 UI 上点刷新
  （`tasks.py:117-131` → `tasks/get`）。

失败时也带 artifact（正文是错误摘要），并把 `status.state` 置 `failed` ——
面板不显示没有 artifact 的失败原因，那等于让人对着空白猜。

### 3.5 方法表与错误码

| JSON-RPC 方法 | 别名（v1 方言） | 参数 | 返回 |
|---|---|---|---|
| `message/send` | `SendMessage` | `{message:{text…, parts, contextId?, taskId?}, configuration?, metadata?}` | task |
| `tasks/get` | `GetTask` | `{id, historyLength?}` | task |
| `tasks/cancel` | `CancelTask` | `{id}` | task |
| `tasks/pushNotificationConfig/set` | `CreateTaskPushNotificationConfig` / `SetTaskPushNotificationConfig` | `{taskId\|id, pushNotificationConfig\|taskPushNotificationConfig\|config:{url, token?, authentication?}}` | `{taskId, pushNotificationConfig}` |
| `tasks/pushNotificationConfig/get` | `GetTaskPushNotificationConfig` | `{taskId\|id}` | `{taskId, pushNotificationConfig}` |
| `tasks/pushNotificationConfig/delete` | `DeleteTaskPushNotificationConfig` | `{taskId\|id}` | `{taskId}` |
| `tasks/pushNotificationConfig/list` | `ListTaskPushNotificationConfig` | — | `-32004`（一个 task 只有一条配置） |

收两种方法名、两种 message 形状（`role:"user"` 与 `role:"ROLE_USER"`、
`parts[].kind=="text"` 与裸 `{text}`）是**十行的事**，换来的是
「OACP 想用哪套方言都通」以及 `client.py:38-39` 那套自动重试永远不会踩空。
回调那三个方法同理：0.3 的斜杠名与 1.x 的驼峰名都收，**语义只有一套**。

**收图扩展了 part 的解析，artifact 的输出形状没变**：`message_parts()` 把三套线形状
（0.3 `kind:"file"` / 老 proto-JSON `type:"FilePart"` / 1.x 无判别符的 `raw`）统一翻成
`{bytes, uri, mime, name}`，图片落 `.openthree/assets/` 后交给 `Runtime.run(images=…)`；
回复仍然只是 `artifacts[].parts[].text` 里的一段文本（**出站不回图**：面板只读 text，
回 file part 会让它一片空白）。细节见 `docs/a2a-image-input-proposal.md`。


| 码 | 何时 | 备注 |
|---|---|---|
| `-32700` | 不是合法 JSON | |
| `-32600` | 不是合法 JSON-RPC（含批量数组：v1 不支持） | |
| `-32601` | 未知方法（含 `message/stream`） | OACP 会据此换方言重试，所以别乱用 |
| `-32602` | `params.message` 不是对象 / 既没有文本也没有可接收的图片 / 回调地址不是 http(s) 绝对地址 / 缺 `pushNotificationConfig` | 坏地址当场拒，见 §3.6 |
| `-32001` | task 不存在（A2A 标准 `TaskNotFoundError`）；`get` 时也用于「这个 task 没有登记回调地址」（消息里说清是配置没有） | 与 `echo_agent.py:363` 一致 |
| `-32002` | task 已终态，不可取消（`TaskNotCancelableError`） | |
| `-32004` | 不支持的 operation（`UnsupportedOperationError`） | 如 `tasks/resubscribe`、回调 `list` |

HTTP 层：`Content-Length > 8 MiB` → 413（先看长度再读，别先读进内存；8 MiB
是因为图片以 base64 传，膨胀 4/3 —— 1 MiB 连一张 700 KB 的原图都装不下）；坏 JSON
仍是 200 + JSON-RPC error（更符合客户端期望，`client.py:184-220` 两种都处理）。
单张图另有上限（默认 10 MiB，`OPENTHREE_MAX_IMAGE_BYTES`），两道闸取小的那个。

### 3.6 回调（push notifications）—— 我们主动把结论送回去

A2A 定义的顺序是这样的（`push.py:1-21` 那套流程，跟我们无关的部分不抄）：

1. 客户端看卡片 `capabilities.pushNotifications` 为真，才会给地址；
2. 地址以 `{url, token}` 的形式递过来，两个位置都合法：
   **内联**在 `message/send` 的 `configuration.pushNotificationConfig`（1.x 叫
   `taskPushNotificationConfig`），或者事后 `tasks/pushNotificationConfig/set`
   （1.x 叫 `CreateTaskPushNotificationConfig`）；
3. 任务**状态或产物变化**时，我们把**整个 Task 对象** `POST` 到那个 url；
4. 对面按自己的意思做幂等与单调处理（迟到的不该让任务倒退），失败可以重试。

我们做的事与不做的事：

| 项 | 我们的做法 | 为什么 |
|---|---|---|
| 卡片能力位 | `capabilities.pushNotifications: true` | 不置它，客户端根本不会递地址 —— 收了配置却宣告不支持，等于给自己造一个「说好了会回调」的谎 |
| 内联配置 | 与任务**同一把锁**里登记，赶在 `emit()` 之前 | 任务可能比回包还早跑完，那一刻它必须已经知道往哪儿送（§3.3 那个 race） |
| 登记时机 | 立刻生效，不等终态 | 任务此刻可能还在跑，正好赶上 |
| 触发点 | **只在终态**（`completed` / `failed` / `canceled`，含「被挤出表」「服务退出」） | 我们观察得到的迁移只有 `submitted → working → 终态`，而 `working` 那一刻调用方手上已经有了（就在 send 的响应里）。规范说「状态或产物变化时」，这是允许的收窄 |
| body | `tasks/get` 的**同一个视图**（`kind/id/contextId/status/artifacts/history`） | 对面不用为回调写第二套解析；测试里断言两者逐字节相等 |
| token | 请求头 `X-A2A-Notification-Token`；`authentication.credentials` 原样进 `Authorization` | token **不进 Task 对象**：`tasks/get` 里搜不到它 |
| 线程 | 一条 FIFO 工作线程 + 队列，`message/send` 只入队 | 接收端挂了也不能拖慢回包；单线程保证同一个 task 的结论**按顺序**送达 |
| 重试 | 最多 4 次，退避 0.5/1/2 秒；`5xx`、`429`、连不上才重试 | 其余 `4xx` 是**对方明确拒收**，重试只是把同一个拒绝重放几遍 |
| 重定向 | **不跟随**（`_NoRedirect`） | 地址本来就是调用方给的，再让它链式改一次，我们就成了替它打内网的跳板 |
| 响应正文 | 最多读 4 KiB | 我们只关心状态码；对面回一个几 GB 的正文不该把内存吃干净 |
| 日志 | 只打 `scheme://host/…` | 一次性 token 常常直接做路径段（OACP 就是 `/a2a/push/<token>`），日志不该复述它 |
| 收尾 | `stop()` 先 `_fail_all`（把「服务退出」入队）再排空队列，上限 5 秒 | 退出时那条结论也是结论 |

**坏地址当场拒**（`-32602`）：`ftp://` / 相对路径 / 缺 host 的地址如果静默收下，
我们就承诺了「结果会送到」而永远不会有人收到 —— 对方看到的正是「任务一直执行中」，
也就是这个功能要消灭的那种症状。宁可让它在登记这一刻失败。

诚实限制（README 与 §9 同步）：

- **地址是调用方给的，我们不设白名单**：它可以把我们指到任何 `http(s)` 地址。送出去的
  内容就是它自己那份任务的结论（不是凭据），但「POST 到哪儿」这件事确实由对方决定。
  内网部署下这就是它的用途；面向公网时请把它当成一条需要你知情的通道。
- **送不到就没有第二次机会**：重试用完仍失败时，结论只在 `tasks/get` 里（日志会留一条
  「拒收 / 连不上」）。我们没有「回调失败后改走别的通道」这套设计。
- **一个 task 一个地址**：`set` 是替换不是追加；`follow up` 复用同一个地址（配置跟着
  task 走，不跟着这一轮走）。

---

## 四、注册、心跳、退订（对 registry 的调用）

用 `channels/_http` 吗？**不用默认的 `urlopen`** —— 它会走 `HTTP_PROXY` 环境变量，
而我们要打的是 `127.0.0.1`。`echo_agent.py:219-221` 已经因为这个把
`trust_env=False` 写死了。所以 a2a.py 自带一个 `ProxyHandler({})` 的 opener，
函数形状照抄 `_http.request_json`（超时是参数、错误抛 `HttpError`）。

| 时机 | 调用 | 处理 |
|---|---|---|
| 启动 | 有存过的 `agent_id` **且** `card_url` 与本次一致 → `GET /v1/agents/{id}` | 200 → **跳过注册**（省限流额度）；404 → 去注册 |
| 启动（无 id / 404 / card_url 变了） | `POST /v1/agents {"url": 卡片地址}` | 201 → 存 `id` + `manageToken`；200 → 只更新 `id`（token 不轮换，ADP §5.2） |
| 启动（`card_url` 与上次不同，且旧条目能删） | `DELETE /v1/agents/{旧id}` + `X-ADP-Token: 旧token` | 204 → 清掉旧记录；401/404/连不上 → 打印一行「旧条目 a_xxx 未清理」，继续跑（见本节末「注册条目的寿命」） |
| 每 60 秒 | `GET /v1/agents/{id}` | 404 → 重新注册；5xx / 连不上 → 退避重试 |
| 退出 | 默认**不**注销 | 见下 |

`card_url` 必须一起存、一起比：`id` 是**旧地址**派生的（§4.1）。用户改了端口或
`--a2a-advertise` 之后，旧的 `id` 一样会 `GET` 回 200，于是我们高高兴兴跳过注册，
而 registry 里那条记录指向一个已经不存在的地址 —— 探测一路失败到 `down`，
新地址却从未注册。这个症状很难从面板上认出来（「它就在那儿，但是 down」），
所以宁可多存一个字段。

三个判断值得写进注释：

1. **启动先 GET 再 POST**，是因为 ADP 对注册按来源 IP 限流 20 次/小时
   （`adp-core.md` §5.6、OACP `OACP_REGISTER_PER_HOUR` 默认 20）。
   一个秒退秒起的开发循环，用「每次都 POST」能在半小时内把自己限死。
2. **端口不再固定，所以 `id` 会漂**。ADP 的 `id = "a_" + sha256(归一化 URL)[:16]`
   （§4.1、`oacp/net.py:183-188`），卡片地址一变就是**另一个条目**。人类裁决要
   自动探测端口（§5.4），于是「一个 URL 一个条目」这张底牌没了 —— 偿还方式
   见本节末「注册条目的寿命」：**先尽量不漂**（复用上次端口），**真漂了就收尸**
   （删掉旧条目）。不接受这笔债的唯一办法是改协议，那不在我们的权限里。
3. **默认不注销**。注销是硬的、不留墓碑（§5.5），而重启后同一 URL 重新注册会被
   「先到先得」地再拿一个新 token（I3 推论 2）；更要紧的是：进程崩了就不会有注销调用，
   于是「有时有、有时没有」比「从来没有」更难解释。要注销，手动一条 curl 即可。

退避曲线照抄 `echo_agent.py:36` 的 `1 → 2 → 4 → 8 → 15 → 30` 封顶；
失败**不挡服务**：注册不上的时候频道照常收 A2A 请求（`--a2a-no-register` 就是这条路的极端形式）。

### 注册条目的寿命：先复用端口，再给旧条目收尸

端口自动探测（§5.4）意味着 `card_url` 可能在两次启动之间变。`id` 由 URL 派生，
所以每次变都是**新条目**，旧的那条不会自己消失（ADP 没有 TTL，只有探测失败累积成 `down`）。
两件事按这个顺序做：

**一、先尽量不漂。** 上次**成功 bind** 的端口存在
`.openthree/channels/<频道名>.json` 的 `last_port` 里（bind 成功就写，与注册成败无关 ——
`--a2a-no-register` 下也要能复用端口），**自动模式下**排到候选序列最前面试（显式端口时
它不参与，§5.4）。正常重启（上个进程已退、端口空着）就复用同一端口 → 同一 `card_url`
→ 同一 `id` → 启动走「`GET` 200，跳过注册」。**只有端口真被别人占住时才漂** ——
而这正是人类要求自动探测的那个场景。

显式端口（§5.4）不在这套机制里：端口由人钉死，`card_url` 天然稳定，**不存在漂移**，
因此也不会有第 2 步的收尸 —— 除非人**改了**那个端口（那是一次明确的配置变更，
按下面的规则处理）。

**二、真漂了就收尸。** 本进程拿到新端口、注册出新 `id` 之后，若存过的 `card_url`
与本次不同，就用**存过的旧 `id` + 旧 `manageToken`** 发 `DELETE /v1/agents/{旧id}`：

| 结果 | 含义 | 处理 |
|---|---|---|
| 204 | 旧条目已删 | 清掉旧记录，只留新的一对 |
| 401 | token 不是首注册者的（有人抢先注册过同一个旧 URL） | 打印「旧条目 a_xxx 我们无权删除，可在面板里手动删」，继续 |
| 404 | 旧条目本来就不在了 | 清掉旧记录 |
| 连不上 registry | 注册这条路本来就没通 | 跳过；注册失败不挡服务 |

**收尸前先探活旧地址**：对旧 `card_url` 发一次 `GET /.well-known/agent-card.json`，
**拿到任何 HTTP 响应就不删** —— 说明那个端口上还有活的东西在服务（很可能是另一个
openthree 实例，它注册的是它自己的 URL，而 URL 恰好相同）。此时删掉会毁掉一个
活实例的注册记录，比留一条僵尸条目糟得多。只有连接失败（那个端口真没人听）才删。

**为什么不能更省事**：`POST` 一个**已存在**的 URL 返回 200 且 `manageToken` 为 `null`
（`routes.py:52-53`、`service.py:103-110`，token 不轮换）—— 所以「重复注册旧地址把
token 换回来」这条路不通。能删的只有我们自己第一次注册时存下 token 的那条；
别人抢先注册的同 URL 记录只能人工清。

---

## 五、配置与 CLI

### 5.1 `-A2A` 是开关，地址是另一个参数

```bash
openthree -A2A                       # 起服务 + 注册到 http://127.0.0.1:8000（本机 OACP）
openthree -A2A --a2a-registry http://10.0.0.5:8000
openthree -A2A -i                    # 控制台 + A2A 并存（与其它频道共用一个会话）
openthree -A2A --a2a-no-register     # 只当普通 A2A 服务端（调试 / 手写注册）
```

**为什么 `-A2A` 不直接吃地址**（这一点是踩出来的）：主解析器里 `task` 是
`nargs="*"`（`cli.py:95`）。如果 `-A2A` 写成 `nargs="?"` + `const=""`，
那么 `openthree -A2A 分析一下这个仓库` 会把**任务文本当成 registry 地址**收走，
而用户看到的只是「任务凭空消失」。开关归开关、地址归地址，这条歧义就不存在。
（`-A2A=<url>` 这种写法 argparse 虽然认，但它依赖一个没写进文档的解析细节，不值得押。）

| 参数 | 默认 | 说明 |
|---|---|---|
| `-A2A` | 关 | 本进程额外起一个 a2a 频道（**不写** channels.yaml） |
| `--a2a-registry` | `http://127.0.0.1:8000` | registry 基址；也读 `OPENTHREE_A2A_REGISTRY` |
| `--a2a-host` | `127.0.0.1` | 监听地址。**不能是常量**：要被别的机器调用就只能改它（改成 `0.0.0.0` 时打印警告，§8.2） |
| `--a2a-port` | 自动（9000–9999） | **给了就用这一个**：不探测、不复用 `last_port`、被占报错退出（§5.4）。也是让 registry 里 `id` 跨重启不变的唯一手段。`channels.yaml` 的 `port` 与之同等地位 |
| `--a2a-advertise` | 由 host/port 推导 | 卡片里 `url` 的基址；`0.0.0.0` → `127.0.0.1`（同 `echo_agent.py:366-372`）。**不能是常量**：跨机器注册时地址必须是对方能抓到的那个（§8.6） |
| `--a2a-no-register` | 关 | 不向 registry 注册 |
| `--a2a-block-wait` | `45` | `message/send` 最多等几秒把答案带回响应（0 = 不等，立刻回 `working`）；缺省就等，显式 `blocking:false` 才立刻 ack（§3.3） |

### 5.2 与既有启动模式的关系

| 组合 | 行为 |
|---|---|
| `openthree -A2A`（终端） | 默认进 `-i`，A2A 频道随其它频道一起自动加载（`cli.py:526-539`） |
| `openthree -A2A`（非终端 / 服务） | **等价于 `-d`**：不读 stdin，只等任务 |
| `openthree -A2A -d` | 同上，显式写法 |
| `openthree -A2A "任务"` | **报错**（与 `-d` 同一条判据，`cli.py:314-318`）：服务随进程退出，跑任务没有意义 |
| `channels.yaml` 里已有 `type: a2a` | `-A2A` 用本次参数**替换**它并打印一行提示；没有 `-A2A` 则按配置起 |

最后一行是「同一件事不能写在两个地方」的具体执行：配置文件是持久真源，
`-A2A` 是**本进程的**临时注入，两者撞名时前者被替换而不是叠加。叠加的后果不是
`OSError`（端口会自动探测，起得来）而是**同一进程里两个 a2a 频道共享一个端点**：
第二个抢不到端口就漂到下一个，于是同一份任务入口被注册成两条 registry 记录 ——
比报错更难查。

「替换」说的是**条目**（同类型只留一个实例）；**字段级是合并** —— 命令行给了的键
覆盖配置，没给的沿用配置。否则 `channels.yaml` 里写过的 `registry` 会因为这次
`-A2A` 没重复说一遍而消失，那等于替用户改他自己的配置（§5.4 对端口的那句
「两个值都是人写的，挑哪个都是替用户改配置」是同一条判据）。

### 5.3 `channels.yaml` 里长这样

```yaml
channels:
  - name: a2a
    type: a2a
    options:
      registry: http://127.0.0.1:8000
      host: 127.0.0.1
      # port 留空 = 自动探测 9000–9999（§5.4）；写死 = 就用这一个，被占则报错退出
```

`config._SPECS` 只加一条（`config.py:66-99` 那张表）。三条约束：

1. **没有 `required=True` 的字段** ⇒ `is_ready()` 立刻为真（`config.py:145-154,163-164`），
   向导（`channels/cli.py:164-176`）逐项回车就能生成一条可用配置。
2. **默认值只写一份**：向导把非空输入才写进 `options`（`cli.py:172-174`），
   而**空值 = 用实现里的默认**（`Channel.option(key, default)`，`base.py:95-97`）。
   所以默认值住在 `a2a.py` 的模块常量里，`_SPECS` 的 `Field` 只写 `hint`（说明默认是什么）
   而**不留空默认值也不复制一份** —— `config.py:5-8` 明说不 import 具体频道实现，
   为了共享常量去 import 会破坏那条判据。
3. `_ALIASES`（`config.py:113-117`）**不是**为大小写准备的 —— `normalize_type` 已经
   `.lower()` 过了；要加的是人嘴里可能冒出来的近义写法（如 `adp`）。
   若只写 `a2a` 也完全够用，别为了「多认几个写法」把这张表变成同义词垃圾场。

### 5.4 端口：显式就听显式的，否则按候选序列 bind

人类裁决有两条，**它们是两种模式，不是一条序列上的先后**：

| 模式 | 什么时候 | 监听端口怎么定 | 被占时 |
|---|---|---|---|
| **显式** | 人写了 `--a2a-port`，或 `channels.yaml` 的 `port` 非空 | **就是那一个**，不构建候选序列、不读 `last_port`、不降级 | **报错退出** |
| **自动** | 两个来源都没给 | `last_port` → 9000 → 9999 升序 | 继续下一个；全占满报错 |

来源判据一句话：**只要有一个是人写下的值，就是显式模式**。CLI 与配置文件在这里
地位相同（配置是持久真源，`-A2A` 只是本进程注入且撞名替换，§5.2）—— 不能实现成
「只有 CLI 给的才算显式」，否则同一条判据在两条启动路径上答案不一样。
端口**没有环境变量**（不像 `--a2a-registry` 有 `OPENTHREE_A2A_REGISTRY`）：
多一个来源就多一处「这值到底从哪来」，而端口这件事在命令行与 yaml 里已经够写。

**「指定了就用指定的」= 被占就失败，不是换个能用的。** 端口被占时唯一的诚实做法是
报错退出：悄悄漂到 9001 会让用户以为「我指定了 9000」，而 registry 里注册的是 9001 ——
两个地址、两个 `id`，且用户手里没有任何线索能发现。所以显式模式下失败是**成功**：
它把一次静默的身份漂移换成一句明确的错误。

实现判据（必须写进代码注释）：**显式时不要构造列表再取第一项**。写成
`ports = [explicit] + [last_port] + range(...)` 再循环，等于把「降级」写进了结构里，
下一个人顺手删掉循环里的 `raise` 就静默回归了。正确形状是两条互斥的路径：

```python
if explicit is not None:
    return _bind_one(explicit)        # 只试一次，失败直接抛；last_port 不参与
for port in ([last_port] if last_port else []) + range(9000, 10000):
    ...
```

显式模式的**真正收益**是身份稳定：`card_url` 跨重启不变 ⇒ `id = a_ + sha256(url)[:16]`
不变（§4.1）⇒ registry 里始终是同一条记录。自动模式只能靠 `last_port`「尽量」不漂；
**要让 `id` 长期不变，唯一的手段就是显式指定端口**。这句话该写进 README 的端口一节，
否则用户会以为显式端口只是「省得探测」的小优化。

### 5.4.1 自动模式：按候选序列 bind，而不是「先探测再 bind」

人类裁决：**自动探测 9000–9999**（固定 9100 会撞 —— 示例 agent 也用 9100）。这里有一条
必须写进代码注释的实现判据：**不写「探测函数」**，而是**按候选序列去 bind 真正的 server，
第一个成功的就用**。

理由是竞态：探到空闲 → 别人抢先 → 我们的 bind 仍会失败。多一个探测函数只是多一条缝
（「探测说空闲却被占」），还多一份要维护的「端口是否可用」判据。
`ThreadingHTTPServer(...)` 本身就是探测：成功即占用，失败即试下一个。

自动模式的候选序列（保序、去重）：

| 顺序 | 来源 | 被占时 |
|---|---|---|
| 1 | `last_port`（上次成功 bind 的那个，`.openthree/channels/<频道名>.json`；bind 成功即写，与注册无关） | 继续第 2 步 |
| 2 | 9000 → 9999 升序 | 全占满 → 报错退出 |

第 1 步是「别让自己漂」的关键：正常重启复用上次端口 ⇒ 同一 `card_url` ⇒ 同一 `id`（§4）。

**只有自动模式才会「换下一个」。** `OSError` 的 `errno` 属于 `EADDRINUSE`
（POSIX `98`、Windows `10048`）才算「被占」；`EACCES`（端口 < 1024 且非管理员）
**必须直接抛** —— 否则 1000 个候选全失败，用户只看到「9000–9999 全占满了」，
而真实原因是权限。错误信息要把「试了哪些端口、为什么每个都不行」说清。
显式模式没有「下一个」，所以它只需要把「这个端口为什么不行」说清（P2-3 给的就是那句话）。

**平台坑（这段必须进代码注释）**：`http.server.HTTPServer` 默认
`allow_reuse_address = 1`，即 `SO_REUSEADDR`，**两个平台的语义不一样**：

| 平台 | `SO_REUSEADDR` 的实际效果 | 我们要的 |
|---|---|---|
| POSIX | 只允许 bind 到处于 `TIME_WAIT` 的地址；**不能让两个 listening socket 共用同一端口** | 保留（否则紧接着的重启会撞 `TIME_WAIT` → 触发漂移 → 白收尸一次） |
| Windows | **允许 bind 一个正在被监听的端口**（要拒绝得用 `SO_EXCLUSIVEADDRUSE`） | **必须关掉** |

所以不能一刀切写 `False`：那会把「端口冲突」从静默变可见的同时，又给 POSIX 造出
「重启即漂」。正确写法是一行、且必须带注释：

```python
allow_reuse_address = not sys.platform.startswith("win")  # Windows 的 SO_REUSEADDR 会抢占已监听端口
```

Windows 上关掉之后就是系统默认行为（不设 `SO_REUSEADDR` 即拒绝重复绑定），
于是冲突会明确报 `EADDRINUSE`、由候选序列去试下一个 —— 这才是我们要的失败信号。

**`--a2a-port 0` 不是特性**：交给系统分配，必然每次漂、每次一个新 `id`。
实现上允许（测试要用），但只在 `--a2a-no-register` 下有实用价值，不写进 README 的常规用法。

**`--a2a-advertise` 与端口的关系，两种模式判法不同**：

- **显式端口**下不存在漂移，只有**自相矛盾**：advertise 里写死了端口且 ≠ `--a2a-port`
  → **报错退出**。两个值都是人写的，挑哪个都是替用户改他自己的配置；能做的只有指出来。
  （advertise 不带端口、或只写主机名，则用显式端口补全 —— 那不是矛盾。）
- **自动模式**下 advertise 只能由实际端口推导；若人写死了带端口的 advertise 而端口漂了，
  卡片地址就是假的（registry 抓不到）→ 打印警告，不阻塞（此时没有「正确答案」可报错）。

---

## 六、任务与运行时的接法

A2A 频道不自己跑模型，它把消息交给 `AgentBridge`（`bridge.py:91-98`）：

```
HTTP 线程: message/send
   → 建 task（submitted）+ 组 InboundMessage（reply_token = task id）
   → Channel.emit(msg) → manager 注入的 handler → bridge.submit()
   → 等到答案就回 completed，否则到窗口上限回 working（§3.3）
bridge 工作线程（串行）
   → compose_input(msg)   ← 「来自A2A[…] 的消息，请直接回复：\n\n<正文>」
   → rt.run(text, session=True)      ← 与 -i 里敲一行字**完全同一条路**
   → channel.send_text(msg, answer)  → 找到 task，写 artifact，state=completed
```

由此得到的一组性质，都是既有设计的直接后果，不需要新机制：

- **串行**（`bridge.py:9-13`）：多个 A2A 任务 + 控制台共享一把锁，一次只跑一条 ReAct。
  机器发的任务密度比人高，所以这一点要**写进文档**：并发 10 个任务时，
  第 10 个会在队列里等到前 9 个跑完，期间状态一直是 `working`。
- **共享会话（已裁决，不是待改项）**：与钉钉 / 微信一样，A2A 任务并进同一个进程内
  会话。人类 2026-11-06 的判据是「这就是个特殊的频道，不动内核」—— 所以
  `Runtime.run()` 不加 `isolate`，`-d` 的 `begin_session()` 语义原样保留
  （`runtime.py:265-270`：`_session_active` 为真时单传 `session=False` 不隔离）。
  好处是「同一个终端里轮流打字」；代价是远端能用一句提示读到本会话最近的对话，
  这句话必须原样进 README（§9）。
- **来源可见**：`InboundMessage.label()`（`base.py:36-42`）的映射表要加一项
  `"a2a": "A2A"`；`user_name` 填「外部智能体」，`user_id` 填调用方 IP，
  `conversation_id` 填 `contextId`，`msg_id` 填 task id。

### 6.1 `tasks/cancel` 的诚实语义：不再等，不是停手

`Runtime.run()` 没有中断点（ReAct 每步都在等模型返回），所以：

- 任务还在队列里没开跑 → 直接置 `canceled`，跑完的结果丢弃（bridge 里没有
  「按 id 出队」的接口，而为了这个去改 bridge 不划算）；
- 任务正在跑 → 只是把 A2A 侧状态置 `canceled`，**模型仍然会跑完**，结果写进日志不写进 task。

这条必须写在 README 与 docstring 里，不能只写「支持取消」。
（面板侧 `tasks.py:133-147` 会把我们的 `canceled` 当终态收下，不会出问题。）

### 6.2 三条「答案不是答案」的约定

`send_text` 收到的文本**不一定**是模型跑出来的结论 —— bridge 自己也会往里塞话
（`bridge.py:28` 的收工文案、`bridge.py:142` 的异常文案），两者都以 `[openthree] `
开头。这层约定如果不写下来，A2A 侧就会出现「服务正在退出」被记成一次
`completed` 的成功任务 —— 对面拿到的是一条"成功"的空结论。

所以三条：

1. `bridge.py` 新增模块常量 `NOTICE_PREFIX = "[openthree] "`，两处文案都引用它 ——
   这是**文案**约定（让人一眼看出这不是答案），不是判定依据；
2. a2a 频道判的是**带外标记**：桥在自己的每一句通知上写 `msg.raw["notice"]`，
   频道命中标记 → task 记 `failed`，`status.message` 说明原因。**不按文本前缀判** ——
   模型完全可能把 `[openthree] ` 当作答案的开头（比如它正在解释这个前缀），
   按前缀判会把一条真答案误记成失败回给对面的智能体；
3. `stop()` 里先把自己名下所有未终态 task 统一置 `failed`（"服务退出，任务未完成"），
   之后姗姗来迟的 `send_text` 落到终态 task 上一律忽略。

第 3 条也有一个副作用要接受：`bridge.stop()` 之后再进来的消息会立刻收到
`SHUTDOWN_NOTICE`，此时 task 已经 failed —— 正好，不必单独处理。

### 6.3 同一个 task 内的追加消息

面板的「follow up」会给 `message.taskId`（`tasks.py:89-115`）。v1 的处理：
**已知 taskId → 把该 task 此前的问答压成一段前缀，作为一个新 run 的输入**，
答案追加进同一个 task 的 history。不引入 per-task 会话，也不假装有 context 隔离；
卡片也不声称 context 支持（A2A 没有这个能力位，所以这条只能写进 README）。

---

## 七、状态落盘

| 文件 | 内容 | 为什么落盘 |
|---|---|---|
| `.openthree/channels/<频道名>.json` | `{registry, agent_id, manage_token, card_url, advertise, last_port}` | 与微信 token 同一位置（`README.md:661`）；`agent_id` 是「先 GET 再 POST」省限流的前提，`card_url` 是「地址变了就重注册 / 收尸」的依据（§4），`last_port` 是**自动模式**下「重启别让端口漂」的依据（§5.4） |
| 任务表 | **只在内存**（dict，最多留最近 200 条，一把 `threading.Lock`） | v1 不做持久化：进程重启后 `tasks/get` 回 `-32001`，面板会显示查询失败并保留本地记录 |

两处细节：

- `manage_token` 是**凭据**：只在启动摘要里印 `agent_id`，绝不回显 token；
  它也不该进日志（`logging` 里只出现 id）。
- 那把锁不是防御性代码：HTTP 线程（`message/send` 建 task）与 bridge 工作线程
  （`send_text` 改状态）**必然**并发，`ThreadingHTTPServer` 的每个连接还是独立线程。

任务表不做落盘的原因要说白：落盘就要处理「重启后 task 还在、但那次 ReAct 的上下文没了」，
而这会让 `input-required` 之类的状态变成谎话。宁可让它干净地消失。

---

## 八、安全边界

`-A2A` 的本质是**把「能跑 shell 的智能体」接到网络上**，所以边界要一次说清：

1. **默认只听 `127.0.0.1`。** 与 OACP 的默认绑定一致（`--host 127.0.0.1`），
   两边都只在本机时，卡片地址是 `http://127.0.0.1:<探测到的端口>/…`，正好落在 OACP 的
   local mode 里（`README.md:102-106`：绑定回环时接受 http 与私有地址的卡片 URL）。
   默认走 §5.4 的自动模式（候选序列），所以**文档里不写死端口号**：启动摘要印实际端口，
   卡片地址由它推导；显式给了 `--a2a-port` 时端口就是那个，同样以启动摘要为准（它可能是
   配置里写的，用户未必记得）。
2. **`--a2a-host 0.0.0.0` 时打印一行警告**：任何能连到这台机器的人都能让它执行任务。
   **不做**「token 认证」这个开关：OACP 的客户端不发任何凭据（`client.py:231-238`
   只有 content-type / accept / ua），加了 token 就等于把面板这条主路堵死；
   真正需要鉴权的部署，应该套反向代理（`securitySchemes` 是卡片的事，不是我们 v1 的事）。
3. **入站请求的硬闸**：`Content-Length ≤ 1 MiB`（流式读、超了 413）、
   连接 socket 超时 15 秒、只接受 `POST /a2a` 与两个 GET 路径、
   **只收 `Content-Type: application/json`**（否则 415）。
   最后这条不是洁癖：只听回环**挡不住浏览器** —— 任意网页可以用 `text/plain` 的
   「简单请求」直接 POST 到这里（没有 preflight），DNS rebinding 还能让它变成同源，
   后果是「用你的工具链执行任务并把结论读回来」。要求 JSON 就够：浏览器为它必定先发
   **preflight**，而本服务不实现 `OPTIONS`（落到 501），请求根本发不出来。
   **判据要守住**：将来谁加了 CORS / `OPTIONS` 支持，就必须同时加 `Host` / `Origin`
   校验，否则这道防线会被自己拆掉。
   另外两条同源的收尾：`Transfer-Encoding`（chunked）→ 411（不读走正文就会让
   keep-alive 把残留字节当下一行解析，即请求走私）；带请求体的 GET → 400（同一个洞）。
4. **`SO_REUSEPORT` 关掉**（`allow_reuse_port = False`）：它允许两个监听者同时绑同一
   端口，「显式端口被占就报错」与自动探测都会变成谎话。`SO_REUSEADDR` 已经按平台分叉
   （§5.4），这条是同一个理由的另一半。
5. **回调是唯一一条出站通道，地址由调用方给**：只收 `http(s)`、不跟随重定向、响应正文
   最多读 4 KiB、日志与异常只打 `scheme://host/…`（path 里常常就是 token，userinfo 也
   一并去掉）。**不做白名单**，理由与诚实条款见 §3.6 / §9。
4. **对外抓取的风险不在我们这边**：抓卡片的是 registry（ADP §7 那套 SSRF 规则是它的事）。
   我们只发一个 `POST /v1/agents`，不代用户访问任何 URL。**这一点要写进文档**，
   否则下一个人会以为该在这里做 https 校验。
5. **ADP 的诚实条款要继承**：注册 ≠ 拥有这个 URL（I3）。所以「面板里出现了一个叫
   openthree 的 agent」不能当成身份证明。**这条不会因为「不做签名」而消失** ——
   恰恰相反：我们**不做卡片签名**（人类裁决，不是「v1 不做 v2 再说」），卡片里不写
   `signatures`，于是 registry 里没有任何东西能证明「这个 openthree 是我的」，
   用户能依赖的只有「那个地址是我自己填的」。文档里任何地方都不许暗示「注册过 = 可信」，
   README 也不许把「已经在面板里」写成「可以被信任」。
   （ADP 的签名是给 Consumer 的锚点、registry 不验签 —— 即使将来要加，也不是为了让
   面板更可信。这条是给读者的，不是留给自己的 TODO。）
6. **跨机器时最容易错的一件事：registry 抓的是「它自己眼里的地址」。**
   `-A2A --a2a-registry http://10.0.0.5:8000` 而卡片地址仍是 `http://127.0.0.1:<port>` 时，
   10.0.0.5 会去抓**它自己**回环上的那个端口，注册必然 `422 CARD_UNREACHABLE`。
   所以启动时就该判一次：registry 的 host 不是本机回环、而卡片地址是回环 →
   打印明确警告并指出 `--a2a-advertise`。**端口自动探测让这件事更容易踩**：
   显式写死 `--a2a-advertise http://10.0.0.5:9000` 而实际端口漂到 9001 时，
   卡片地址就是假的 —— §5.4 那条「advertise 里的端口 ≠ 实际端口就警告」正是为它。
   同理，**strict registry 只收 https 卡片地址**（§7.1）：远程注册需要在前面挂一层
   TLS 反向代理，`--a2a-advertise https://…`，代理把我们那两个路径转回本机。
   这两条是**部署知识**，不写进 README 就会变成一堆「注册失败」的提问。

---

## 九、已知限制（诚实清单，v1 就这样）

| 限制 | 后果 | 为什么不现在解决 |
|---|---|---|
| 没有流式（`message/stream`） | 客户端只能轮询 | OACP 自己也明确不做（`README.md:251`） |
| 任务共享一个进程内会话（**已随裁决定案**） | 远端调用方能通过提示让模型复述本会话里最近的对话，反之亦然 | 人类 2026-11-06：「这就是个特殊的频道，不动内核」。隔离得给 `Runtime.run()` 加 `isolate`（`-d` 已 `begin_session()`，单传 `session=False` 会被静默无视 —— `runtime.py:265-270`），那就是改内核，不做。**README 必须原样写出这条泄露**，默认只听回环顶替不了这句话 |
| 并发任务串行 | N 个任务排队，队尾等很久 | `Runtime` 不是线程安全的（`bridge.py:9-13`），这是既有约束 |
| `cancel` 不停手 | 计算照跑 | ReAct 没有中断点 |
| 任务表不落盘 | 重启后 `tasks/get` 是 `-32001` | 落盘会把「上下文已丢」变成谎话（§7） |
| **端口会漂 ⇒ registry 条目 `id` 跟着变**（已随裁决定案） | 面板里可能短暂出现两条同名记录，旧的那条一路 `down` | 人类 2026-11-06：端口有冲突，要自动探测（§5.4）。偿还两件事：优先复用 `last_port`（正常重启不漂）+ 旧条目自动收尸（§4 末）。收不掉的那条（token 不是我们的）只能人工删 —— **README 要写明「端口变了可能在面板里看到重复条目」** |
| 无鉴权 | 谁能连上谁能用 | 见 §8.2；默认只听回环 |
| **回调地址由调用方决定** | 它可以把我们指到任何 `http(s)` 地址（我们就往那儿 POST 那份 Task） | 这是协议本身的样子：地址是客户端给的，不是我们选的。不设白名单的理由是主流部署就是回环（`localhost:8000/a2a/push`），一条白名单会把唯一真实用法挡在外面。我们只做三件事兜底：不跟随重定向、响应只读 4 KiB、日志不复述路径里的 token（§3.6） |
| **回调送不到就只剩 `tasks/get`** | 重试 4 次仍失败时，结论不会再有第二次机会 | 「回调失败后改走别的通道」是另一套设计（多渠道兜底），不在频道层。日志会留一条「拒收 / 连不上」，而结论本身一直在 `tasks/get` 里 |
| 只回 0.3 形状的响应 | 只认 1.x 响应的客户端可能不满意 | 主流客户端都做归一（`adp/card.py:257-275,317-365`）；真遇上了，在 card 里改 `protocolVersion` 就切过去了 |

---

## 十、改哪些文件（不含测试约 600 行）

| 文件 | 改动 | 行数 |
|---|---|---|
| `openthree/channels/a2a.py` | **新增**：卡片构造、任务表、JSON-RPC 路由、HTTP 服务端（端口两态：显式只用给定值、否则候选序列；平台分支的 `allow_reuse_address`）、注册器与收尸 | ~580 |
| `openthree/channels/config.py` | `_SPECS` 加一条 + `_ALIASES` 补两个写法 | ~14 |
| `openthree/channels/base.py` | `label()` 的中文名映射加 `a2a` | 1 |
| `openthree/channels/bridge.py` | 新增模块常量 `NOTICE_PREFIX = "[openthree] "`，`SHUTDOWN_NOTICE`（`:28`）与异常文案（`:142`）都引用它 —— §6.2 的判定靠它做唯一真源 | ~3 |
| `openthree/channels/manager.py` | `from_workspace` / `load_manager` 加 `extra: Sequence[ChannelConfig] = ()` | ~6 |
| `openthree/cli.py` | 7 个参数 + `-A2A` 与 `-d`/任务参数的关系 + 把注入配置传给 `_interactive` **与** `_daemon` 两条路径 | ~45 |
| `openthree/channels/cli.py` | **不改**（向导对任何 `_SPECS` 条目通用） | 0 |
| `README.md` | 「频道」一节加 a2a 行 + 一节完整用法 | ~30 |

**内核不动**：`agent.py` / `runtime.py` / `llm.py` / `prompt.py` / `builtins.py` 一个字节不碰。
这也是本方案能不能成立的第一判据 —— 一旦发现必须改 `runtime.py`，就说明选错了层。
人类 2026-11-06 已把它钉成**裁决**（「这就是个特殊的频道，不动内核」）：原先挂在这里
当候选的「每 task 隔离会话」（要给 `Runtime.run()` 加 `isolate`）因此撤销，
共享会话成为设计本身（§6），它的代价改写进 README（§9）。

---

## 十一、测试计划（`tests/test_a2a_channel.py`，~320 行）

不依赖 OACP/FastAPI，全部用标准库起真实 socket：

1. **假 registry**：`http.server` 回 201/200/404，断言
   ① 首次 `POST /v1/agents` 用卡片地址（不是卡片内容）；
   ② 存过 id 时启动先 `GET`、200 就不 POST；
   ③ `GET` 回 404 → 重新 POST；
   ④ registry 连不上 → 频道照常服务、按退避重试（把退避做成可注入的序列，别真等 30 秒）。
2. **真 A2A 服务端**（绑定 `127.0.0.1:0`，从 `server_address` 取实际端口）：
   `message/send` → 立刻拿到 `working` 的 task id；喂一个假 `send_text` →
   `tasks/get` 变 `completed` 且 artifact 有文本。
3. **协议边角**：坏 JSON → `-32700`；未知方法 → `-32601`；未知 task → `-32001`；
   `content-length` 超限 → 413；`SendMessage` / `ROLE_USER` 别名通；
   批量数组 → `-32600`。
4. **等待窗口**：`--a2a-block-wait 0` 时立刻回 working；再用「缺省就等」验默认分支
   （跑得快的假答案必须进响应），用超时分支验「窗口到点回 working 而不是失败」。
5. **Channel 契约**：`stop()` 后 `run()` 在 5 秒内返回（manager 的 join 上限是 5 秒）；
   候选端口全被占时 `run()` 抛错、`manager._run_one` 把它记成 ✗ 而**不影响别的频道**
   （`manager.py:132-137` 的既有行为）。
6. **可选一致性**：如果 OACP 装在环境里（`import oacp` 成功），额外断言
   `oacp.adp.card.validate_card(card)` 与 `validate_card(card, mode="strict")` 都为空表 ——
   这是「我们的卡片真的能被对方收下」的最硬证据。装不上就 skip，不把它做成硬依赖。
7. **端口：两种模式各测一遍**（§5.4）。
   **自动模式**：裸 socket 占住 9000 → 起在 9001；`last_port` 存着 9001 且空闲 → 起在 9001
   （不是 9000，这条是「别让自己漂」的回归）；候选全被占 → 错误信息里逐个列出试过的端口。
   **显式模式**（本轮新增的三条，缺一条就说明又写回「序列首项 + 降级」了）：
   ① 空闲端口 → bind 成功；② 被占端口 → **抛错退出**；③ **决定性的一条**：
   `last_port` 存着 9001（空闲）而显式 `--a2a-port 9002` 被占 → 必须抛错，
   **不得**起在 9001 —— 它证明显式模式没有降级路径；④ 显式值写成
   `channels.yaml` 的 `port` 时行为相同（两条来源同等地位）。
   再加一条一句话断言：`server.allow_reuse_address == (not sys.platform.startswith("win"))`
   —— 它是「探测不会假成功」的唯一保证（Windows 上默认为真会静默抢占用中的端口，
   而 POSIX 上关掉它会让重启撞 `TIME_WAIT`，两侧都不能想当然）。显式模式下这条同样成立：
   Windows 上若不关它，「被占」永远不会报错，显式端口会以为自己绑上了。
8. **收尸**（§4 末）：`card_url` 与存的不同 → 发 `DELETE`；旧地址仍有 HTTP 响应 → **不发**
9. **回调（§3.6）**：全部用真 socket 的 HTTP 接收端，断言的是「对面收到了什么」：
   ① 卡片 `capabilities.pushNotifications === true`（能力位就是那扇门）；
   ② 内联配置 → 终态后收到 POST，`X-A2A-Notification-Token` 等于登记的 token，
      body 与 `tasks/get` 的 result **逐字节相等**；
   ③ 三个方言名（`tasks/pushNotificationConfig/set` / `CreateTaskPushNotificationConfig` /
      `SetTaskPushNotificationConfig`）各登记一次都能生效；
   ④ `get` 回登记的配置、`delete` 之后置终态**不再有人收到**、`delete` 幂等；
   ⑤ 坏地址（`ftp://` / 相对路径 / 缺 host / 空）→ `-32602`，且任务本身没被带走；
   ⑥ token 不出现在 `message/send` 的响应与 `tasks/get` 里；
   ⑦ 缺省 `blocking` + 有回调 → 立刻 ack（`< 1s`）；显式 `blocking: true` 仍等；
   ⑧ follow up 复用同一个回调地址（配置跟着 task 走）；
   ⑨ `cancel` 也回调（终态就是终态）；
   ⑩ 接收端连不上时 `message/send` 不被拖慢（投递在另一条线程）；
   ⑪ `5xx` 用满重试预算后放弃、`4xx` **只发一次**、`3xx` 不跟随（把退避注入成 0 秒，别真等）。

   （有活实例）；`DELETE` 回 401 → 打印但不退出，频道继续服务。

---

## 十二、落地顺序

1. `a2a.py` 的骨架 + `_SPECS` 条目 → `openthree channel add --type a2a` 能生成配置
2. JSON-RPC 路由 + 任务表（先不注册，用 `--a2a-no-register` 手测 curl）
3. 注册器（GET→POST、404 重注册、退避）
4. CLI 接线（`-A2A` / `-d` 语义 / 注入）
5. README + 测试；`pytest -q` 全绿、`self_test.py` 全绿
6. 端到端：`oacp serve` + `openthree -A2A` → 面板里 1 秒内出现 → 发一条任务 → 刷新看结论

---

## 十三、被否掉的备选方案

| 备选 | 否掉的理由 |
|---|---|
| A. 只做 A2A **客户端**（openthree 去调别人） | 与需求相反：用户要的是「接收任务」 |
| B. 让 OACP 代理 A2A 流量 | ADP 明令 Registry 从不发 A2A 请求（I1、§6.4）；改协议才能做到，代价远大于收益 |
| C. 做成内置 function `a2a_serve()` | 服务要活过 ReAct 循环；function 的生命周期是**一步**。而且工具 schema 会进每一次请求，为一个常驻服务付全场的字节不划算 |
| D. 复用 FastAPI（照抄 `echo_agent.py`） | 频道层只用标准库有明确判据（`channels/_http.py:3-9`：任何装了 openthree 的环境都得能跑）；`http.server` 对「两个 GET + 一个 POST」够用 |
| E. 同一 request 里跑完再回（不用 task） | 会把 A2A 的 task 模型当摆设；超过对方超时就是一整次失败，连任务 id 都不剩 |
| F. 起服务时**随机**端口 | 不可预测、不可复用，`id`（`adp-core.md` §4.1）每次变。裁决采纳的是**升序候选 + `last_port` 复用**（§5.4），与「随机」不是一回事 |
| G. `-A2A <url>` 直接吃地址 | 会和 `nargs="*"` 的任务参数打起来（§5.1）；人类 2026-11-06 裁决 `-A2A` **永远是开关** |
| H. 固定 9100、不做探测 | 示例 agent 也用 9100，同机同跑必然撞；人类 2026-11-06 裁决要自动探测 9000–9999 |
| I. 卡片里加 `provider` / `documentationUrl` | ADP 不校验，属于「看起来更专业」的装饰。卡片多一个字段就多一处要保证不假的信息 —— v1 不加；真要加是一行的事，等有真 URL 再说 |
| J. 显式端口被占时降级到下一个（把它当成候选序列首项） | 用户说的是「**指定了就用指定的**」，降级恰好是反着做：它会注册到另一个地址、另一个 `id`，而用户手里只有「我指定了 9000」这一条线索。显式模式的失败**就是它的正确行为**（§5.4） |

---

## 十四、裁决（已无待确认项）

### 第一轮（2026-11-06，人类原话）

> 「这就是个特殊的频道，不动内核」

| 问题 | 裁决 | 落到哪 |
|---|---|---|
| 每个 A2A task 是否独立会话？（隔离需要给 `Runtime.run()` 加 `isolate`，因为 `-d` 已经 `begin_session()`，单传 `session=False` 会被静默无视 —— `runtime.py:265-270`） | **不做隔离**。要隔离就得改 `runtime.py`，与「不动内核」冲突，方案撤销。共享会话是**设计**（与钉钉同构），不是待修的缺陷 | §6（性质）/ §9（代价写进 README）/ §10（内核零改动） |

### 第二轮（2026-11-06，人类原话）

> 「是开关；最好自动探测9000~9999的可用端口，因为会有冲突；不需要签」

| 问题 | 裁决 | 落到哪 |
|---|---|---|
| `-A2A` 是开关还是带地址？ | **永远是开关**。地址只走 `--a2a-registry`（默认 `http://127.0.0.1:8000` = 本机 OACP）。不提供 `-A2A <url>` —— 那会与 `nargs="*"` 的任务参数打架（`cli.py:95`） | §5.1 |
| 监听端口用什么？ | **自动探测 9000–9999**：显式 `--a2a-port` > `last_port` > 9000→9999 升序；**用 bind 重试而不是先探测**（§5.4）；显式给了端口却被占 → 报错、不偷偷漂（**第三轮把「显式」从序列首位改成互斥模式**，见下） | §5.4 |
| 端口漂了，registry 里的旧条目怎么办？ | 两条一起上：**优先复用 `last_port`**（正常重启不漂）+ **旧条目自动收尸**（`DELETE` 带旧 token；旧地址仍有 HTTP 响应就不删，怕删掉活实例） | §4 末 |
| 卡片签不签名？ | **不做**（不是「v1 不做、v2 再说」）。卡片不写 `signatures`，诚实条款「注册过 ≠ 可信」保留 | §8.5 |

### 第三轮（2026-11-06，人类原话）

> 「再支持个参数，支持指定port，指定了就用指定的」

| 问题 | 裁决 | 落到哪 |
|---|---|---|
| 端口的默认行为与显式行为是什么关系？ | **两种互斥模式，不是一条序列上的先后**。显式（`--a2a-port` / `channels.yaml` 的 `port`）= **就用那一个**：不探测、不读 `last_port`、不降级；被占 → 报错退出。自动（两个来源都没给）= `last_port` → 9000–9999 | §5.4（含实现判据：显式时不构建列表，结构上排除降级） |
| 显式端口被占，要不要换个能用的？ | **不要**。换端口 = 注册到另一个 `id`，而用户手里只有「我指定了 9000」这条线索 —— 显式模式的失败就是它的正确行为 | §5.4 / §13-J |
| 显式端口的价值只是「省一次探测」吗？ | 不是。**它是让 registry 里 `id` 跨重启不变的唯一手段**（`id` 由 `card_url` 派生）；自动模式只能靠 `last_port`「尽量」不漂 | §5.4 / README 端口一节 |
| CLI 与配置文件给的 port，哪个算「显式」？ | **同等地位**。只要是人写的值就是显式；不能实现成「只有 CLI 算」 | §5.4 / §5.3 |
| 要不要加 `OPENTHREE_A2A_PORT` 环境变量？ | **不加**（按极简自定）。端口来源已经两处，再多一处就多一条「这值从哪来」的追问 | §5.1 表 |

### 不再需要人拍板的两条（按极简自定，改动都是一行）

- **卡片 `provider` / `documentationUrl`**：不加（§13-I）。ADP 不校验，加了只是多一处要保证不假的信息。
- **端口 9100**：不再是任何默认值。它只是 `--a2a-port 9100` 显式指定时才存在的选择。

---

## 十五、实现记录：与本文的偏差（2026-11-06，落地时补记）

方案是**计划**，代码是**事实**。两者不一致的地方以代码为准，逐条记在这里，
免得下一个人拿方案当施工图、撞上「文档没写的那一半」。

| 偏差 | 为什么 | 落到哪 |
|---|---|---|
| 「同类型替换、字段级合并」这条规则住在 `manager.apply_extra()` | 方案只写了「给 `from_workspace` 加 `extra` 参数」，没定这条规则的归属。它是**装配语义**（配置怎么并），所以跟 manager 走，而不是塞进 cli | `manager.py` / 本文 §5.2 已补写 |
| `load_manager()` **也**要接 `extra` | 方案 §10 只点名了 `from_workspace`，而 cli 两条启动路径调的是模块级别名 `load_manager`。实现时按方案写全了文件、漏了函数，**既有测试当场抓住**：`-d` 直接 `[错误] 读取频道配置失败：load_manager() got an unexpected keyword argument 'extra'` | `manager.py`；教训是「文件清单 ≠ 函数清单」 |
| 端口全占满时，≤12 个逐个列出，更多时报「首尾 + 个数」 | §11-7 要求「逐个列出试过的端口」，但自动模式的候选有 1000 个 —— 把 1000 个数字打在一行里会把真正的原因（权限 / 全被占）挤出屏幕。测试用 3 个端口的小范围，所以「逐个」这条依然被守住 | `_ports_desc()` |
| `_daemon` 有频道异常退出时，退出码 0 → 1 | §5.4 要求「显式端口被占 = 报错退出」，而 `_daemon` 原本恒返回 0 —— 报错了但脚本看不出。收益不只属于 A2A：任何频道起不来都该非零 | `cli._daemon` |
| `message/stream` 归 `-32601`，`tasks/resubscribe` / `pushNotificationConfig/*` 归 `-32004` | §3.5 的表里两处描述有重叠（既说「含 message/stream」又说「如 tasks/resubscribe」）。实现按表的第一行：**流式意图**回「不认识这个方法」（客户端据此换方言重试），**已知但支持不了的操作**回「不支持该操作」 | `a2a._UNSUPPORTED_METHODS` |
| `-A2A` 与 `--init` / `--report` / `--inspect` / `--functions` / `-u` 互斥 | 方案没提。它们都是「做完就退」的开关，与常驻服务语义相反；默默忽略其中一个，用户会以为另一边生效了 | `cli.main` |
| `-A2A --only wx` 直接报错 | 自评审 P0：`--only` 的过滤在 `build()` 里，A2A 会被静默丢掉 —— 服务看起来起来了，其实收不到任务。现在 `build()` 把被排除的频道记进 `skipped`，cli 再判一次冲突 | `manager.build` + `cli._a2a_only_conflict`；测试 `test_only_exclusion_is_recorded_not_silent` |
| 启动摘要里的三条警告（非回环监听 / 跨机器却给回环卡片地址 / strict registry 要 https） | §8.2、§8.6 要求「打印警告」，实现把它们收进 `_print_startup()`。**只是警告，不阻塞** —— 自动模式下「漂了」没有正确答案可报错 | `a2a._print_startup` |

测试现状：`tests/test_a2a_channel.py` 61 项全绿；整套 `pytest tests` 716 项全绿
（含既有的 `test_channels.py` / `test_default_interactive.py`）。整套里偶尔会掉一条
`test_llm.py::test_post_chat_error_body_reaches_the_caller`（Windows 上
`ConnectionAbortedError`）—— 那是既有的 flaky，单跑与整文件跑都通过，与频道层无关。

### 15.1 第一轮 `ocr review` 修掉的 14 条（全部成立，无一条误报）

评审跑在 6 个被改文件上（`a2a.py` 1006 行新增 + 5 个被改文件）。逐条对源码核实后**全部
接受**，其中三处是 high，都改动了行为契约：

| # | 问题 | 改法 |
|---|---|---|
| 1 | `message/send` 带一个**已终态**的 `taskId` 时，`_set_state` / `_finish` 都因「已是终态」直接返回 —— 新答案被丢弃，blocking 调用方立刻拿到**旧**答案 | follow up 分支先复位 `status` / `artifacts` / `_done` 再跑 |
| 2 | `resolve_base_url()` 抛错时端口已经 bind 住且不会关 —— 端口泄漏到进程结束 | 抛之前 `server.server_close()` |
| 3 | `-A2A --auto 3` / `--no-interactive` 在终端里既不进交互也不进 `-d`，掉进单次任务那条路，`-A2A` 被静默忽略 | 抽出 `_wants_interactive(args)`，`-A2A` 与主流程**共用**这一处判据 |
| 4 | 自动模式里 `last_port` 若是上次**显式**指定的特权端口（80/443），今天绑不上（EACCES）会让整个自动模式起不来 | 只对 `last_port` 容忍非 `EADDRINUSE`；范围里的 EACCES 仍然直接抛 |
| 5 | `_evict` 把被挤出的 task 记 `failed` 却不写 artifact —— 面板上是一片空白 | 抽出 `_set_terminal()`，三个入口（回话 / 退出 / 挤出）共用 |
| 6 | `do_POST` 的 400/404/413 分支不读正文，HTTP/1.1 keep-alive 上残留字节会变成下一个请求行 | 错误响应一律 `close_connection = True` |
| 7 | `send_text` 按文本前缀判「这是不是通知」，模型把前缀当答案开头时被误记成失败 | 改成**带外标记** `msg.raw["notice"]`（见 §6.2） |
| 8 | follow up 把带前缀的文本写回历史 → 下一次的前缀再包一层，逐轮翻倍 | 历史只存原文，前缀只用于这次 run |
| 9 | `--only` 的预判按字面名 `a2a` 猜，而 `apply_extra` 会沿用配置里的频道名（如 `bot`）或改名 —— 两个方向都能错 | 改成**按实际装进 manager 的类型**判（`_a2a_started`），删掉按名字猜的预判 |
| 10 | `channels.yaml` 坏掉时预判吞掉错误，用户看到的是 `--only` 冲突而不是真正的 YAML 错误 | 同上：预判删掉后这条路径不存在了 |
| 11 | `-A2A -u --check` 会在 `_u --check` 的早返回处溜掉，静默忽略 `-A2A` | A2A 校验整块上移到 `Runtime` 之前、`-u --check` 之前 |
| 12 | `stop()` 与 run() 的 `_serving.set()` 之间有窗口：stop 跳过 shutdown，run 随后阻塞在 `serve_forever` 里永不退出 | `_lifecycle` 锁把「检查 stopping」与「标记已服务」配成原子 |
| 13 | `--a2a-*` 参数在不带 `-A2A` 时被静默丢弃 | 显式报错 |
| 14 | `apply_extra` 丢弃注入项的名字，与 cli 的 `--only` 校验互相矛盾 | 由 #9 的改法一并消掉（不再有名字假设） |

修完之后端口只剩**一处** `server_close()`：原来 bind 失败与正常退出各关一次，现在是
同一个 `finally` —— 结构上不再有「新加一条退出路径就忘了关端口」的可能。

### 15.2 第二轮 `ocr review` 修掉的 6 条（也全部成立）

第一轮的 14 处修完后再跑一轮，剩 6 条。**没有一条是上一轮修复引入的新问题**，
但其中一条指出了我自己写 follow-up 复位时留下的并发坑：

| # | 问题 | 改法 |
|---|---|---|
| 1 | 心跳线程只捕 `_TransportError` / `A2aError` / `ValueError`，别的异常（stdout 被掐断时 `print` 抛的 `BrokenPipeError`、意料之外的映射错误）会让线程**静默死掉** —— registry 里那条记录一路 stale 到被人当尸体清掉，日志里一个字都没有 | 加一条兜底：退避 + `log.exception`，循环继续 |
| 2 | `--a2a-advertise` 写成 `http://host:abc` / `:99999` 时 `urlsplit().port` 抛裸 `ValueError` | 包成 `A2aError`（带人话），与其他配置错误一致 |
| 3 | 状态文件里 `last_port` 被手改坏（非整数）会让**整个自动模式**起不来 | 当作没有这一项，回落去扫 `PORT_RANGE`（`_load_state` 对坏 JSON 已是同一个态度） |
| 4 | follow up 在上一轮**还在跑**时到达：上一轮的答案稍后回到同一个 task，先到先得，这一轮的真答案被丢掉 —— 调用方看到「问 A 答 B」 | 每轮一个 run id（`task["_run"]` + `msg.raw["run"]`），`send_text` 丢弃过期那一轮的回复 |
| 5 | 每个被占端口一条 note，忙机器上会打几百行，把紧跟其后的安全警告挤出屏幕 | 聚合成一条（复用 `_ports_desc`），只有 `last_port` 的失败单独说 |
| 6 | `_print_startup()` 的 `print` 在 stdout 被掐断时抛异常，从 `run()` 逃出去 —— 那时端口已经 bind 住 | `_notice` 吞掉 `BrokenPipeError` / `OSError`（它只是给人看的）；`run()` 用一个 `try/finally` 覆盖 bind **之后的所有出口**，少一个出口就少一处端口泄漏 |

第 6 条还顺手把 `run()` 重排成「bind 之后只有一处收尾」：原来地址配错与打印失败是

## 十六、实测记录：对着真实 OACP 跑一遍（2026-11-09）

前十五节是「对着源码与规范读出来的一致性」。这一节是**真跑**：本机起 OACP 0.2.1
（`http://127.0.0.1:8000`），让 `openthree -A2A --a2a-port 9100` 注册进去，再从面板发
一条任务。结果抓到两个 bug —— 一个在实现里，一个在测试里，两个都只有在「对面是真的」
的时候才会露头。

### 16.1 注册交错了地址（实现已修）

`_register()` 曾经 POST `self.card_url`，而它是 RPC 端点 `http://127.0.0.1:9100/a2a`。
OACP 的 `register(url)` 把交上来的 url **当卡片地址**去 `_fetch_card()`，于是：

```
POST /v1/agents -> 422 {"code":"CARD_UNREACHABLE","message":"origin returned HTTP 404"}
```

它 GET 一个只收 POST 的路径，必回 404 —— 这个错误与端口、网络、卡片内容都无关，只与
「交的是哪个地址」有关。修法是把两个地址分成两个属性（`openthree/channels/a2a.py`）：

| 属性 | 值 | 谁用它 |
|---|---|---|
| `card_url` | `base_url + /a2a` | 卡片里的 `url` 字段（客户端调它） |
| `card_endpoint` | `base_url + /.well-known/agent-card.json` | `POST /v1/agents {"url": …}` |

`_registration_tick()` 的「地址变了没有」比较、`_bury()` / `_probe_alive()` 拿到的旧地址，
一并对齐到 `card_endpoint`；`_probe_alive()` 因此不再自己拼一次 `CARD_PATH`。

### 16.2 单测把这个 bug 固化成契约（测试已修）

`test_register_sends_the_card_url_not_the_card` 传的第三个参数是
`http://127.0.0.1:9000/a2a`，断言 `body == {"url": "http://127.0.0.1:9000/a2a"}`。
测试名里的判据（「交地址、不交卡片内容」）是对的，**但「地址」被读成了 RPC 端点** ——
于是 61 项全绿，而真 registry 一次都收不下。

改法：

- `_reg_channel(tmp_path, registry_base, base_url)` 从 base 派生两个地址，让「它们是两
  个东西」成为夹具的一部分；
- 断言改成 `body == {"url": "…/.well-known/agent-card.json"}`，并加一行
  `body["url"] != ch.card_url`；
- 新增 `test_only_the_card_endpoint_answers_a_GET`：真起一个频道，断言
  `GET card_endpoint == 200`、`GET card_url == 404` —— 它守的是**为什么**必须那样交，
  而不只是「我们交了它」。

现在 `tests/test_a2a_channel.py` 62 项。

一条判据留给下一次：**单测用 mock 时，「对面会怎么用这个字段」必须由真契约决定，
不能由「我们已经写好的实现」决定** —— 否则测试只会把实现里的错误重复一遍。

### 16.3 Windows 下 NUL 被当成 tty（未修，待裁决）

后台启动（`subprocess.DEVNULL` / `start /b … < NUL` / Windows 服务）时，Windows 上
`sys.stdin.isatty()` 返回 **True**。于是 `_wants_interactive()` 认为「有人在键盘边」，
`openthree -A2A` 进交互模式，stdin 一读即 EOF，进程退出 —— **A2A 服务跟着一起死**，
而日志里「注册到 registry」那行看起来一切正常。

实测（同一台机器，同一份解释器）：

| stdin | `sys.stdin.isatty()` |
|---|---|
| `subprocess.PIPE`（无输入） | False |
| `subprocess.DEVNULL`（NUL 设备） | **True** |
| `open(os.devnull)` 文件对象 | **True** |

`--no-interactive` 能绕开，本轮实测就是这么跑的；但「后台跑频道」是这个功能最自然的
用法，默认不该踩这个坑。**本轮没有改**：它属于 `cli._stdin_is_tty` 的既有判据，改动
会波及「不带参数 = 交互模式」这条默认行为，属于要人拍板的范围。

### 16.4 真跑通过的部分

| 项 | 结果 |
|---|---|
| 注册 | 201 → `id=a_c3c1cf8ea415660d`；`GET /v1/agents` 里 `status=ok`，`url` 是卡片地址 |
| 面板发任务 | `POST /api/panel/agents/{id}/tasks` → 立刻 `working`；**答案要再调一次 `/refresh` 才进账本** —— 那是 RPC 表面，不是面板路径（面板从不轮询，见 16.6） |
| 协议 | `message/stream` → `-32601`；未知 id → `-32001`；坏 JSON → `-32700`（HTTP 仍 200）；非 v2 / 批量 → `-32600`；驼峰 `GetTask` 同样回 `-32001` |
| 卡片 | `GET /.well-known/agent-card.json` 200，`url` 是 `/a2a`，`capabilities.streaming=false` |
| 显式端口被占 | 第二个实例 `--a2a-port 9100` → exit 1，「端口 9100 被占用。**指定了端口就不会自动换一个**」 |
| 清理 | `DELETE /v1/agents/{id}`（带 `X-ADP-Token`）→ 204，`/v1/agents` 回到空表 |

一条判据留给下一次：**单测用 mock 时，「对面会怎么用这个字段」必须由真契约决定，

### 16.5 两轮 `ocr review` 修掉的 9 条（全部成立）

**第三轮**（5 条）。改完 16.1 / 16.2 之后跑，全部是并发与生命周期的真问题：

| # | 问题 | 改法 |
|---|---|---|
| 1 | `_a2a_started` 名不副实：只判「装进 manager」，而两个调用点都在 `start()` 之前 —— bind 失败（显式端口被占）看不到；交互模式下 REPL 照跑，服务其实没起 | 新增 `_a2a_error()`，`start()` 之后查 `Status.error`；`_daemon` 里 A2A 是唯一频道即退出 1 |
| 2 | follow up 的复位与 `task["_run"] = run_id` 分两次加锁，缝里旧 run 能把旧答案写进刚复位的 working | `run_id` 提前生成，`_run` 与复位放进同一把锁 |
| 3 | `send_text` 的 run-id 比较在锁外，`_finish` 只在锁内查终态 | `_finish(run_id=…)`：比较与转换同一临界区 |
| 4 | `_public` 无锁快照：会看到 `completed` + 空 `artifacts` 的撕裂态（面板一片空白） | 新增 `_view()` 持锁取快照，三处读取改用它 |
| 5 | `apply_extra` 只替换**第一个**同类型条目，第二个照样启动 —— 单实例不变量没守住 | 收掉全部同类型、只留一个，并记一条 note |

**第四轮**（4 条）。修完第三轮再跑，全部是第三轮修复**自己留下的缝**：

| # | 问题 | 改法 |
|---|---|---|
| 1 | `_a2a_error()` 在 `start()` 后立刻查 —— 但 bind 在线程里，那一刻 `Status.error` 几乎一定是空的，端口被占仍被报成「已启动」 | 改成等待型：轮询到「有错」或「端口已绑定」，成功时几毫秒返回 |
| 2 | `task["_done"].set()` 在锁外重读 dict；follow up 换过 Event 时会唤醒**新一轮**的等待者（它还在 working） | 锁内捕获 `done`，锁外 `done.set()` |
| 3 | 新任务路径：`new()` 返回后任务已对 `get()` 可见，`_run` 在第二个临界区才写 —— 同 `taskId` 的重试会看到一条「还没有主」的任务 | `new()` 与 `_run` 放进同一个临界区（RLock 可重入） |
| 4 | `json.dumps(ensure_ascii=False).encode("utf-8")` 遇孤立代理项（客户端塞 `\ud800`）抛 `UnicodeEncodeError`，该 task 此后每次 `tasks/get` 都断连 | `.encode("utf-8", "replace")` |

两轮合计 9 条，无一误报。第四轮的 4 条里有 3 条是第三轮改法**自己**引入的缝隙 ——
这正说明「修完再跑一轮」不是仪式：并发修复最容易在新的临界区边界上漏东西。

**第五轮**（3 条）。改完 16.6 的 blocking 语义之后再跑，全部是那轮改动的连带面：

| # | 问题 | 改法 |
|---|---|---|
| 1 | `_a2a_error()` 空转：没有 A2A 频道（只有钉钉 / 微信）时循环永远命中不了，白等满 2 秒 —— 两个调用点都在 `start()` 后**无条件**调它 | 先挑出 A2A 状态；空则立刻返回，轮询循环只在真有时才跑 |
| 2 | `do_POST` 只按 `Content-Length` 读正文：`Transfer-Encoding: chunked` 时 body 完全没被读走，200 + keep-alive 会把残留字节当成下一个请求行（请求走私） | 显式识别 `Transfer-Encoding` → 411 并关连接（411 ≥ 400，`_send_json` 顺手 `close_connection`）；`test_chunked_body_is_refused_instead_of_desyncing` 读到 EOF 守它 |
| 3 | `_rpc_send` 的「查 + 建」是 check-then-act：`get()` 在锁外、`new()` 在锁内，同 `taskId` 的并发请求会各建一条，后到的覆盖先到的（先到那条的答案被所有权检查丢掉，任务却跑了两遍） | 整个分支收进同一把锁（`_TaskStore.lock` 是 RLock，`new()` 内部可重入） |

第五轮 3 条也全部成立。第 3 条尤其值得记：它就是「我以为上一轮已经把并发收干净了」的那类
缝 —— 锁只包住了一半的分支，等于没包。

### 16.6 面板永远「执行中」：`blocking` 的缺省语义被实测推翻（2026-11-09）

人类原话：「是否完整测试了，我发了个任务，一直执行中的状态」。

**先承认测试缺口**：16.4 那行原来写「面板发任务 → 7 秒后 completed」，那不是面板路径 ——
那 7 秒里有两次 `POST /api/panel/tasks/{id}/refresh` 是我自己发的（事件流：`submit` →
`response(working)` → `refresh(working)@01:04:46` → `refresh(completed)@01:04:49`）。
我测的是 RPC 表面，不是「用户在界面里看得见什么」。

**现象与证据**：

| 事实 | 出处 |
|---|---|
| 用户那条任务在我们这边是 `completed`，比提交晚 5 秒，带 artifact | `tasks/get t-4935b348814a4006` |
| OACP 账本停在 `working`，`updatedAt == createdAt == syncedAt` | `GET /api/panel/tasks?limit=5` |
| 面板的静默刷新只重读自己的 SQLite，不发 `GetTask` | `web/app.js:1124` |
| `GetTask` 只在用户点「从智能体刷新」时发 | `tasks.py:123` + `api.py:324` |
| 服务端没有任务级的后台同步（只有 agent 探活） | 全仓 grep `synced_at` / `create_task` |

**根因**：§3.3 当时「只认显式 `blocking: true`，缺省回 `working`」在协议上无可指摘，
但对面**从不轮询**。于是「回得早」不再是「更安全的默认」，而是「把结论丢进一个没人读的
地方」：任务 5 秒跑完，界面挂 10 分钟。

**改法**（§3.3 已改写）：缺省就等，窗口 `--a2a-block-wait`（默认 45 < OACP 的 60 秒
超时）；显式 `blocking: false` 才立刻 ack；读不懂的值按「等」处理。顺手把等待用的
`Event` 改成**在同一把锁里捕获** —— 锁外重读会拿到 follow-up 换上去的下一轮事件，
而这一轮的答案按所有权规则不会去 set 它，于是白等满窗口。

**复验**（脚本 `.openthree/tmp/e2e_panel.py`，真起实例 + 真 OACP）：一次 POST 用了
2.9 秒 → `state=completed`、`artifactCount=1`、`text='在的'`；账本同步为 `completed`；
随后 registry 条目 204 删除、面板账本条目删除。

**留下的判据**：测试的期望值必须来自**对面的行为**，不是来自我们自己的实现。那条
`test_absent_blocking_returns_immediately` 就是照实现写的 —— 它守的是一个错的决定，
61 项全绿而产品不可用。

测试与实测现状（2026-11-09，含本节）：`tests/test_a2a_channel.py` 66 项；整套

### 16.7 接入回调：把「等」换成「送」（2026-11-09）

人类原话：「oacp 新支持了执行结果回调的协议支持，接入下；注意你不要耦合 oacp 具体实现，
你锚定的是协议」。

**先定锚点，再写代码。** 抄 OACP 的实现会很省事，也很容易在它下一次改参数名时全废。
这次落地的每条判据都能在 A2A 自己的文字里找到出处，OACP 只是其中一个实现：

| 我们做的事 | 协议依据 | 和 OACP 的关系 |
|---|---|---|
| 卡片 `capabilities.pushNotifications: true` | 客户端按能力位决定要不要给地址 | OACP 的 `tasks.py:649` 只读这一个字段 |
| 收内联 `configuration.pushNotificationConfig` | 0.3 的 send 配置里就这么放 | OACP 内联在 `client.py:138` 发 |
| 收 `tasks/pushNotificationConfig/set` | 0.3 方法名；1.x 是 `CreateTaskPushNotificationConfig` | OACP 两个方言都试（`client.py:38-46`） |
| POST 整个 Task + `X-A2A-Notification-Token` | push notification 的投递形状 | OACP 的接收端在 `/a2a/push/<token>` |

**一处必须自己拿主意的判断**：`blocking` 的缺省。上一轮为了「面板从不轮询」把缺省改成
「等」；现在**有回调地址的调用方拿得到答案了**，等的理由消失，于是缺省变成
`not push` —— 显式 `blocking` 一律照办（含「登记了回调但仍要等」）。这不是两条规则，
是同一条判据（「对方拿得到答案吗」）在两个输入下的两个答案。

**实测**（`.openthree/tmp/e2e_push.py`，源码版 OACP 0.1→8010 + `openthree -A2A` 9101，
**一次 refresh 都不调**）：

```
[ok] 面板派了任务 t_a9e458b5c873c28c，提交时 state=working
[ok] 面板账本自己走到了：state=completed（3.1 秒），状态序列=['working', 'completed']
[ok] 回调证据：pushState=registered pushCount=1 lastPushAt=2026-11-09T06:06:02Z
[ok] artifactCount=1 artifact 文本：'1+1 等于 2。'
[结论] PASS —— 不点刷新，面板靠回调自己收到了结论
```

状态序列里**只有** `working → completed`，中间没有 `refresh` 参与 —— 这就是 16.6 那个
「界面永远执行中」的正面回归。

**两个坑（都是我自己这边的，记下来免得下次重踩）**：

1. 用户本机 8000 上跑的是**安装版 0.2.1**，`/api/panel/summary` 里没有 `pushEnabled` ——
   它还不支持回调。第一次跑 e2e 时我对着它测，只会得到「没收到回调」，而那不是我们的
   bug。**判据：先确认对面真的支持，再判断我们做得对不对**（看 `pushEnabled`，不是猜）。
2. 我的 e2e 脚本把面板 API 的形状猜错了两次：`GET /v1/agents` 返回 `{"items": [...]}`、
   `POST /api/panel/agents/{id}/tasks` 返回 `{"task": {...}}`。两次都表现为「我们的代码
   没反应」，其实是**测量工具**错了。这也是一条通用判据：e2e 的失败先分「产品错」还是
   「量具错」，别一上来就改产品。

**留下的判据**：这一轮没有新增任何「靠我们自觉」的约束 —— 每条都写成了结构或测试：

- 有回调地址才改缺省（`_client_waits(config, push=…)`，不是注释里的劝告）；
- 投递在另一条线程（`test_a_dead_callback_endpoint_does_not_slow_the_reply` 守它）；
- body 与 `tasks/get` 逐字节相等（`test_push_payload_is_exactly_the_tasks_get_view`）；
- token 不进 Task（`test_push_token_never_leaks_into_the_task_view`）；
- 坏地址当场拒（`test_push_url_must_be_an_absolute_http_url` 四个例子）；
- 4xx 不重试 / 5xx 用完预算（两条，退避注入成 0 秒，测试不慢）。

**第六轮 `ocr review`（7 条；6 条成立并已修，1 条前提有误）**：

| # | 它说的 | 核实 | 处理 |
|---|---|---|---|
| 1 | follow up 没带 `configuration` 时 `blocking` 缺省算成「等」，可这个 task 其实还登记着回调 —— 白等满窗口 | 成立 | 判据改成看 **task** 上的 `_push`（配置跟着 task 走，判据也必须跟着走） |
| 2 | `_TransportError` 的消息里带着原始 URL，而 `_deliver` 会把它打印出来 → token 进日志 | 成立 | `http_call` 抛错时改走 `_redact` |
| 3 | `_redact` 保留了 netloc 里的 `user:password@` | 成立 | 去掉 userinfo（`rpartition("@")[2]`） |
| 4 | `allow_reuse_port` 没关，POSIX 上两个实例可以同时绑同一端口 | **前提有误**：`inspect.getsource(http.server.HTTPServer)` 只设了 `allow_reuse_address`，`socketserver.TCPServer.allow_reuse_port` 是 `False`（Python 3.12.9 实测） | 仍然**写死** `allow_reuse_port = False` —— 它是继承来的默认值，钉住它才与上面那条 `reuse_address` 同一个性质（别人改 stdlib 也不会静默把「被占报错」变成谎话） |
| 5 | 不校验 `Content-Type` / `Host`，只听回环挡不住浏览器（简单请求 / DNS rebinding） | 成立 | 只收 `application/json`（→ 415），并在代码与 §8 写清「谁将来加了 CORS/OPTIONS，就必须同时加 Host 校验」。**没做** Host 白名单：`--a2a-advertise` / `0.0.0.0` 部署下合法的 Host 不止一个，猜错就是把人挡在门外 |
| 6 | `do_GET` 不读请求体，带正文的 GET 之后 keep-alive 会把残留当下一行解析 | 成立 | 带正文的 GET → 400（`Content-Length: 0` 仍放行） |
| 7 | 回调 body 编码没带 `errors="replace"`，孤立代理项抛的 `UnicodeEncodeError` 会绕过 `except _TransportError` → 这条回调永远发不出去也不重试 | 成立 | 与 `_send_json` 同一处修法 |

两条我自己这边的教训（写下来免得重踩）：

1. 我写的第一版「token 不进日志」的用例忘了接桥（没设 `_on_message`），任务停在
   `working`，于是「没有回调也没有日志」——**测试绿不绿先看它有没有跑到那条路径**。
2. 同一个用例还假设「连一个没人听的端口会立刻被拒」。实测它会**等到超时**（每次 5 秒 ×
   4 次），于是断言在投递还没结束时就执行了。把 `PUSH_TIMEOUT` / `PUSH_TRIES` 一起注入成
   小值 —— 测的是「日志里没有 token」，不该顺带测「连不上有多快」。


`pytest tests` **721 项全绿**；真 OACP 上注册 → 面板发一条任务 → **一次 POST 就拿到
`completed` 与 artifact** → 退订，全链路走通。


