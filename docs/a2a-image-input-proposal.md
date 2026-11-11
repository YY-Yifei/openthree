# A2A 收到图片：协议事实与集成方案

> 目标：别的智能体通过 A2A 发一张图过来，openthree 能**真的看见它**（进模型的
> 多模态 `content`），而不是把它当一段被丢掉的 part。
>
> 行号与字段名都对着磁盘核实过：协议侧的真源是 `a2a-sdk 1.1.2`
> （`site-packages/a2a/compat/v0_3/types.py`、`a2a/types/a2a_pb2.py`），
> 代码侧的真源是本仓库 `main`。自评审见 `docs/a2a-image-input-proposal-self-review.md`。

## 0. 一句话结论

**内核一行都不用改。** 缺口全在频道层与桥：

1. `a2a.py` 的 `message_text()` 只认 `text` part，图片 part 被静默丢弃；
2. `InboundMessage` 没有承载图片的字段，桥也只把纯文本喂给 `Runtime.run()`；
3. 卡片的 `defaultInputModes` 只声明 `text/plain`，严格客户端会因此不发图。

内核的多模态链路（`Runtime.run(images=…)` → `ws.stage_images()` →
`multimodal.build_content()` → history 末尾的 user 消息）**已经完整存在** ——
A2A 只是第三、第四个入口（前两个：CLI `--image`、工具返回 `ImageResult`）。

---

## 1. 协议事实：两代 parts 形状，必须都认

### 1.1 我们声称的版本：0.3.0

`a2a.py:65` 把 `PROTOCOL_VERSION` 钉在 `"0.3.0"`。0.3 的 `Part` 是**带判别符**的联合类型
（`a2a/compat/v0_3/types.py`）：

| 类型 | 定义位置 | 字段 |
|---|---|---|
| `TextPart` | `types.py:1005` | `kind: "text"`、`text: str`、`metadata?` |
| `FilePart` | `types.py:1135-1152` | `kind: "file"`、`file: FileWithBytes \| FileWithUri`、`metadata?` |
| `FileWithBytes` | `types.py:369-385` | `bytes: str`（**base64**）、`mime_type`（JSON `mimeType`）、`name?` |
| `FileWithUri` | `types.py:388-404` | `uri: str`、`mime_type`、`name?` |
| `DataPart` | `types.py:272-277` | `kind: "data"`、`data: dict` |

`Message.parts: list[Part]`（`types.py:1461`）。所以一条带图的 0.3 消息长这样：

```json
{
  "role": "user",
  "parts": [
    {"kind": "text", "text": "这张图里是什么？"},
    {"kind": "file", "file": {"bytes": "iVBORw0KGgo…", "mimeType": "image/png", "name": "shot.png"}},
    {"kind": "file", "file": {"uri": "https://example.com/x.jpg", "mimeType": "image/jpeg"}}
  ]
}
```

### 1.2 现实中的版本：1.x（没有 `kind`）

`a2a-sdk 1.1.2` 的 `lf.a2a.v1.Part` 用 protobuf 反射出来是：

```
oneof content: ['text', 'raw', 'url', 'data']
- text       string
- raw        bytes   → JSON 里是标准 base64（不带 data: 前缀）
- url        string
- data       google.protobuf.Value
- metadata / filename / media_type（json_name = mediaType）
```

**没有 `kind`，也没有 `type`。** 图片是这样：

```json
{"parts": [{"raw": "iVBORw0KGgo…", "mediaType": "image/png", "filename": "shot.png"}]}
{"parts": [{"url": "https://example.com/x.jpg", "mediaType": "image/jpeg"}]}
```

判别只能靠**字段存在性**：`text` → 文本；`raw` 或 `url` → 文件；`data` → 结构化数据。

### 1.3 三处最容易搞错的地方

1. **`mimeType` vs `mediaType`**：0.3 是 `mimeType`，1.x 是 `mediaType`。Python 侧两边都是
   snake_case（`mime_type` / `media_type`），线上只认 camelCase。
2. **`raw` / `bytes` 是裸 base64**，不带 `data:` 前缀。
3. **更老的 proto-JSON 还会发 `{"type": "FilePart", "file": {"fileWithBytes": …}}`**。
   现有 `message_text()`（`a2a.py:358-385`）已兼容 `{"type":"TextPart"}` 那一代，
   新解析器必须把这条也带上。

