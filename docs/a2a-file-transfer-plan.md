# A2A 传文件：现状、四条路线与落地步骤

> **2026-11-12 补记（第三轮修订）**：本文的 R3（出站 `file` part）已单独成方案 ——
> 见 `docs/a2a-outbound-files-plan.md`。它是 R3 的具体化。
> 注意：第三轮人要求「发新包后别人 A2A 模式启，自然具备」，所以那份方案**不再**是
> 「内核零改动」—— `attach_file` 内置进包，需动 `builtins.py` / `runtime.py` / `cli.py`
> 各几行（理由见其 §3.4.2）。本文保留作路线对比与 R0/R1 的参考资料。


> 面向：把 openthree 当 A2A 端点用、需要「给对方一个文件」的人。
> 结论先行：**挂工具改变不了「出站只有文本」这件事**。要让对方真拿到文件，只有
> 「链接」和「base64」两种形态；想让对端收到协议里的 `file` part，得改频道（R3）。

| 项 | 值 |
|---|---|
| 适用版本 | openthree 0.8.0（`protocolVersion 0.3.0`） |
| 本文结论的性质 | 现状（代码事实）+ 四条路线 + 一条实测过的工具 |
| 实测环境 | Windows / Python 3.12 / 本文附带的 `a2a_file_link.py`（21 项环回实测全通过） |

---

## 1. 现状：五条代码事实

写方案前先把地基坐实 —— 下面每条都能对着源码核。

| # | 事实 | 位置 |
|---|---|---|
| 1 | 出站只写**文本** part：`{"kind":"text","text": …}` | `channels/a2a.py:623-637`（`_set_terminal`）、`:1522-1543`（`_finish`）、`:1499-1520`（`send_text`） |
| 2 | 频道契约只有 `send_text(msg, text)`，**一个字符串** | `channels/base.py:85-86` |
| 3 | 桥只搬字符串：`reply(msg, answer)`，签名 `Callable[[InboundMessage, str], None]` | `channels/bridge.py:105`、`:148-158`；`channels/manager.py:121` |
| 4 | 卡片声明 `defaultOutputModes: ["text/plain"]`；注释明说「回 file part 会让面板一片空白」 | `channels/a2a.py:542`、`:527-528` |
| 5 | 端点只有两个 GET 面和一个 POST 面：卡片 / `/a2a`，**没有文件路由** | `channels/a2a.py:722-725`、`:727-731` |

再加两条与「传文件」直接相关的入站事实：

- **入站只吃图片**：`save_inbound_image` 只认 magic bytes 的 png/jpeg/gif/webp/bmp
  （`multimodal.py:325-341` 的 `asset_dest`，认不出就 `ImageError`「只认 … 的
  magic bytes」），其它字节一律「未接收」；URI 一律不下载（`a2a.py:1218-1220`）。
- **入站硬闸**：请求体 8 MiB、一条消息最多 8 张、`assets/` 总预算 512 MiB
  （`a2a.py:87`、`:96`、`:101`）。

**所以现状是：双向都传不了任意文件。** 收方向只收图，发方向只有文本。

---

## 2. 「A2A 时额外挂工具」这个想法，对了一半

- **对的一半**：要让对方拿到文件，你确实需要一样「能把文件变成对端可取之物」的能力
  —— 那是一个工具，或者一个现成的静态服务。
- **错的一半**：**没有哪个工具能改变出站只有文本**。工具的输出会变成你答案里的文本，
  然后被 `_set_terminal` 包成 `{"kind":"text"}`。挂十个工具，artifact 里也不会出现
  `file` part。那需要改 `channels/`（见 §5 的 R3）。
- **还有一句要说清**：工具不是「A2A 时才挂」。它跟着 profile 走 —— 放进 profile，
  一次挂载，所有 `-A2A` 进程都带着它（见 §4.1）。

一句话判据：**「传文件」在 0.8.0 里只能以「文本」为载体** —— 要么文本里放 URL，
要么文本里放 base64。

---

## 3. 四条路线

