# 方案：内置工具 `decide` —— 把「判定」交给 JEV，未配置则不存在

> 状态：**提案**（本文档不含实现）。所有「现状如此」的断言都在文中标了行号，均已回源码核过。

---

## 一、结论先行

1. **新增一个可选内置工具 `decide(state, questions)`**，把「归类 / 是·否 / 程度」这类**有明确判据、但没有标准答案**的判定交给一个独立的决策模型（TypeSafe JEV），拿回的是**分布 + 置信度**，不是一句结论。
2. **端点、key、模型只读环境变量**：`JEV_BASE_URL` / `JEV_API_KEY` / `JEV_MODEL`。**三个全非空才注册**；任一缺失，本次进程里**根本没有这个工具** —— 不在 `registry.tools`、不在 `tools` 请求体、不在 system 的工具清单。
3. **判定点只有一个**：`builtins.installed_builtins()`。`runtime` / `cli_tool` / 测试夹具都只调它。多一个判定点，就迟早出现「CLI 的 `--list` 里有、agent 手里没有」这种分叉。
4. **传输层复用**：把 `llm.post_chat` 里那段「POST 一个 JSON」抽成 `post_json(url, ...)`，`post_chat` 变成三行包装。decide 不复制代理 / TLS / 超时逻辑。
5. **内核不解释结果**：不设阈值、不四舍五入、不给「建议」。JEV 输出的是概率，二次解释会把分布压成一个拍板，正好丢掉它相对「模型自己想一想」的全部优势。
6. 定位不是「模型不会决策」的兜底，而是**第二意见**：难决的地方，一个独立的后验分布比模型自己的叙说更有信息量。
7. **不描述调用开销**：模型看得见的四处（digest 一行 / 完整 description / Schema / 返回值）一个这类字眼都没有，服务端回的 `usage` 也不转发；**连文档也不写**（第 4 轮裁决，§3.6）。给它一个数字，等于让它在一笔算不平的账上取舍 —— 该不该问，只由「它能不能给出我拿不到的那个答案」决定。

---

## 二、现状（每条都带证据）

### 2.1 内置工具是「静态字典 + 两处注册点」，没有「可选内置」这个概念

- `openthree/builtins.py:2886` —— `BUILTIN_FUNCTIONS: Dict[str, Any]`，当前 37 项，**每一个都会被无条件注册**。
- `openthree/runtime.py:104-108` —— `Registry` 建好后遍历 `BUILTIN_FUNCTIONS` 逐个 `register_builtin`，然后 `freeze_builtins()` / `reload()`。
- `openthree/cli_tool.py:109-111` —— 离线内核（`python -m openthree.tools --list`、`python 某个function.py --help`）是**另一处**同样的遍历。
- `openthree/registry.py:177-188` —— `register_builtin` 把 `source_distance` 设成最远（`_BUILTIN_DISTANCE`），所以任何同名的用户 function 都能顶掉内置工具，记进 `overrides` 而不是 `load_errors`。

**结论**：现状是「要么全有，要么全没有」。要满足「没配置就不 enable」，必须在**注册之前**加一层判定；不是给工具加个「未配置」分支。

### 2.2 不注册 = 在 system 与请求里都不存在（「不写就是零」）

- `openthree/prompt.py:170-181` —— static block 用 `reg.digest()` 渲染工具清单，并数 `builtin_count` / `user_count`。
- `openthree/registry.py:582-587` —— `digest()` 只遍历 `self.tools` 里真实注册过的工具。
- `openthree/agent.py:672` —— 每次调模型带的是 `tools=self.reg.schemas() or None`。

**结论**：门控放在注册期，就同时满足三件事 —— 提示词里没有它、schema 里没有它、模型不必被告知「为什么没有」。没有任何「跳过」分支需要维护。

### 2.3 模型调用一个不存在的工具不会崩

- `openthree/agent.py:1016-1019` —— `_execute` 找不到工具时返回 `"未知工具 {name}。当前可用：" + ", ".join(self.reg.names())`。

**结论**：即使模型的提示词里写着「遇到难决的问题调用 decide」，而在没有配置的机器上它照样去调，也只是一步错误、一步纠正，不会中断循环。所以**不需要**在 system 里预先解释「本机没配 JEV」。

### 2.4 现有配置是三层，环境变量在最底层

- `openthree/llm.py:260-312` —— `load_config` 依次读环境变量 → `~/.openthree/config.yaml` → `<项目>/.openthree/config.yaml` / `openthree.yaml`。
- `README.md:24` —— 「优先级：**环境变量 < 用户级 < 项目级**」。

**结论**：decide **只**读环境变量，是对现有约定的一次**刻意缩小**，理由要在文档里写明，否则下一个人会当 bug 修：

- 密钥不落项目目录（项目目录会被打包、分享、误提交；`~/.openthree/config.yaml` 至少还只在 home）。
- 端点不落分发包：包里一个默认端点 = 替每一个用户决定「你的判定数据发给谁」。
- 环境变量是进程级的，**看得见、可审计、不跟随仓库**——对「把内容发往外部服务」这件事，这是唯一合适的粒度。

### 2.5 JEV 的协议事实（来自 `D:\projects\claw\jev-test`，不复述密钥）