---

## 2. 现状：缺口在哪一处、后果是什么

| 环节 | 现状 | 后果 |
|---|---|---|
| `a2a.py:358-385` `message_text()` | 只取 `text` part | 图片 part 被**静默**丢弃，模型永远看不到 |
| `a2a.py:1053-1055` `_rpc_send()` | `text` 为空即 `-32602` | **纯图消息直接 400**，连模型那一层都到不了 |
| `base.py:21-42` `InboundMessage` | 只有 `text` | 频道拿到了图也无处安放 |
| `bridge.py:37-40` `compose_input()` | 只拼「来源标注 + 原文」 | 图片信息丢失 |
| `bridge.py:106-110` `ask()` | `rt.run(text, session=True)`，无 `images` | 送不进模型（唯一调用点是 `:140`） |
| `a2a.py:421-422` `build_card()` | `defaultInputModes: ["text/plain"]` | 严格客户端**不会发图** |
| `a2a.py:73` `MAX_BODY_BYTES = 1 << 20` | 入站请求硬闸 1 MiB | base64 膨胀 4/3，**>≈700 KB 的图直接 413** |
| `a2a.py:461` / `:1095-1096` | task 历史只写 text part | 历史里没有图片记录 |
| `a2a.py:503-517` `_set_terminal()` | artifact 只写 text | **出站回不了图**（本方案不做，见 §5） |

### 2.1 内核侧：已经完备

| 能力 | 位置 |
|---|---|
| `Runtime.run(images=…)`（**只在首轮**传图） | `runtime.py:238-241`、`:286` |
| `_run_round` 里 `ws.stage_images()` 归置 | `runtime.py:318-325`、`:347-351` |
| `Workspace.stage_images()`：本地文件复制进 `assets/`（sha1 去重），http(s)/data: 原样保留 | `workspace.py:1163-1186` |
| `multimodal.build_content()`：有图 → OpenAI `image_url` parts；单图失败降级为文本 | `multimodal.py:172-192` |
| `Agent._sync_current_input()`：本轮输入 = history 末尾 user 消息；有图无字 → 「（图片输入）」 | `agent.py:952-994`、`:942-950` |
| `view_image` 内置工具 | `builtins.py:52` |
| 单张上限 / magic bytes 判别 / sha1 去重落盘 | `multimodal.py:71-81`、`:107-119`、`:292-319` |

### 2.2 「零改动」成立的两个前提（必须写出来）

1. **图能撑到下一轮**：首轮的 `images` 会被存进 `_session_turns`（`runtime.py:460-465`），
   下一轮的 Agent 播种历史时用 `build_content` 还原成图片（`agent.py:615-637`，`:622-626`）。
   **前提是 `--turns != 0`**（`runtime.py:80` 默认 4；`_effective_turns()` 在 `turns == 0`
   时返回 `[]`，`runtime.py:229-236`）。`--turns 0` 时图在后续轮消失 ——
   **这是既有行为（CLI `--image` 同样），不是本方案引入的**。
2. **同一会话的后续消息也能看见上一张图**：桥传 `session=True` ⇒ `owns_session=False`
   （`runtime.py:267-270`），会话跨调用累积。A2A 频道共享本进程会话
   （`a2a.py:24` 已声明这条限制）。

---

## 3. 集成设计

### 3.1 一条图进来之后的完整路径

```
POST /a2a   {"kind":"file","file":{"bytes":"iVBOR…","mimeType":"image/png"}}
  │
  ├─ message_parts(message)   →  (text, [FileBlob(bytes_b64, uri, mime, name), …])
  │
  ├─ save_image_bytes()       →  .openthree/assets/<sha1前16>.<ext>
  │      · 先按 base64 长度估算，比 max_image_bytes() 不过就拒
  │      · sniff_mime() 定类型（认不出就拒）；后缀只由它决定
  │      · 临时文件 + os.replace()：并发写盘不产生半截图
  │
  ├─ InboundMessage(text=…, images=["D:/…/assets/3f2a….png"])
  │
  ├─ bridge.compose_input()   →  「来自A2A[…]的消息，请直接回复：\n\n看这张图\n\n（本条消息附带 1 张图片）」
  │
  ├─ bridge.ask(text, images=[…])  →  rt.run(text, session=True, images=[…])
  │
  ├─ ws.stage_images()        →  assets/ 里同 sha1 的文件已存在 → 不重复写（一份真源）
  ├─ build_content()          →  [{"type":"text",…},{"type":"image_url",…}]
  └─ agent history 末尾 user 消息  →  模型真的「看见」了
```

