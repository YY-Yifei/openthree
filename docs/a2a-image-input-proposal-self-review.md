# A2A 收图方案 · 自评审

对着 `docs/a2a-image-input-proposal.md` 逐条反驳。结论先行：

**骨架与哲学一致**（内核零改动、卡片不撒谎、图片只增一份）。
**缺陷落在「边缘机制」这一层**：三处实现细节是错的（P0），两处措辞与开关是站不住的（P1）。

---

## 一、设计哲学八问

### 1. 道（无硬编码智能） —— 通过，但要点名三处「代码在判断」

方案里有三处 if/常量，逐一交代它们为什么不是「把智能写死」：

| 判断 | 位置 | 为什么该由代码做 |
|---|---|---|
| `sniff_mime()` 认不出就拒 | `multimodal.py:107-119` | 模型无法判定「这串字节是不是图」，这是类型判别，不是理解内容 |
| `MAX_BODY_BYTES` / 单张上限 | `a2a.py:73`、`multimodal.py:71-81` | 资源边界，与「理解」无关 |
| `compose_input` 的「（本条消息附带 N 张图片）」 | `bridge.py:37-40` | 一句文案，不是判定 |

反过来，方案**没有**做的事是对的：没有「按图片内容决定收不收」，没有「按关键词判断要不要看图」。
要不要看、看到什么程度，全是模型在 ReAct 里自己决定的（`view_image` 就在它手上）。

### 2. 一（可写 · 可回滚） —— **不通过（P0-1）**

图片进的是 history 的 user 消息，不动 system，方向对。但我漏了一条既有机制：

`Agent._sync_current_input()`（`agent.py:952-994`）**会原地刷新**本轮输入那条消息 ——
模型用 `write_user_prompt` 改了任务正文时，`body` 变了，它就 `build_content(body, images, …)`
**重新读一遍图片文件**（`agent.py:977-982`）。

于是方案里那条「inbox 文件数超限就删最旧的」直接把文件从 history 脚下抽走 →
刷新时变 `[图片加载失败]`。**我自己引入的 GC 会撕掉自己正在用的真源。**

这不是「要不要回滚」的问题，是「真源被自己删了」的问题。改法见 §三 P0-1。

### 3. 二（能力 · 磁盘即真源） —— **不通过（P0-1 / P0-2）**

原方案：频道落 `.openthree/channels/<name>/inbox/<sha1>.png`，然后
`ws.stage_images()`（`workspace.py:1163-1186`）又把它**复制**进 `.openthree/assets/<sha1>.png`。

**同一张图两份拷贝**，而且两份的寿命不同（inbox 会被 GC，assets 只增不减）。
这正是「同一件事写在两个地方，迟早发散」。

而且还有第二个真源问题：如果频道自己拼文件名（`sha1[:16] + ext`），而 `save_asset()`
（`multimodal.py:292-319`）用的是**它自己那套**命名（`_EXT_BY_MIME` 兜底、`path.suffix`
兜底），两者的扩展名规则一旦分叉，`stage_images` 就会**再写一份不同名**的文件 ——
于是「一份图」变成「两份」。判据：**命名规则只该有一处**。

### 4. 三（循环不预设） —— 通过

不改 ReAct、不改 step 结构。图片只是 user 消息 `content` 的一种形态（`multimodal.py:5-8`
已经确立这条，方案沿用）。

### 5. 万物（自造） —— 通过，但有一条反向成本要写清

收图让模型能看见更多外部给的东西，不妨碍它自造。反向成本是：图占的是**同一个上下文**
—— 一张图折算 2000 字符（`multimodal.py:46-48`），但真实的 base64 **每一步请求都会重发**
（见第 7 问）。收很多图会挤掉工具结果的空间。这是取舍不是缺陷，但文档必须说出来。

### 6. 极简 —— **不通过（P1-1）**

我列了三个新选项：`allow_images` / `max_body_mb` / `allow_remote_images`。