| 路线 | 对端拿到什么 | 改内核？ | 代价 | 什么时候选它 |
|---|---|---|---|---|
| **R0** 已有静态服务 | 文本里的 URL | 否 | 你本来就有 nginx / `http.server` / 对象存储 | 最省事，**优先** |
| **R1** 自带小服务（`a2a_file_link`） | 文本里的 URL | 否 | 挂一个工具（本文附带，已实测） | 没有静态服务，但本机能被对端访问 |
| **R2** base64 塞进正文 | 文本里的字节 | 否 | 编码后膨胀 4/3，占 token、占 `tasks/get` | 没有网络通道、文件很小（≤ 几十 KB） |
| **R3** 出站 file part | 协议里的 `file` part | **是** | 5 个文件、卡片与兼容性都要动 | 要求协议级、且客户端能收 file part |

推荐顺序：**R0 → R1 → R2 →（确有需要才）R3**。

**R1（以及 R2 之外的任何「给链接」方案）有一个前提，必须先回答**：**对端能不能访问
到你给的那个地址？** 同机 / 同局域网 → 可以；对端在公网另一端、或堂在本机 NAT 之后
→ 不行，此时只剩 R0（对端自己有一个能取到的存储）或 R3。这个问题没答案之前，别开始
配 R1。

---

## 4. R1 落地：把文件发布成 URL（照抄四步）

### 4.1 把工具放进一个 profile（一次，之后所有 A2A 进程都有）

工具源码见本仓库 `.openthree/functions/a2a_file_link.py`（三件事：`publish_file` /
`list_links` / `revoke_link`）。放进 profile 目录即可复用：

```cmd
:: 1) 造一个 profile（已存在就跳过）
openthree -C %USERPROFILE%\.openthree-profiles\base --init

:: 2) 把工具复制进去（profile 只认 .openthree/functions/*.py）
mkdir %USERPROFILE%\.openthree-profiles\base\.openthree\functions 2>nul
copy .openthree\functions\a2a_file_link.py ^
     %USERPROFILE%\.openthree-profiles\base\.openthree\functions\
```

> `-C %USERPROFILE%\…` 用 `%USERPROFILE%` 而不是 `~` —— `-C` **不展开 `~`**
> （它会在当前目录建一个名字就叫 `~` 的目录）。`-profile` 才会展开。
>
> **只放一处**：这份工具若同时出现在两个 profile 里，两份 `__system_prompts__` 都会
> 进 system（附随提示词按「文件」收，不按赢家 —— profile 指南里记的既有行为）。

### 4.2 起 A2A 时挂上它

```cmd
:: 同机对端（默认只听 127.0.0.1）
openthree -C myproj -profile "%USERPROFILE%\.openthree-profiles\base" -A2A

:: 对端在局域网另一台机器：显式钉端口，并给出它能访问的地址
openthree -C myproj -profile "%USERPROFILE%\.openthree-profiles\base" ^
         -A2A --a2a-host 0.0.0.0 --a2a-port 9000
```

**注意**：`--a2a-host/--a2a-port` 是 **A2A 频道自己**的监听地址，与下面工具起的
端口不是同一个 —— 工具的端口由 `publish_file(port=…)` 决定。

### 4.3 让模型按纪律用它

工具文件自带 `__system_prompts__`（291 字符），挂上 profile 就生效：

> - A2A 频道的返回体**只有文本**，文件只能以「链接」的形式送到对端：调
>   `publish_file` 拿到 URL，然后**把 URL 原样写进你的答复**。
> - 别编造 URL，也别把 `publish_file` 没给过的地址写进答复。
> - 一次发布只够一次下载：对端可能失败重试，先调 `list_links` 看还剩几次。
> - 发布后要在答复里写清「链接有效期 / 能下载几次 / 文件多大」。

### 4.4 实际回执长这样（本机实测原文）

```
http://127.0.0.1:54337/f/db308cb0e30cdfbaebb5b16e53054c40
文件：报告 2026.bin（1500000 字节，sha256 03f8deab47ff55fd…）
有效期：30 秒（到 14:57:54），可下载 2 次
把上面这一整行 URL 原样写进答复。
```