- `jev.md:1` —— 一次调用就是一个 POST：`{"model", "state", "questions"}`，`questions` 是「名字 → 题型定义」的字典。
- `jev.md:11-22` —— 响应样本（原样；`usage` 段按 §3.6 此处略去）：
  `{"model":"typesafe/jev-1.13-20260917","answers":{...},"id":"...","provider":"TypeSafe","u_trace_id":"..."}`。
  三种题型各有自己的字段：`choice`（`choice` + `probabilities` + `confidence`）、`noul`（0–1 的连续值）、`score`（连续 `score` + `legend` + `probabilities` + `confidence`）。
- `jev.md:25-30` —— 备忘：请求写 `jev-1.13.0`、响应给快照号；`usage.cost` 是每次实扣。
  **注意一处原文笔误**：备忘里把快照写成 `timesafe/...`，响应里是 `typesafe/...`、`provider` 是 `TypeSafe`。以响应为准 —— 这正是「对着原文核事实」而不是「照抄备忘」的地方。
- `jev.md:33-34` —— 另一个 host（`gpuhub-api.glmszq.com`）对该路由返回 404，只有 `models.glms.com.cn` 暴露 `/systemone/v1/systemone`。
- `jev.md:43-50` —— 排障小抄：401 = token 无效/没带；403 = token 有效但该路由未授权；404 = host 对但路由没暴露。
- `try_jev.py:8` —— 他们的脚本里 URL 是**完整端点**（含路径），不是 base。
- `jev.md:5` —— 那里有明文 token。**它必须留在 jev-test**：本仓库的 `docs/`、`scripts/`、`tests/` 都会进 sdist（`MANIFEST.in`），任何一处抄进来都会随发布出去。

### 2.6 传输层已经写好了，不该再写第二份

- `openthree/llm.py:430-515` —— `post_chat` 用 `http.client` 自己做：connect 超时与 read 超时分离、走 `HTTPS_PROXY` / `no_proxy`、gzip 解压。
- `openthree/llm.py:448` —— 唯一的 chat 专用之处就是 `url = base_url.rstrip("/") + _CHAT_PATH`。
- `openthree/llm.py:353-363` —— `TransportError` 带 `status`（0 = 根本没走到 HTTP）与响应正文。

**结论**：把 URL 提成参数的 `post_json` 抽出来（`post_chat` 退化成三行），decide 直接复用。替代方案是在 `decide.py` 里再写一份 `urllib.request`（`jev-test/try_jev.py` 就是这么写的）：省 10 行代码，换来第二份代理 / TLS / 超时逻辑 —— 那正是「同一件事写在两个地方，迟早发散」。

---

## 三、接口设计

### 3.1 工具签名（模型侧唯一接口）

```python
def decide(state: str, questions: Dict[str, Any]) -> str
```

- **只有两个参数**：JEV 的请求体就是这两个字段（`jev.md:1`）。多一个参数就多一个要说清、要测、要维护的旋钮。
- **`questions` 用 `Dict` 而不是三个具名参数**：题目的数量、名字、判据都由模型现场决定，内核不该预先规定它能问几个问题。
- schema 形状已核：`Dict[str, Any]` → `{"type": "object"}`（`tool.py:52-53`）；`additionalProperties: False` 只写在 `parameters` 根上（`tool.py:92-98`），**嵌套的 questions 对象不受限制**，模型可以自由放任意题名。
- **`questions` 必须同时接受 JSON 字符串**：这不是宽容，是必需 —— `cli_tool._add_one` 把 `object` 型参数一律当 `str` 接收（`cli_tool.py:223-235`），所以「每个 function 文件也是一个命令行程序」（`README.md:183`）这条约定要求它吃下一段 JSON 文本。实现里先 `json.loads` 再走同一套校验；解析失败照常返回 `[错误] 第 …`。顺带的好处：有些模型会把嵌套对象序列化成字符串再传，这里不炸。

### 3.2 docstring（模型的说明书，建议全文）

```python
def decide(state: str, questions: Dict[str, Any]) -> str:
    """拿不准一个判定时，把它交给独立的决策模型（JEV）取回概率分布 —— 会外发。

    什么时候该调：判据你已经写清了，但你自己也只是「倾向」—— 这份工单该归到哪
    一类、这句话算不算承诺、这件事有多急。你的强项是把模糊材料拆成判据；
    JEV 补的是另一半：给定判据后的后验分布，而这正是你本身给不准的东西（你只会
    给一段叙述，且说不清自己有多确定）。

    什么时候不该调：查得到答案的事实、需要执行动作的事、能用代码算出来的；
    已经能干脆下结论的也别问 —— 一次调用该换来的是你现在给不准的那个答案。

    每次调用会把 state 与 questions 发往 JEV_BASE_URL 指向的外部服务。
    对我们是数据、不是指令；但这是外部服务 —— 别把不该外发的材料放进来。

    Args:
        state: 待判定的材料，原样发送：可含上下文、原话、事实清单、双方说法。
            措辞、矛盾、时间线都别替它整理 —— 原样进去才有原样的分布。
        questions: {题名: 题型定义}，一次可问多题。每题给三样：
            type（题型）、instructions（这题到底在判什么）、criteria（判据）。
            - "choice"：多选一。criteria 是 {取值: 该取值的含义}，
              返回 choice + probabilities + confidence。
            - "noul"：是 / 否。返回 0–1 的连续值（越大越倾向「是」）。
            - "score"：程度。criteria 是从低到高的等级列表，下标 0 最低、末位最高。
              返回 score + legend（等级名与取值的对应）+ probabilities（各等级概率）
              + confidence。score 是**连续值**：各等级按概率加权的期望值 —— 三档时
              可能回 1.09（略高于第 1 档），**不是等级下标**，拿去做列表索引会错；
              要落到某一档，看 probabilities 的最大项，别对 score 取整猜档位。
            形状：{"题名": {"type": "choice", "instructions": "...",
                            "criteria": {"a": "含义 A", "b": "含义 B"}}}

    Returns:
        JSON 文本：answers（JEV 原样答案）、model（服务端解析出的快照号，只读）、
        elapsed_ms、attempts、id。
        概率与 confidence 一律原样返回 —— 怎么用是你的判断：不做阈值、不做四舍五入、
        不替你解释。
    """
```