- `allow_remote_images` 属于阶段 2，**阶段 1 一行代码都不实现** —— 它不该出现在阶段 1 的
  改动清单里（那正是「没被用过的兼容层」）。
- `max_body_mb` 是「一个常量变成可配置」。判据：**没有真实部署差异时，常量优于开关**。
  `MAX_BODY_BYTES` 从 1 MiB 提到 8 MiB 只是一行，且 `do_POST` 本来就是「先看
  Content-Length 再读」（`a2a.py:619-627`），内存峰值有上界。

→ 阶段 1 只留**一个**开关：`allow_images`。它的判据是真的（有人公开部署但不想要图像输入）。

### 7. 成本 —— 通过，但必须诚实写进文档

- system 字节：**不变**（图片进 history，不进 system）。
- 每步请求体：**变**。一张 1 MB 的图 base64 后 ≈ 1.33 MB，它躺在 history 里，
  **每一步 ReAct 的请求都会重发一次**，直到上下文折叠把它换掉。
- 上下文预算：`estimate_content_chars()`（`multimodal.py:253-273`）按 `IMAGE_CHAR_COST=2000`
  折算图片，不按 base64 长度 —— 所以「预算读数」几乎不动，而**真实 token 与带宽涨了**。
  这是既有设计（`multimodal.py:9-10` 写明了理由），方案继承它，但要在限制清单里说明
  「预算读数不等于真实请求体」。

### 8. 诚实 —— 有两处我说得比实际硬（P1-2 / P1-3）

- 「inbox 上限删最旧」→ 机制与 history 冲突（见第 2 问），**删掉这句话，不要假装有 GC**。
- 「内核一行都不用改」→ 成立，但**有前提**：多轮接力时图能留下来，靠的是
  `runtime.py:460-465` 把首轮的 `images` 存进 `_session_turns`，再由
  `agent.py:615-637` 在下一轮播种时用 `build_content` 还原。前提是 `--turns != 0`
  （`runtime.py:80` 默认 4；`_effective_turns()` 在 `turns == 0` 时返回 `[]`）。
  **`--turns 0` 时，图在第 2 轮起消失** —— 这是既有行为（CLI `--image` 同样），
  不是本方案引入的，但文档必须写。

---

## 二、三查

### 查一：对着代码核事实

| 方案里的断言 | 核实结果 |
|---|---|
| `message_text()` 只取 text part | ✅ `a2a.py:358-385`，非 text 的 part 直接 `continue` |
| `_rpc_send` 无 text 即 `-32602` | ✅ `a2a.py:1053-1055` |
| 卡片只声明 `text/plain` | ✅ `a2a.py:421-422` |
| `MAX_BODY_BYTES = 1 MiB` | ✅ `a2a.py:73` |
| `InboundMessage` 无 images | ✅ `base.py:21-42` |
| 桥 `ask()` 不传 images，且唯一调用点是 `:140` | ✅ `bridge.py:106-110`、`:140` |
| `Runtime.run(images=…)` 只在首轮传图 | ✅ `runtime.py:238-241`、`:286` |
| `ws.stage_images()` 复制本地文件进 assets、远程原样保留 | ✅ `workspace.py:1163-1186` |
| 单张上限默认 10 MiB | ✅ `multimodal.py:44` |
| `tasks/get` 深拷贝整条 task | ✅ `_public()` `a2a.py:520-533` |
| `_set_terminal` 只写 text artifact | ✅ `a2a.py:503-517` |
| OACP 面板只从 `artifacts[].parts[].text` 取文本 | ✅ `a2a.py:19-20` 的判据 3 |
| 0.3 的 `FilePart` / `FileWithBytes` / `FileWithUri` 字段 | ✅ `compat/v0_3/types.py:1135-1152` / `:369-385` / `:388-404` |
| 1.x 的 `Part` oneof `text/raw/url/data` + `mediaType` | ✅ `a2a/types/a2a_pb2.py` 反射（`lf.a2a.v1.Part`） |
| `sniff_mime` 认得 png/jpeg/gif/webp/bmp 五种 | ✅ `multimodal.py:107-119`（**SVG / TIFF 认不出**，方案必须写） |