### 3.2 改动清单

#### A. 协议解析 —— `openthree/channels/a2a.py`

- 新增 `message_parts(message) -> Tuple[str, List[Dict[str, str]]]`，放在 `message_text()`
  （`:358`）旁边。第二个返回值是候选文件：
  `{"bytes": <base64>, "uri": <str>, "mime": <str>, "name": <str>}`。
- 形状识别（**只看字段存在性，不看扩展名**）：
  1. `kind` / `type` 显式判别（0.3 与老 proto-JSON）；
  2. 无判别符时：有 `text` → 文本；有 `raw` 或 `url` → 文件（1.x）；
  3. `file` 里再分 `bytes` / `fileWithBytes` / `uri` / `url` 四种拼写。
- `message_text()` 改成 `return message_parts(message)[0]` —— 行为逐字节不变，
  `tests/test_a2a_channel.py` 里所有 `_user_msg()`（`:127`）继续通过。
- `_rpc_send()`（`:1049`）：报错条件从「没有文本」改成「**既没有文本也没有可收的文件**」。

#### B. 落盘 —— `openthree/multimodal.py` + `a2a.py`

**关键：不建第二份拷贝，命名规则只留一处。**

- `multimodal.py` 新增 `save_image_bytes(assets_dir, data, *, sniff=True) -> Path`，
  把 `save_asset()` 里「sniff → 定 ext → sha1[:16] → 写盘」那段抽出来，
  `save_asset()` 改为调它。于是**频道与 CLI 走同一条命名规则**，
  `ws.stage_images()` 看到同名同内容文件时 `if not dest.exists()` 直接跳过（`multimodal.py:317-318`）
  —— 一份图只落一次盘。
- **写盘必须原子**：写 `<dest>.tmp-<uuid>` 再 `os.replace()`。
  `do_POST` 是 `ThreadingHTTPServer`（`a2a.py:537`），两个线程同时收到同一张图时，
  「先 exists 再 write_bytes」存在写一半被桥线程读到的窗口 → 模型收到坏图。
- 频道侧新增 `save_inbound_image(file) -> Optional[str]`：
  - **顺序不能反**：先按 `len(b64) * 3/4` 估算，比 `max_image_bytes()`
    （`multimodal.py:71-81`，默认 10 MiB）不过就拒；再解码；再 `sniff_mime()`。
  - `sniff_mime()` 认不出 → 拒。**对端声明的 `mimeType` 不参与任何命名或类型判定**，
    只在「声明与实测不一致」时记一句日志。
  - **不做 GC**。理由见 §5-2：`Agent._sync_current_input()`（`agent.py:977-982`）会在模型
    改任务时**重读图片文件**重建 content —— 删旧文件等于把 history 脚下的真源抽掉。
  - 目录用 `Workspace` 的 `assets/`（`.openthree/assets/`），不是频道自建的 `inbox/`。

#### C. 载体 —— `openthree/channels/base.py`

```python
images: List[str] = field(default_factory=list)   # 本地绝对路径或 http(s) URL
```

放在 `text` 之后。有默认值 ⇒ 微信 / 钉钉 / 所有既有构造点零改动。

#### D. 桥 —— `openthree/channels/bridge.py`

- `compose_input()`（`:37-40`）：文本为空且有图 → 正文用「（图片输入）」；
  有图时追加一行「（本条消息附带 N 张图片）」。**这是给模型的提示**，
  否则模型只看到一句「请直接回复：」。
- `ask(text, *, images=None)`（`:106-110`）：`rt.run(text, session=True, images=images or None)`。
- `_handle()`（`:140`）：`self.ask(compose_input(msg), images=msg.images)`。
- `ask()` 的唯一调用点是 `:140`（已核实），改签名不波及别处。

#### E. 卡片 —— `a2a.py:388-431` + `:908-910`

- `build_card(..., input_modes: Sequence[str] = ("text/plain",))`。
- `A2aChannel.card()` 按 `self.allow_images` 决定：
  - 开：`["text/plain", "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"]`
  - 关：`["text/plain"]`