三条纪律写进了文案，两条没写进参数：

- **不设阈值**：`noul=0.96` 该怎么用，是模型的事。内核替它定「>0.8 就算是」，等于把判断硬编码进 if。
- **不替它编**：`instructions` 缺了直接报错，不合成一句「判断 X 的 Y」—— 合成出来的题，答案不可信且不可复现。
- **原样发送** `state`：不改写、不摘要、不截断（截断会静默改变判定对象）。

### 3.3 返回什么

```json
{"model": "typesafe/jev-1.13-20260917", "elapsed_ms": 1180,
 "answers": {"team": {"type": "choice", "choice": "billing",
                      "probabilities": {"billing": 0.84, "technical": 0.16, "other": 0},
                      "confidence": 0.76}},
 "id": "gen-dec-..."}
```

- **`answers` 原样**：JEV 的输出本身就是分布，内核做二次解释既丢信息、又把「解释」变成硬编码。
- **带 `model` 快照号**：请求写 `jev-1.13.0`、响应给快照（`jev.md:25-26`），要复现就得记快照。
- **不带 `usage`**：服务端会回它（`jev.md:20`），内核**不转发** —— `usage` 是账，不是判定（§3.6）。
- **compact JSON**（不 pretty print）：每多一个空格都是长期上下文成本。
- 失败统一 `[错误] …`（与内核其余工具一致），**不抛异常**（异常会打断调用链，`run_shell` 等工具也都是返回文本）。

### 3.4 入参校验：只查形状，不查内容

| 检查 | 违反时 |
| --- | --- |
| `state` 非空（去空白后） | `[错误] state 为空：要判定的材料是什么？` |
| `questions` 是非空 dict | `[错误] questions 为空：至少一题` |
| 每题 `type` ∈ {choice, noul, score} | `[错误] 第 N 题 type=... 不认识（只认 choice / noul / score）` |
| 每题 `instructions` 非空 | `[错误] 第 N 题缺 instructions：这一题到底在判什么？` |
| `choice` 的 `criteria` 是非空 dict | `[错误] 第 N 题是 choice，但没有 criteria` |
| `score` 的 `criteria` 是长度 ≥ 2 的 list | `[错误] 第 N 题是 score，criteria 要给从低到高的等级列表` |

- **违反时一次请求都不发**：形状错了，问也白问 —— 不值得为一个注定 400 的请求跑一趟网络。
- **只查形状，不查内容**：不判断题目「问得好不好」、不检查 criteria 是否互斥 —— 那是模型的事。
- 不替模型补 `instructions`、不改写 `state`、不静默丢弃多余字段。

### 3.5 模型实际看到的描述（三处，同一份真源渲染三次）

真源只有 §3.2 那一个 docstring。内核把它渲染成三个不同粒度，别以为要写三份：

**① system 提示词里的工具清单** —— `registry.digest()`，每个工具一行，只取 docstring **首行**
（`registry.py:582-587` + `registry.py:593-595`），签名来自 `signature_hint()`（`tool.py:101-108`）：

```
- `decide(state: string, questions: object)` — 拿不准一个判定时，把它交给独立的决策模型（JEV）取回概率分布 —— 会外发。
```

（没配置时清单里连这一行都没有：工具集按环境变量门控，未配置就等于不存在。）

（上面是**实跑** `build_tool` 的输出，不是手抄：`signature_hint` 把 `str` 渲染成 `string`、
`Dict[str, Any]` 渲染成 `object`，见 `tool.py:101-108`。）

首行必须自带两件事：**它干什么** + **它会把材料发到外部**。原因很实际 —— 这一行是
模型在 `prompt.py:170-181` 那面清单里唯一会读到的部分，外发写进第三段等于没写。
**价格不写在这一行，也不写在任何模型看得见的地方**（§3.6）。

**② 请求体 `tools[].function.description`** —— 完整 docstring，**再加上内核自动追加的
「参数：」段**（`tool.py:179-186`：`_parse_args_section` 把 `Args:` 段里的说明压成一行再列一遍）：