**版式错误修正**：我把 `TextPart` 写成 `types.py:1005-1014`，实际 `class TextPart`
在 `1005`、`kind` 在 `1010`；改成 `1005` 更稳。

**漏掉的既有事实（要补进方案）**：

| 事实 | 位置 | 为什么重要 |
|---|---|---|
| 首轮 `images` 存进 `_session_turns` | `runtime.py:460-465` | 「图能撑到下一轮」的机制在这里 |
| 播种时用 `build_content` 还原图片 | `agent.py:615-637`（`:622-626`） | 同上 |
| `session=True` 时 `owns_session=False`（会话跨调用累积） | `runtime.py:267-270` | 同一会话的后续消息也能看见上一张图 |
| `_sync_current_input` 会重读图片文件 | `agent.py:977-982` | **决定了图不能被删**（P0-1 的依据） |

### 查二：与既有决策对账

- `a2a.py:22-26` 的「限制」清单（无流式 / 共享会话 / 任务表在内存 / cancel 只是不再等）
  **没有一条提到图片**；`message_text` 的行为从「只 text」变成「text + 文件」之后，
  模块 docstring、README 的 A2A 一节、以及 §「限制」都要补一句。
  **只改代码不改文档，下一个人会按旧 docstring 改回去。**
- `docs/a2a-channel-proposal.md` 里若有「parts 只取 text / 答案只写 text」之类的判据，
  要指名「本方案扩展了 part 解析，没改 artifact 的输出形状」。→ 该文档需补一段（P2）。
- `multimodal.py:12-17` 已经列了图片的三条路（用户输入 / 工具返回 / 会话记忆），
  本方案给它加**第四条**（频道进来）。那段模块 docstring 要改 —— 它是这个模块的
  「世界观」，少一条就会让后来者以为 A2A 的图走了别的路。

### 查三：我这一版方案的硬伤

**P0-1（实现出来是错的）：inbox + GC 与 `_sync_current_input` 的二次读盘冲突。**
图被删 → 模型刷新本轮输入时读到「图片加载失败」。
→ **改法**：不建 inbox。图片直接落 `.openthree/assets/`（Workspace 的领域，
`ws.assets_dir`），**不做 GC**，并把「assets 只增不减」写进限制清单
（这是既有事实：`reset_assets()` 在 `workspace.py:1188` 定义、**全仓库没有调用点**，
CLI `--image` 也一直在往里堆）。

**P0-2（实现出来是错的）：并发写盘会产生半截文件。**
`do_POST` 是 `ThreadingHTTPServer`（`a2a.py:537`），每条连接一个线程 ——
两个线程同时收到同一张图（同 sha1）时，`save_asset` 的
`if not dest.exists(): dest.write_bytes(data)`（`multimodal.py:317-318`）存在写一半被读的窗口；
而桥线程正在 `build_content` 读它 → 模型收到一张损坏的图。
→ **改法**：写临时文件 + `os.replace()`（同目录原子替换）；命名逻辑抽成
`multimodal.save_image_bytes(assets_dir, data)`（`save_asset` 改为调它），
**命名规则只留一处**（P0-1 的第 2 条）。

**P0-3（实现出来是错的）：`image_part()` 的 `mime` 来自 data URL，而对端可控。**
`to_data_url()` 用 `sniff_mime(data) or guess_mime(path)`（`multimodal.py:153`）——
落盘时按 sniff 出的类型命名，这条链是安全的。但**如果**频道把对端声明的
`mimeType` 写进文件名/后缀，`guess_mime` 就会把它当真相。
→ **改法**：落盘文件的后缀只由 `sniff_mime()` 决定，对端的 `mimeType` 只用于
「声明与实测不一致时记一句日志」，**不参与任何命名或类型判定**（方案 §3.2-B 已写，
但要补一句「`name` 同样不进 history」，见 P1-2）。