- **判据：卡片不撒谎。** 声明了 `image/*` 就必须真能收。
  `defaultOutputModes` 保持 `["text/plain"]`（出站回图不做，见 §5）。
  注意：声明的是 **MIME 能力**，不承诺任意大小（8 MiB body 闸仍在）。

#### F. 请求闸 —— `a2a.py:73`

`MAX_BODY_BYTES` 从 `1 << 20` 提到 `8 << 20`（一行 + 改注释）。判据：

- 1 MiB 装不下一张常见截图（base64 膨胀 4/3，≈700 KB 原图就到顶）；
- 8 MiB ≈ 6 MiB 原图，且 `do_POST` 本来就**先看 Content-Length 再读**（`:619-627`），
  内存峰值有上界；
- **不新增 `max_body_mb` 选项**：没有真实部署差异时，常量优于开关（见自评审八问 6）。
  要压小单张图，用环境变量 `OPENTHREE_MAX_IMAGE_BYTES`（`multimodal.py:71-81`）——
  两道闸取小的那个。

#### G. 任务历史 —— `a2a.py:455-468` / `:1095-1096`

- 历史里**不存 base64**，只存一行占位：`{"kind": "text", "text": "[图片 1: image/png]"}`。
- **占位里不写对端给的 `name`**：它是不可信输入，会随 `tasks/get` 回吐、也会经
  `_follow_up_prefix`（`a2a.py:1531+` 用 `message_text()` 抽文本）进模型的 follow-up 上下文。
- 理由（写进代码注释）：`_public()`（`:520-533`）会 `json.loads(json.dumps(task))` 深拷贝
  整条 task，`tasks/get` 原样回吐，且响应体没有上限 —— base64 进历史 = 每次查询几十 MB。
- 纯图消息（无文本）：历史里就是占位本身（`[图片 1: image/png]`），不会出现一条
  空 text part。

#### H. 选项 —— `openthree/channels/config.py:103` 的 `_SPECS["a2a"]`

阶段 1 只加**一个**：

| 选项 | 默认 | 判据 |
|---|---|---|
| `allow_images` | `true` | 关掉 = 卡片只声明 `text/plain`、图片 part 一律忽略并回一句说明 |

**为什么默认 `true`**：A2A 是「你自己显式 `-A2A` 才起」的频道，默认只听 `127.0.0.1`
（`a2a.py:57`）；能力默认与模型能力一致，少一个「为什么我的图没被处理」的坑。
谁对外暴露，谁显式关或配 `allow_from`。

---

## 4. 两条安全边界

### 4.1 `FileWithUri` / `url`：默认不下载、也不透传

| 做法 | SSRF 面 | 代价 |
|---|---|---|
| **(a) 拒绝 URI 图片**（本方案，阶段 1） | 零 | 对端得用 base64，或把链接写成文本 |
| (b) 透传给模型层（将来可开的 `allow_remote_images`） | 零（`is_url()` 分支不下载，`multimodal.py:84-86`） | URL 随请求**外发给模型提供商** |
| (c) 自己下载 | **有**（内网 / 云 metadata / 重定向 / 无大小上限） | 要一整套 URL 白名单与超时 |

- 阶段 1 走 (a)：URI 图片不进 `images`，文本里补一句「（有 1 张 URI 图片未接收）」。
- (c) **不做**。判据：一个无鉴权端点 + 服务端主动取任意 URL，是本文件能引入的最大的洞，
  收益只是「省对端的带宽」。

### 4.2 图片内容是不可信输入

- 图片可携带提示注入（图里写字、exif、文件名）。落盘文件名由哈希决定已削掉文件名注入；
  内容本身会直接进模型上下文 —— 与 A2A 本来的信任模型一致（远端本来就能发任意文本）。
- 声明与实际不符（`mimeType: image/png` 但内容是 HTML）→ `sniff_mime()` 认不出就拒。

---

## 5. 不做的事（以及为什么）

1. **出站回图**。OACP 面板只从 `artifacts[].parts[].text` 取文本（`a2a.py:19-20` 判据 3），
   回 file part 会让面板一片空白；且 0.3 / 1.x 两代各要写一份编码器。等真有对端要图再做。