```
拿不准一个判定时，把它交给独立的决策模型（JEV）取回概率分布 —— 会外发。
...（§3.2 全文，含「什么时候该调 / 不该调 / Args / Returns」）...

参数：
- state: 待判定的材料，原样发送：可含上下文、原话、事实清单、双方说法。 措辞、矛盾、时间线都别替它整理 —— 原样进去才有原样的分布。
- questions: {题名: 题型定义}，一次可问多题。每题给三样： type（题型）、instructions（这题到底在判什么）、criteria（判据）。 - "choice"：多选一。…… 形状：{"题名": {"type": "choice", …}}
```

（「参数：」段是**所有工具**共有的内核行为，不是 decide 特有的；续行被空格连接也是
`_parse_args_section` 的既有做法（`tool.py:133-138`）。）

**③ `function.parameters`**（`tool.py:85-99` + `_json_type` 的映射）：

```json
{
  "type": "object",
  "properties": {
    "state": {"type": "string",
              "description": "待判定的材料，原样发送：… 措辞、矛盾、时间线都别替它整理 —— 原样进去才有原样的分布。"},
    "questions": {"type": "object",
                  "description": "{题名: 题型定义}，一次可问多题。每题给三样：…"}
  },
  "required": ["state", "questions"],
  "additionalProperties": false
}
```

- `questions` 只能是 `{"type": "object"}`，**没有 `properties`** —— `_json_type` 对 `Dict[str, Any]`
  只给顶层类型（`tool.py:52-53`）。模型靠 `description` 学形状，靠入参校验（§3.4）拿回错误。
  这是内核级的限制，不是 decide 的懒：为它单独加一套嵌套 schema 生成器，等于给 `build_tool`
  开一条只有 decide 走的分支。
- `additionalProperties: false` 只写在**根**上（`tool.py:96`），所以 `questions` 里随便加题名是合法的。
- **上面三处不是手抄**：把 §3.2 那段代码直接 `exec` 后跑 `build_tool()`，digest 那一行用
  `registry.py:586` 的同一表达式（`signature_hint()` + `_first()`）拼出。同一脚本对「价格词表」
  逐个断言，`description` / digest / `schema` 三处全 `CLEAN`（§3.6）。改完 docstring 重跑一次，
  就不会出现「§3.2 改了、§3.5 没跟上」这种两份文案 —— 第一版方案里 `Returns` 段漏改
  `usage（含 cost）`，正是这条脚本当场抓出来的。


---

### 3.6 不描述调用开销（人类裁决，2026-11-05，第 3 + 4 轮）

**模型看得见的四处** —— digest 那一行、完整 `description`、Schema 的 `description`、**返回值** ——
都不带这类表述；服务端回的 `usage` 也不转发。**连文档（README 与本文档）也不写**（第 4 轮裁决）。

三条判据（按强度排）：

1. **调用前的偏置**：一次判定的代价，模型在当下没有任何可比对象 —— 它不知道这次误判意味着什么，
   也无法把任何数字与「少问一次」的收益放在同一个尺度上。给它一个数字，等于让它在算不平的账上
   取舍，结果只会偏向「为了省而不问」。
2. **调用后的残留**：工具结果会成为历史的一部分，**后面每一步都被重读**。这类信息一旦进了返回值，
   就不是一次提醒，而是整场会话里持续的暗示 —— 比在提示词里加一句「别滥用」影响更深。
3. **内核不是账房**：算账是运维的事，判定是模型的事，两件事不该塞进同一个字符串。

代价（诚实记账）：本地也没有任何逐次读数了。要把账落到本地，就得自己写第二份状态（§九 不做）。
**要判定 → 模型；要账 → 服务端。**

守它的是测试而不是措辞：`test_no_price_in_model_facing_text`（6.3 第 15 条）对模型可见的四处
逐个断言不含那张词表，改文案时它先红；`usage` 不转发则另由 `test_success_roundtrip` 守着。

---

## 四、门控（本方案的核心）

### 4.1 三个环境变量，一个默认值都没有

```
JEV_BASE_URL   **完整端点地址**（例：https://models.glms.com.cn/systemone/v1/systemone）
JEV_API_KEY    鉴权
JEV_MODEL      模型（例：jev-1.13.0）
```

- **为什么没有默认**：一个默认端点 = 替人决定「数据发给谁」；一个默认 model = 替人决定「用哪一版、花谁的钱」；一个默认 key 根本不存在。默认值在这里不是便利，是**越权**。
- **只认 `JEV_*` 一套名字**（已定）：不做 `OPENTHREE_JEV_*` 别名，也不做 `JEV_TIMEOUT` —— 多一个别名 = 多一条要维护的路径。
- **`JEV_BASE_URL` 是整串端点**（已定）：它已经是 `/systemone/v1/systemone` 那个地址，内核不做任何拼接（见 4.2）。
- **只读环境变量**（已定）：`config.yaml` / `openthree.yaml` / `~/.openthree/config.yaml` 一律不参与。于是 `decide` 的配置**只有一处真源**，不存在 `llm_config_diag` 那种「三层回退」——排查时看一眼环境就知道答案。

### 4.2 URL 原样使用，不拼路径

```python
url = base            # JEV_BASE_URL 就是完整端点，一个字符都不改
```