**P1-1：三个开关砍到一。** 见八问第 6 问。
**P1-2：任务历史里的占位文本不能带对端给的 `name`。**
`[图片 shot.png image/png 已归档]` 里的 `shot.png` 是对端可控字符串，会随
`tasks/get` 回吐、也会进模型的 follow-up 上下文（`_follow_up_prefix`，`a2a.py:1531+`
调 `message_text` 取文本）。→ 改成 `[图片 1: image/png]`，不带 `name`。
**P1-3：「内核一行不改」要加前提。** 见八问第 8 问（`--turns 0` 时图在后续轮消失）。

**P2 类**：
1. 纯图消息时 `_TaskStore.new(text="")` 会在 history 写一条空 text part →
   写占位 `[图片输入]`（`a2a.py:455-468`）。
2. `sniff_mime` 只认 5 种（png/jpeg/gif/webp/bmp），**SVG 与 TIFF 会被拒** ——
   写进文档（拒是安全的；SVG 本来就不能直接喂 `image_url`）。
3. `docs/a2a-channel-proposal.md` 与 `README.md` 的限制清单要补图片相关两条（P1-2 的
   「不存 base64」与「assets 只增不减」）。

---

## 三、结论

| 级别 | 条目 | 改法 |
|---|---|---|
| **P0-1** | inbox + GC 与 `_sync_current_input` 二次读盘冲突 | 落 `assets/`、不做 GC、限制清单写明「只增不减」 |
| **P0-2** | 并发写盘产生半截文件 | 临时文件 + `os.replace`；`save_asset` 与新增 `save_image_bytes` 共用命名逻辑 |
| **P0-3** | 对端 `mimeType` 不得参与命名与类型判定 | 后缀只由 `sniff_mime()` 决定；`name` 不进 history |
| **P1-1** | 三个开关 | 阶段 1 只留 `allow_images`；`MAX_BODY_BYTES` 常量提到 8 MiB |
| **P1-2** | 历史占位带对端 `name` | 改 `[图片 N: image/png]` |
| **P1-3** | 「内核零改动」没写前提 | 补 `_session_turns` / `--turns` 的事实 |
| **P2** | 空文本占位 / SVG·TIFF 被拒 / 两份文档待补 | 随阶段 1 一起做 |

**骨架判断**：不改。频道只负责收发、内核不动、卡片不撒谎 —— 这三条与项目一贯的
判据一致。上面六条全落在「怎么把一张字节流安全地变成一条 history 引用」这一层。

---

## 四、提交前复核：对未提交代码的复核评审（2026-11-11）

结论先行：**骨架与哲学一致，改动可以提交**；但有一处必须先修（P0，已修），
三处文档错误（P1，已修），四条 P2（未改，判据写在下面）。

### P0（已修）：新增的 25 条收图测试，每条白等 45 秒

- **判据**：`_img_channel()` 从不跑 `run()`，于是 `_load_options()` 也不跑 ——
  `block_wait` 只在 `_load_options()` 里从 options 落地（`a2a.py:852`），所以传在
  `opts` 里的 `"0"` 不生效，`self.block_wait` 保持 `__init__` 的
  `DEFAULT_BLOCK_WAIT = 45`（`a2a.py:827`）。于是每条 `message/send` 白等满 45 秒，
  走两次 send 的用例等 90 秒。
- **证据**：`--durations=0` 实测 45.01s / 90.02s；对照既有同类测试
  `test_send_then_answer_lands_in_artifacts`（用 `_channel()` + `_start()`，`run()` 跑过）
  只有 0.35s。
- **后果**：`tests/test_a2a_channel.py` 整文件跑 560 秒仍不出结果（实测被杀），
  全量从一分多钟变成十几分钟 —— 在 CI 上就是「挂住」。