对端 `GET` 这个 URL 就得到字节（`Content-Disposition: attachment`，中文名走
`filename*=UTF-8''`）。**环回实测 21 项全通过**（17 项链路 + 4 项「重载后仍是同一份表、同一个端口」）：字节完全一致（1.5 MB 随机数据）、
额度用尽 404、TTL 过后 404、撤销后 404、`/`、`/f/`、`/f/deadbeef`、`/../etc/passwd`
一律 404、目录/不存在/空文件被拒。

### 4.5 跨机时必须显式给的两样

| 参数 | 说明 |
|---|---|
| `host="0.0.0.0"` | 工具自己**只听 127.0.0.1**。跨机要显式放宽 —— 这是人的安全决定，工具不替你猜网卡。 |
| `base_url="http://192.168.1.7:8123"` | 对端实际访问用的前缀。只给 `host` 不给 `base_url`，回执会按 `127.0.0.1` 拼并**明确警告**「对端取不到」。 |

还有两件容易漏的：**Windows 防火墙要放行那个端口**；`port=0`（默认）每次是随机端口，
要稳定就把 `port` 钉死。

### 4.6 R1 的边界（工具自己不藏事）

- 链接表与监听端口都在**进程内存**：`reload_functions` 或重启，链接一起消失 —— 这是
  「临时投递」的语义，不是持久文件服务。
- URL 里只有 128 bit 随机 token，**永不出现本机路径**；只服务登记过的那一个文件。
- 单次默认、TTL 默认 900 秒、单文件默认 ≤ 64 MiB；`read_bytes()` 全量进内存
  （并发 N 个 ≈ N 份文件大小 —— 别把它当 CDN）。
- 没有 HEAD / Range：**不支持断点续传**。
- token 会随你的答复进 A2A task 历史，`tasks/get` 会原样回吐 —— 对端（及其日志）能
  长期看到这个链接。**别把「链接保密」当成权限模型**；要撤就 `revoke_link`。
- **每次发布都是新 token**：`list_links` 不回显 URL（这是有意的 —— 表里存 token 就等于
  把链接抄了一份），所以「对端没收到、想再贴一次」只能重新 `publish_file`：会得到一条
  新链接，旧的按原 TTL / 额度自行作废。要贴同一个文件给多个对端，请显式给
  `max_uses=对端个数`。

---

## 5. R2：base64 塞进正文（没有网络条件时的下限）

不做任何改造，把文件 base64 进答复正文。代价要算清（下面都是**估算**，不是实测）：

- 编码膨胀 4/3。
- 主流分词器对 base64 文本约 **2.5–4 字符/token**；按保守的 3 字符/token、单次生成
  上限 32768 token 算：**≈ 98k 字符 base64 ≈ 73 KB 原文**。实践上建议 **≤ 50 KB**。
- 这段字节会进 artifact，而 `_public()` 深拷贝整条 task、`tasks/get` 原样回吐
  （`a2a.py:640-653`）—— **此后每一次查询都背着它**。
- 客户端常见的答案截断/摘要会把它毁掉，且没有任何机制告诉你「被截断了」。

**判据**：能用 R0/R1 就别用 R2；R2 只配「几十 KB、一次性的小东西」。

---

## 6. R3：让 artifact 真的带 file part（要改内核）

> 这一节是**方案**，不是既成事实。0.8.0 里没有一行这样的代码。

### 6.1 改动面（5 个文件）

| 文件 | 改什么 |
|---|---|
| `channels/base.py` | `InboundMessage` 加可选 `reply_files`；`send_text` 之外给一个**有默认实现**的发送口（不然 wechat/dingtalk 立刻 break） |
| `channels/bridge.py` | `reply` 签名从 `(msg, text)` 变成 `(msg, text, files)`；`_handle` 把这次 run 收集到的文件带出去 |
| `channels/manager.py` | 接线（`:121` 那处 `channel.send_text`） |
| `channels/a2a.py` | `_set_terminal` / `_finish` 产出 **text + file 双 part**；`build_card` 的 `defaultOutputModes` 加 `application/octet-stream` |
| 新增内置工具 | `deliver_file(path, name=…)`：把「这次 run 要发出去的文件」登记下来 |