- **不拼** `/systemone/v1/systemone`、不补尾部 `/`、不做「如果已经写了路径就跳过」的补救：拼路径意味着存在第二份「端点长什么样」的知识，而它已经在你给的变量里了。想换端点就去改环境变量，不必改代码。
- **配置有效性 = 三个变量非空 且 base 是 http(s) 绝对地址**：

  ```python
  u = urlparse(base)
  ok = u.scheme in ("http", "https") and bool(u.netloc)
  ```

  这一条是**门控的一部分**，不是宽松校验：写成 `models.glms.com.cn/systemone/v1/systemone`（漏了 scheme）时，`http.client` 要到真发请求才炸，那一步已经白花了。与「没配置就不注册」走同一条路径：地址不合格就当没配置 —— 不注册，也不留别的痕迹。
- **两条测试**：整串地址原样出现在请求 URL 里（多一个 `/` 都不行）；漏 scheme 的地址 → 不注册。

### 4.3 判定时机：注册期，一次

- **在** `Runtime.__init__` 里注册内置工具的那一刻判定（`runtime.py:105`），整个进程不变。
- **不要**做成每步重读（像 `limits.json` / `config.yaml` 那样）。理由：`registry.reload()` 里 `tools = dict(self.builtins)`（`registry.py:197`），工具集是注册期产物；如果会话中途增减工具，`tools` 数组与 system 前缀会同时变化 —— 而 `prompt.py:161-162` 把「每步都会变的值」明确列为缓存前缀的禁区。
- **后果必须写进文档**：`export JEV_...` 之后要**重启进程**。不写这一句，第一次用的人会以为没生效。

### 4.4 唯一判定点与调用点

```python
# openthree/decide.py（新文件，纯标准库 + 复用 llm.post_json）
def _jev_settings() -> Tuple[Dict[str, str], List[str]]:
    """读环境变量 → (取值, 缺失或无效的变量名)。

    取值总是原样返回（门控不过也一样）：「能不能用」与「为什么不能用」是两个
    问题，后者要能区分「没设置」和「设置了但不合法」。"""

def _env_configured() -> bool:
    """这三个变量现在配好没有（去空白后非空）。只看环境，不代表工具在不在：
    工具在不在的唯一判据是 installed_builtins()（注册期冻结，见 4.4）。"""

def optional_builtins() -> Dict[str, Any]:
    """本次进程该额外注册的内置工具；条件不满足时返回空 dict。"""

# openthree/builtins.py
def installed_builtins() -> Dict[str, Any]:
    """本次进程真正注册的内置工具 = 常驻集 + 条件集。"""
    out = dict(BUILTIN_FUNCTIONS)
    out.update(optional_builtins())
    return out
```

需要同步改的注册点（共 2 处生产 + 若干测试夹具）：

| 位置 | 现状 | 改成 |
| --- | --- | --- |
| `runtime.py:105` | 遍历 `BUILTIN_FUNCTIONS` | 遍历 `installed_builtins()` |
| `cli_tool.py:110` | 遍历 `BUILTIN_FUNCTIONS` | 遍历 `installed_builtins()` |
| `tests/conftest.py:54`、`tests/test_builtins.py:180`、`tests/test_function_sources.py:69`、`tests/test_profile_sources.py:63` | 同上 | 同上 + 测试里显式清空 `JEV_*`（见 6.1） |

**`BUILTIN_FUNCTIONS` 的语义就此收窄为「常驻集」**，`installed_builtins()` 是「本次进程的实集」—— 两个名字要让人一眼看出谁是判据。`audit_functions` / `write_function` 的静态审计读的是 `KERNEL.registry.builtins`（`builtins.py:695`、`builtins.py:1059`），注册后自然正确，不用改。

### 4.5 缺席时的诚实边界

| 情况 | 表现 |
| --- | --- |
| 未配置 | 工具不存在：system 清单里没有、`schemas()` 里没有（2.2 有证据） |
| 模型仍试图调用（提示词里写着 decide） | `agent.py:1019` 回「未知工具 decide。当前可用：…」，一步纠正 |
| 用户在 `.openthree/functions/decide.py` 自己写了一个 | 那是**用户工具**，不受门控；内置 decide 同时启用时，用户那份按距离赢，记进 `overrides` |
| 已配置但服务不可用 | 每次调用返回 `[错误] ...`；不影响其它工具与循环 |

- **不**在 system 里写「本机有个 decide 但没配置」：那是往「一」里注入内核的文案（违反「一」的唯一真源），而且每步都要为这句话付费。真想让模型知道，用户可以自己写进提示词 —— 那是他的「一」。

---

## 五、传输、错误与重试（人看的，不进模型）

### 5.1 复用 `post_json`

```python
# openthree/llm.py
def post_json(url: str, api_key: str, payload: Dict[str, Any], *,
              timeout: float = 600.0, connect_timeout: float = 5.0) -> Dict[str, Any]:
    """向任意 JSON 端点 POST 一次，返回解析后的 JSON；失败抛 TransportError。"""

def post_chat(base_url, api_key, payload, *, timeout=600.0, connect_timeout=5.0):
    return post_json(base_url.rstrip("/") + _CHAT_PATH, api_key, payload,
                     timeout=timeout, connect_timeout=connect_timeout)
```