- **改法**：helper 里直接落到属性上 —— `ch.block_wait = float(opts.get("block_wait") or 0)`。
- **修后**：25 条 **1.7 秒**全绿；全量 **780 passed in 128.83s**。
- **教训**：这与 §9-1（`allow_images` 做成 property）是同一个时序坑 ——
  **`_load_options()` 没跑时，options 里的值一律不算数**。写新 helper 时先看这条。

### P1-1（已修）：README 的「收图」小节把「端口与身份」劈成两半

`#### 端口与身份：自动挑，还是钉死` 之后紧接着就是 `#### 收图：…`，而端口那节的
两条 bullet（`- **不给 --a2a-port**`、`- **给了 --a2a-port**`）排在「收图」正文**之后** ——
读者会把它们读成收图的限制。改法：整段搬到 `#### 三条要知道的限制` 之前，
端口标题后补一个空行。

### P1-2（已修）：README 的「两道闸 + 三条判据」下面列了 5 条，且漏掉两道真实存在的闸

实现里还有 `MAX_IMAGES_PER_MESSAGE = 8`（一条消息最多几张）与
`ASSETS_BUDGET_BYTES = 512 MiB`（`assets/` 总量），README / `config.py` 的 hint /
本方案都没写。这两条存在的理由正是「**无鉴权入口 + `assets/` 只增不减**」——
不写等于把「这个端点可以被当成磁盘填充入口」藏在代码里。改法：README 按
7 条重列（含 8 张与 512 MiB），`config.py` 的 `allow_images` hint 补一句。

### P1-3（已修）：proposal 与实现三处不一致

1. 卡片清单缺 `image/bmp`（实现 `IMAGE_INPUT_MODES` 与 README 都是 5 种）；
2. §6-8 仍写「纯图用 `[图片输入]`」，实现是 `[图片 N: mime]`（§9-3 已说明，§6 没同步）；
3. §9 说「落地的 17 条测试」，实际 25 条；标题「三处差异」也扩成 5 条。

### P2（未改，判据留在这里）

1. `-32602 "消息里没有文本（parts 里找不到 text）"` —— 判据已扩成「既无文本也无文件」，
   文案仍只说文本。
2. `notes.append(f"（{why}）")`，而 `why` 本身是「1 张图片未接收（…）」→ 双层括号
   `（1 张图片未接收（…））`。
3. `_assets_usage()` 每条消息、每张图都全目录 `iterdir()` + `stat()`；8 张 = 8 次扫描。
   收图是低频操作，可接受 —— 但如果 `assets/` 长到上万文件，这里会先疼。
4. `allow_images: false` 且消息**只有图**时，返回的是 `-32602`（原因写在错误里），
   不是「给模型的文本里说明一句」。README 原句已按这个事实改正。

### 复核过、确认没问题的（省掉后来者再查一遍）

- `reset_assets()` 全仓库**确实**没有调用点 ——「`assets/` 只增不减」这个前提成立。
- `stage_images()` → `save_asset()` → `save_image_bytes()` 是**一条**命名规则，
  「一份真源」不是口号：频道与 CLI 落到同一个文件名，去重才真的生效。
- `sniff_mime()` 认 `BM`（bmp），`IMAGE_EXTS` 也有 `.bmp` —— 卡片声明与实测能力一致。
- `save_asset()` 的宽松兜底（认不出落 `.bin`）与频道的严格拒绝（认不出就拒）不是双标：
  前者处理本地可信文件，后者处理对端字节。
- URI 一律不下载：`not b64` 时直接返回原因，`urlopen` 一次都不调（有 monkeypatch 断言）。
- 预算检查与写盘同在 `self._assets_lock` 里；`asset_dest()` 先判去重，「重发的图被计费」
  不会发生。
- 历史里只落 `[图片 N: mime]`，`tasks/get` 不含 base64、也不含本机路径。
- 空内容 / 超限 / 认不出 / 写不进 都归 `ImageError` 或「返回原因不抛」，
  一条存不下来的图不会把整条消息变成 `-32603`。