### 6.2 模型怎么表达「发这个文件」——三条缝，推荐哪条

| 缝 | 做法 | 判据 |
|---|---|---|
| **(a) 工具转正** | 内置 `deliver_file(path)` 把路径写进 `KERNEL.state["outbox"]`，桥在 `_handle` 前后 set/clear，`_finish` 读 | **推荐**：意图由模型表达、由工具登记，正是「二」该长的样子 |
| (b) 扫目录 | 频道在 `_finish` 时扫 `.openthree/outbox/<task_id>/` | 模型**不知道 task_id**；改成全局目录又会串台 |
| (c) 正文标记 | 答案里出现 `[[file:路径]]` 就解析 | **反对**：把判断写进 if / 正则 = 硬编码智能，且与「模型不该被格式驯化」冲突 |

(a) 之所以成立，靠一个前提要写明：**桥是单工作线程串行跑的**（`bridge.py:136-146`），
所以一个全局 outbox 不会串台；若将来桥改成并发，这条前提消失，就得改成按 run 分槽。

### 6.3 必须同时守住的四条

1. **text + file 双 part**：面板只从 `artifacts[].parts[].text` 取文本
   （`a2a.py:19-20`、`:527-528`）—— 只回 file part，面板是空白。
2. **大小闸**：file part 用 `bytes`（base64）时，8 MiB 请求体那条闸在**对端**那边，
   我们这边要自己定上限，否则一次 `tasks/get` 就是几十 MB。
3. **不发本机路径**：`uri` 只能是 `file://` 之外的对端可取地址；路径属于本机实现细节
   （与「历史里不存本地路径」同一条判据，`a2a.py:1334-1336`）。
4. **`tasks/get` 会回吐**：artifact 存的是 base64 的话，此后每次查询都背着它 —— 这就是
   为什么 R3 更该走 **`uri` + R1 的文件服务**，而不是 base64。

### 6.4 测试要点（写进 `tests/test_a2a_channel.py`）

- artifact 里**同时**有 text 与 file part，且 text 非空；
- 文件大于上限时降级为纯 text + 一句说明（而不是整条失败）；
- `defaultOutputModes` 与实发一致（卡片不撒谎）；
- `tasks/get` 的响应体大小 **不随文件大小线性膨胀**（走 uri 时）；
- outbox 在 run 结束后被清空（否则下一次 run 会重发上一次的文件）。

---

## 7. 反方向：对端给我们传 pdf / zip，现在也不支持

`save_inbound_image` 的类型判定只看 magic bytes 的六种图片（`multimodal.py:325-341`），
其它一律「1 张图片未接收（认不出图片类型…）」。要收任意文件是 R3 的**对偶**，而且
更危险：那是一个**无鉴权**入口，`assets/` 只增不减（`a2a.py:97-101`）。真要做得先定
「存哪 / 多大 / 保留多久 / 谁来清」四条，再谈实现。

---

## 8. 待你拍板的三件事

1. **对端在哪**：同机 / 同局域网 / 公网？—— 决定 R1 的 `host` 与 `base_url`，
   也决定 R1 到底可不可行。
2. **文件多大、取几次**：几 MB 一次 → R1 足够；几十 MB 且要对端反复取 → 该上 R0
   （静态服务）或 R3。
3. **要不要走 R3**：那要改 5 个文件、动卡片与频道契约。你说改，我才改；不改就停在
   R1 —— 它已经能让对方拿到文件，只是载体是「链接」。

---

## 附录：三行速查

```
R0  已有静态服务      → copy 进去 + 答复里贴 URL（零工具）
R1  没有静态服务      → publish_file(path) → 答复里原样贴 URL
R2  连网络都没有      → base64 进正文，≤ 50 KB
R3  要协议 file part  → 改 channels/ 5 处（本文 §6，未实现）
```