- **对外行为逐字节不变**：URL 拼接、头、代理、gzip、错误处理全在原处，只是「路径从哪来」变成了参数。
- **回归护栏**：`tests/test_llm.py` 已覆盖 `post_chat` 的正常 / 401 / 超时 / 非 JSON 路径，重构后必须先全绿再动 decide。

### 5.2 自动重试：2 次（共 3 次尝试），判据不新写一份

```python
_RETRIES = 2                       # 常量，不开环境变量
for attempt in range(_RETRIES + 1):
    try:
        return post_json(url, key, payload, timeout=_TIMEOUT,
                         connect_timeout=_CONNECT_TIMEOUT)
    except Exception as exc:
        if attempt == _RETRIES or not _retryable(exc):
            raise
        time.sleep(_backoff(attempt))
```

- **判据复用 `llm._retryable` / `llm._backoff`**（`llm.py:633-651`）：「抖动还是 4xx」这个问题只该有一个答案。`_retryable` 放行 `status=0`（DNS / 连接 / 超时）与 408/409/429/5xx，对 400/401/403/404 立刻放弃 —— 重试一个鉴权失败，只是把同一个错误再买一次。
- **必须回报 `attempts`**：与 `LLMClient.chat` 同名同义（`llm.py:532-533`、`llm.py:616`），`attempts > 1` 才说明重试过。
- **超时重试的语义要说清**：超时只说明响应没回来，**不代表服务端没受理**；重试是一次新请求。这条不写进 docstring —— 模型不需要 TCP 语义，`attempts > 1` 本身就是它需要知道的全部信号（§3.6）。
- **上限 3 次而非 LLM 的 4 次**：LLM 侧 `retries=3`（`llm.py:79`）是因为一次生成要等分钟级、宁可多试；判定是秒级的，3 次已够覆盖一次网关抖动。

### 5.3 超时：常量，不开旋钮

- connect 5s / read 60s，写在 `decide.py` 模块常量里。
- 不做 `JEV_TIMEOUT`：目前没有已知的慢场景，多一个环境变量就多一条要测的路径。真出现「判定要跑两分钟」的端点，再加不迟。

### 5.4 错误映射（每一行都要有测试）

| 情况 | 返回给模型的话 |
| --- | --- |
| `TransportError(status=0)` | `[错误] 连不上 JEV 端点（DNS/连接/超时）：…（检查 JEV_BASE_URL 与本机代理）` |
| 401 | `[错误] JEV 鉴权失败（401）：JEV_API_KEY 无效或未带` |
| 403 | `[错误] JEV 路由未授权（403）：key 有效，但这个 token 没被放行到 /systemone（需网关侧开通，不是换个 key 能解决的）` |
| 404 | `[错误] JEV 路由不存在（404）：JEV_BASE_URL 是否指向 models.glms.com.cn（另一台 host 对该路径返回 404，见 jev-test/jev.md）` |
| 429 | `[错误] JEV 限流或额度用尽（429）` |
| 5xx | `[错误] JEV 服务端错误（5xx，可稍后重试）` |
| 200 但非 JSON | `[错误] JEV 响应不是 JSON：<前 200 字>` |
| 200 但无 `answers` | `[错误] JEV 响应缺 answers：<前 200 字>` |

401 / 403 / 404 的区分直接来自 `jev.md:43-50` 的排障小抄 —— 这三条正好是「第一次接入」最可能撞上的三个坑，把判据写进错误文案，比写进文档更容易被读到。

### 5.5 密钥不外泄

- 请求头里的 `Authorization` 永不进返回值、不进日志；`recorder` 记录的是工具参数与输出（`agent.py:880-886`），两者都不含 key。
- 错误正文里若出现 key 字符串（服务端回显的极端情况），替换成 `***` 再返回。
- **没有常驻的诊断入口**（人类裁决，2026-11-06）。一个专为「默认不存在的工具」服务的
  常驻入口，会让每一步的工具清单都替它付字节，也会让模型去调一个不存在的功能；
  而它与门控是两套时序（门控在注册期判定一次、诊断在调用期读环境），天然会发散。
- 配没配看环境变量本身即可：三个 `JEV_*` 都在、`JEV_BASE_URL` 是 http(s) 绝对地址，
  重启进程后 `decide` 就在工具清单里。命令行尾钩只点名 `decide`，不交出 `globals()`
  —— 取环境的内部函数返回原始 key，把它变成子命令等于给密钥开一条打印路径。

---

## 六、测试清单（每条给判据）

新文件 `tests/test_decide.py`，全部**不打真网**（monkeypatch `decide.post_json`）。

### 6.1 门控