2. **给 `assets/` 做 GC**。`Agent._sync_current_input()`（`agent.py:977-982`）会在模型改任务时
   重读图片文件；删旧文件 = 撕掉 history 脚下的真源。**如实记录**：`artifacts` 之外，
   `assets/` 只增不减（`reset_assets()` 定义在 `workspace.py:1188`，全仓库没有调用点；
   CLI `--image` 也一直在往里堆）。这是既有事实，本方案不假装解决它。
3. **把 base64 留在 task 历史 / 内存里**：见 §3.2-G。
4. **图片压缩 / 缩放**：`detail=auto|low|high`（`multimodal.py:40`）是模型侧的旋钮。
5. **per-task 会话隔离**：A2A 频道本来共享本进程会话（`a2a.py:24`）。
6. **给「看懂图」单开一个工具**：模型已经有 `view_image`（`builtins.py:52`）。

---

## 6. 分阶段实施

### 阶段 1：MVP

**代码**（`a2a.py` ~150 行 / `multimodal.py` ~25 行 / `bridge.py` ~10 行 / `base.py` 1 行 /
`config.py` 1 个 Field）：

1. `message_parts()` + `message_text()` 改薄包装。
2. `multimodal.save_image_bytes()`（抽公共命名 + 原子写）+ `save_asset()` 改调它。
3. `a2a.save_inbound_image()`（估算 → 解码 → sniff → 落 `assets/`）。
4. `InboundMessage.images`。
5. 桥三处。
6. 卡片 `input_modes` + `allow_images`。
7. `MAX_BODY_BYTES` 8 MiB（含注释）。
8. 历史占位（不带 `name`；纯图消息的历史就是占位本身 `[图片 N: mime]`）。

**文档同步**（不做就会让下一个人按旧判据改回去）：

- `a2a.py` 模块 docstring 的「限制」清单（`:22-26`）补两条：图片只收 base64、不存进历史。
- `multimodal.py` 模块 docstring 的「图片从哪来、到哪去」（`:12-17`）从三条变四条
  （加「频道进来」）。
- `README.md`「A2A：让别的智能体来派任务」一节：收图 + 两道闸（8 MiB body / 10 MiB 单张）
  + `assets/` 只增不减。
- `docs/a2a-channel-proposal.md` 补一段：part 解析扩展了，artifact 的输出形状没变。

### 阶段 2（可选）

- `allow_remote_images`（(b) 透传）+ 文档里那句 trade-off。
- `tasks/get` 的历史里回图片**引用**（`mimeType` + 说明，仍不含 bytes）。

### 阶段 3（真有需求再说）

- 出站回图（artifact file part，两代形状各一份编码器）。

---

## 7. 测试计划（`tests/test_a2a_channel.py`）

沿用现有 helper：`_channel()`（`:84`）、`_call()`（`:121`）、`_user_msg()`（`:127`）、
`_text_of()`（`:133`）。

| 编号 | 断言什么 | 怎么测 |
|---|---|---|
| T1 | 0.3 `FileWithBytes` → 一张真实文件 | 断言 `msg.images` 有 1 项、路径在 `assets/`、字节与原文一致、后缀按 sniff 定 |
| T2 | 1.x `raw` + `mediaType` 同样能收 | 用 1.x 形状构造 |
| T3 | 老 proto-JSON `{"type":"FilePart"}` 也能收 | 形状兼容性回归 |
| T4 | 纯图消息不再 400 | parts 只有图片，断言返回 `task` 且状态非终态 |
| T5 | 声明 `image/png` 但内容是文本 → 拒 | 断言 `images` 为空且有说明（不抛 500） |
| T6 | 超 `max_image_bytes` → 拒；超 `MAX_BODY_BYTES` → 413 | 两个上限各一条 |
| T7 | URI 图片默认不下载 | `monkeypatch` 掉 `urllib.request.urlopen`，断言**一次都没被调用** |
| T8 | `allow_images=false` → 卡片只有 `text/plain`，图片被忽略 | 卡片 + 消息各断言一次 |
| T9 | 桥真的把 images 传进 `rt.run` | 假 runtime 记录 kwargs |
| T10 | `tasks/get` 响应里没有 base64 | 断言响应体不含超长 base64 串，且长度 < 阈值 |
| T11 | 同一张图发两次 → `assets/` 里只有一个文件 | 断言目录文件数不变（sha1 去重 + 命名唯一真源） |
| T12 | 并发写同一张图不产生坏文件 | 多线程同时 `save_image_bytes`，断言落盘内容完整且 `sniff_mime` 通过 |