1. `test_missing_any_var_disables` —— 参数化：分别缺 1 个 / 缺 2 个 / 三个都缺 → `_env_configured() is False`，`installed_builtins()` 里没有 `decide`（谓词只说明环境配没配，工具在不在由 `installed_builtins()` 说）。
2. `test_all_three_enables` —— 三个都设 → 在 `installed_builtins()` 里，且注册后 `reg.get("decide").origin == "builtin"`。
3. `test_no_default_endpoint` —— 环境全空时，`installed_builtins()` 不含 `decide`。**判据：任何「不配置也能用」的路径都不存在。**
4. `test_bad_base_url_disables` —— 参数化：漏 scheme / 非 http(s) / 端口写错（含 `:0`）/ 没有主机名 / 带空格 / 非 ASCII → 不注册。**判据：地址写错与没配置走同一条路径。**
5. `test_no_diagnostic_entry` —— 工具清单里以 `decide` 开头的工具只有 `decide` 一个，模块上也没有第二个公开的 `decide*` 入口。**判据：未启用的功能不留任何痕迹，也不存在「它为什么不在」的第二个答案。**
6. `test_registered_as_builtin` —— 启用时 `reg.get("decide").origin == "builtin"`，`signature_hint()` 为 `decide(state: string, questions: object)`。
   （同时守住 2.2 那三条证据：提示词、schema、计数 —— 禁用时 digest 与 `reg.schemas()` 里都没有它。）
7. `test_tests_are_env_clean` —— autouse 夹具清空 `JEV_*`：**本机配了 JEV 时，测试也必须绿**。否则「CI 绿、本机红」是必然。

### 6.2 形状与校验

8. `test_schema_shape` —— `build_tool(decide)`：`required == ["state", "questions"]`，`questions.type == "object"`；`description` 与 `digest()` 那一行都含「外部」（文案不变量：数据出口必须在第一屏），且**价格词表一个都不出现**（§3.6，与第 15 条同表）。
9. `test_validation_never_sends` —— 空 state / 空 questions / 未知 type / choice 缺 criteria / score criteria 不是 list / 缺 instructions，各一条：返回以 `[错误]` 开头，且**假传输的调用计数为 0**（用「一调就抛」的假函数证明没发出去）。

### 6.3 调用与返回

10. `test_success_roundtrip` —— 假传输返回 `jev.md:11-22` 的样本：断言输出含 `answers` 原样、`model` 快照号、`attempts`，**且不含 `usage` / `cost`**（§3.6：服务端回了也不转发）；断言发给假传输的 payload 里 `state` 与 `questions` **逐字段相等**（内核不改写输入）。
11. `test_request_url_verbatim` —— 假传输收到的 URL 与 `JEV_BASE_URL` **逐字符相等**（不补 `/`、不拼路径）。
12. `test_retry_policy` —— 参数化：`status=0` / 429 / 503 → 重试到 3 次后返回错误且 `attempts == 3`；400 / 401 / 403 / 404 → **只发 1 次**、`attempts == 1`。用计数假传输证明次数，不靠日志。
13. `test_error_mapping` —— 401 / 403 / 404 / 0 / 429 / 500 / 非 JSON 各一条：返回 `[错误] …`、含对应关键词、**不抛异常**。
14. `test_key_never_echoed` —— 假传输抛出的正文里塞进 api_key，断言返回文本里没有它、有 `***`。
15. `test_no_price_in_model_facing_text` —— **文案不变量**，守 §3.6。四处逐个断言不含价格词表
    （`计费|费用|价格|价钱|单价|收费|花钱|成本|cost`）：
    (a) `build_tool(decide).description`；(b) 每个 `properties[*]["description"]`（schema 会随
    `tools` 请求发出去）；(c) `Registry.digest()` 里 decide 那一行；(d) 一次成功调用的返回文本
    —— 假传输喂 `jev.md:11-22` 那段**带 `usage.cost`** 的样本，验证「服务端会回、我们不转发」。
    判据：把 `usage` 加回返回值、或在 docstring 里补一句「按量计费」时，这条测试先红。

### 6.4 CLI 与回归

16. `test_cli_keeps_internal_settings_off_the_command_line` —— 子进程跑 `python -m openthree.decide`：取环境的内部函数不是子命令（exit ≠ 0），`--help` 里不出现任何内部函数名、也不出现 key。
17. **回归** —— `tests/test_llm.py` 全绿（`post_json` 重构的护栏）+ 全仓库测试全绿。

### 6.5 不打真网，但要有一次真实验收

- 实网不放进 pytest（CI 没有 key，也没有理由为每次提交付费）。
- 实现完成后手动跑一次：`python -m openthree.decide decide --state "…" --questions "{\"is_urgent\":{\"type\":\"noul\",\"instructions\":\"是否表达了紧迫性？\"}}"`，把**真实返回**贴进提交说明。
  （`questions` 在命令行上是**一段 JSON 字符串** —— object 型参数在 `cli_tool._add_one` 里就是 `str`，见 `cli_tool.py:223-235`；这正是 3.1 要求它同时接受字符串的原因。）
- **实现前必做的一次协议确认**：用**一正一反两个 state**（例如「客户演示马上开始，按钮找不到」vs「有空看看文档」）各打一次 `noul`，确认 `noul` 的语义方向与字段接受度。目前只有一个样本（`jev.md:11-22`），而 `noul` 的解释会直接写进 docstring —— 猜错方向，模型就会把「不太紧急」读成「紧急」。

---

## 七、文档与代码同步

| 文件 | 改什么 |
| --- | --- |
| `README.md:22-43`（配置节） | 加一小节：decide 需要三个环境变量，缺一不出现；给三行示例；指向本方案 |
| `README.md:682-694`（安全声明） | 加一条：`decide` 会把 `state` 与 `questions` 发往 `JEV_BASE_URL` 指向的外部服务（第三方端点，不在 openthree 的安全边界内）。**只说外发，不写任何开销表述**（§3.6） |
| `README.md:136` | 「36 个工具（内置 33 / 自造 3）」是**示例输出**，会随工具集漂移；顺手改成不写死数字，或标注「示例」 |
| `docs/decide-tool-proposal.md` | 本文档 |

---

## 八、实施顺序（可分批，每步都能单独回滚）

1. **`llm.post_json` 重构**：纯搬运，`post_chat` 三行包装；跑全测试，必须全绿。此时 decide 还一个字都没有。
2. **`decide.py`**：门控 + 校验 + 调用 + 错误文案 + 模块 CLI 尾钩；配 `tests/test_decide.py` 的 6.2–6.4。**先不接任何注册点**，此时它对 agent 不可见。
3. **接上判定层**：`installed_builtins()` + `runtime.py:105` / `cli_tool.py:110` + 测试夹具；补 6.1 的门控测试（含 autouse 清 env）。
4. **文档**：README 两处 + 工具数那一行。
5. **实网验收**：手动打一次，把返回样本贴进提交说明；同时确认 `noul` 方向（6.5）。

每一步都可单独合并 —— 第 1 步即使最后决定不做 decide，也是一次没有行为变化的重构。

---

## 九、明确不做

- **不做诊断入口**（人类裁决，2026-11-06）：没有 `decide_status`、没有 `decide_diag`，也不加 `--status` 全局开关（`run_cli` 的全局开关只有 `--json` / `--quiet` / `--traceback`，`cli_tool.py:151-156`）。没配置就等于没有这个工具，配没配看环境变量本身。
- **不做开销回显 / 累计 / 上限**：服务端回的 `usage` 一律**不转发、不落盘、不累计**（§3.6）。累计值要么落盘（第二份状态），要么每步重算（缓存前缀），都不值。
- **不做多后端**：现在是「decide = JEV」。将来接第二个决策模型时再加 `provider` 参数 —— 现在预留一个空参数，就是让模型每次都要填一个它填不出意义的值。
- **不做提示词自动插入**：内核不替模型决定「什么时候该用 decide」。
- **不把任何密钥 / 内网地址写进代码、测试、文档**：`JEV_BASE_URL` 的例子可以写（`jev.md` 里就是它），key 一个字符都不抄。
- **不自动截断 `state`**：静默截断会让判定对象与模型以为的不一致。

---

## 十、已定与待确认

### 10.1 已定（人类裁决，2026-11-05）

| # | 问题 | 裁决 | 落在哪 |
| --- | --- | --- | --- |
| 1 | 变量命名 | `JEV_BASE_URL` / `JEV_API_KEY` / `JEV_MODEL`，不设别名 | §4.1 |
| 2 | `JEV_BASE_URL` 语义 | **完整端点地址**，内核原样使用、不拼路径 | §4.2 |
| 3 | `config.yaml` 覆盖 | **不覆盖**，只读环境变量 | §4.1 |
| 4 | 自动重试 | **要**：2 次（共 3 次尝试），复用 `llm._retryable` / `_backoff`，回报 `attempts` | §5.2 |
| 5 | `state` 长度上限 | **先不加**（不截断、不改写） | §3.4 / §九 |
| 6 | 调用开销（第 3 + 4 轮裁决） | **一个字都不提**：模型可见四处不写、`usage` 不转发不落盘、**连文档（README / 本提案）也不写**。守它的是 6.3 第 15 条测试与 `test_success_roundtrip` | §3.6 / §九 |
| 7 | 常驻诊断入口（2026-11-06） | **不要**：`decide_status()` 整条移除。未启用的功能不留痕迹；配没配看环境变量本身 | §5.5 / §九 / §6.1 |

### 10.2 已确认：`noul` 的方向（2026-11-05 实网验收）

两个 state 各打一次同一道 `noul` 题（「这条消息是否表达了紧迫性？」）：

| state | `noul` |
| --- | --- |
| 「我的发票下载按钮找不到了，已经等了3天，客户演示马上开始。」 | **0.96** |
| 「有空的时候看看文档里关于导出格式的说明，不急。」 | **0.03** |

→ 结论：**越大越倾向「是」**。docstring 里那句「0–1 的连续值（越大越倾向『是』）」由此成立；
不需要改接口，也不需要更细的解释（内核不解释数值这件事没变）。

---

## 附：为什么这算「最大化模型能力」，而不是给它一个拐杖

模型最擅长的不是「拍板」，而是**组织问题**：把一团模糊的材料拆成「该归哪类、是否紧急、不满到什么程度」这样的判据，正是它已经会做的事。JEV 补的是另一半 —— 给定判据后的**后验分布**，而这恰恰是自回归模型最不擅长给的东西（它只会给一个叙述，且不知道自己有多确定）。

于是分工是：

- 模型负责**问什么**（`state` + `questions` 由它写）；
- JEV 负责**给定判据下的分布**；
- 内核只负责**搬运与如实报告**（不解释、不阈值、不缓存；抖动的重试只在传输层，且如实回报 `attempts`）。

三者各守一段，没有一段是硬编码的智能 —— 这正是「道」要求的样子。