---

## 8. 将来改这里的人要守住的六条判据

1. 卡片声明的 `defaultInputModes` **必须等于**端点真实能收的集合。
2. 图片类型只认 magic bytes（`sniff_mime`），**不认**对端声明的 `mimeType`。
3. 落盘文件名只由内容哈希决定，**绝不用**对端提供的 `name`；命名规则**只许有一处**
   （`save_image_bytes`）。
4. `tasks/get` 的响应里**永远不出现** base64。
5. URI 引用默认不下载；即使将来开启，我们也**永不发起出站请求**（只透传给模型层）。
6. 写盘必须原子（临时文件 + `os.replace`）—— 收图是并发入口，CLI 不是。

---

## 9. 阶段 1 落地记录（实现与方案的差异）

1. **`allow_images` 是 property，不进 `_load_options()` 的属性。**
   `handle_rpc()` 在没跑 `run()` 的场景（测试、以及任何直接用频道的用法）里也会被调，
   而 `_load_options()` 那时还没跑；写成 property 现读 `options`，卡片与端点就始终看
   同一个值。`_load_options` 里多一个属性等于多一个「谁先跑」的时序假设。
2. **Windows 上原子写本身会互相撞。** `os.replace()` 在两个线程同时替换同一个目标时
   回 `WinError 5`（拒绝访问）—— 那不是失败，是「别人刚刚把同一个文件写好了」。
   `save_image_bytes` 因此加了兜底：目标已存在且大小与这次一致就当成功，否则才抛。
   §8-6 只说「必须原子」，没料到同一份内容被并发落到同一个名字时原子替换会互相打架。
3. **纯图消息的历史占位就是占位本身**（`[图片 1: image/png]`），不需要另写
   `[图片输入]`：走到写历史那一刻，文本与占位至少有一个非空（两者都空在之前就被
   `-32602` 拦下了），再加一个兜底字符串是死代码。
4. **多出两道闸（评审阶段补上的）**：一条消息最多 `MAX_IMAGES_PER_MESSAGE = 8` 张；
   `assets/` 总量超过 `ASSETS_BUDGET_BYTES = 512 MiB` 后只拒新图。判据：§4.2 说
   「图片是不可信输入」，但收图是**无鉴权**入口、`assets/` 又只增不减 —— 只挡「单张
   多大」与「请求体多大」，挡不住「一条请求发很多张小的」和「每次改一个字节绕开
   sha1 去重」。两条都是常量而不是选项（同 §3.2-F 的判据：没有真实部署差异时，
   常量优于开关）。预算检查与写盘在同一把锁里 —— check-then-act 拆开就是 N 个并发
   请求读到同一个用量、全部通过、再各写几 MB。
5. **`image/bmp` 也在收的集合里**：`sniff_mime()` 一直认 `BM`、`IMAGE_EXTS` 也有
   `.bmp`；§3.2-E 的清单漏了它，卡片与 README 都是 5 种。

测试落地的 25 条在 `tests/test_a2a_channel.py` 末尾，覆盖 §7 的 T1–T12，另加 13 条：
`message_parts()` 的纯解析断言、`compose_input()` 的图片说明、「CLI 落过的图频道
再收一次不该多出第二个文件」（§8-3 那条命名唯一真源的可执行证据）、条数上限、
`assets/` 预算上限（含「去重命中不计费」与「崩溃碎片不计费」）、畸形 URI 不拖垮
整条消息、`_uri_label()` 不泄凭据也不被超长 host 撑爆、对端声明的 MIME 不参与类型
判定、并发写同一张图不留半截。

**新测试的 helper 有一条坑（已修，值得记住）**：`_img_channel()` 从不跑 `run()`，
于是 `_load_options()` 也不跑 —— `block_wait` 传在 options 里不生效，`self.block_wait`
保持 `__init__` 的默认 45 秒，每条 `message/send` 都白等满（走两次 send 的用例 90 秒）。
修法是 helper 里直接 `ch.block_wait = float(opts.get("block_wait") or 0)`：25 条用例
从「整批跑不完」回到 1.7 秒。这与 §9-1 是同一个时序坑，只是这次踩在测试里。

