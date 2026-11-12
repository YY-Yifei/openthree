# openthree profile：复用与组合（使用者手册）

> **一句话**：`-profile` 是一个**可重复的目录列表**，每一项是一个「含 `.openthree/` 的工作区」，
> 它只交出其中的 `.openthree/functions/*.py`。**组合 = 按你想要的优先级把几个目录列一遍**；
> 同名只有一条裁决依据：距离（越近越赢），本项目恒最近。
>
> 这份文档只讲**怎么用**：怎么造、怎么写、怎么组合、怎么确认生效、哪里会静默不生效。
> 设计理由见 `docs/profile-functions-proposal.md`；一句话摘要见 `README.md` 的「function 从哪来」一节。

**读法**：§0 照抄一遍就会用了；§2 是模板库；§4 是六个场景配方；§7 是排错表；其余按需查。

标注约定：**「实测」** = 本文写作时在本机真跑过并贴了输出；**「代码」** = 读源码得出（位置见附录 A）。

---

## 0. 五分钟跑通（照抄）

### 0.1 造一个 profile

profile 首先得是一个**工作区**（含 `.openthree/` 的目录）：

```bash
# Windows cmd
openthree -C %USERPROFILE%\.openthree-profiles\base --init

# bash / zsh
openthree -C "$HOME/.openthree-profiles/base" --init
```

`--init` 只铺工作区、**不调模型**，零成本。

> **一个坑先说**：`-C` **不会**展开 `~`。实测 `-C ~/x --init` 会在**当前目录**下
> 建一个名字就叫 `~` 的目录。要让**shell** 替你展开 —— Windows 用 `%USERPROFILE%`，
> Unix 用 `$HOME`。（`-profile` 相反：它**自己**展开 `~`，见 §3.5。）

输出（实测，节选）：

```
[ok] 工作区已就绪 …\.openthree-profiles\base\.openthree
工具     38 个（内置 38 / 自造 0）
```

**顺带一个好消息**：**任何已有项目都能直接当 profile** —— 它本来就是一个含 `.openthree/`
的目录。所以「把上个项目的工具挂过来」不需要任何准备。

### 0.2 放一个工具

新建 `<base>/.openthree/functions/text_kit.py`，内容照抄：

```python
"""文本小工具：统计与 slug 化。

一个 profile 能力包的最小完整形态：工具函数 + 使用纪律（__system_prompts__）
+ 可以直接当命令行程序跑。
"""

__system_prompts__ = """
## 用 text_kit 时的纪律
- 要字数 / 行数时调 `word_stats`，不要自己数 —— 它对中英文混排按字符计，口径统一。
- 生成文件名或 URL 片段用 `slugify`，它只保留 ASCII 字母数字与连字符。
"""


def word_stats(text: str) -> str:
    """统计一段文本的字符数、词数、行数。

    Args:
        text: 任意文本；「词」按空白切分。

    Returns:
        一行统计结果；参数不是字符串时返回错误说明而不是抛异常。
    """
    if not isinstance(text, str):
        return f"[错误] text 必须是字符串，收到 {type(text).__name__}"
    chars = len(text)
    words = len(text.split())
    lines = len(text.splitlines())
    return f"字符 {chars}，词 {words}，行 {lines}"


def slugify(text: str) -> str:
    """把一段文本转成 URL / 文件名安全的 slug。

    Args:
        text: 任意文本；非 ASCII 字符会被替换成连字符。

    Returns:
        小写、单连字符分隔的 slug；全被过滤掉时返回空字符串。
    """
    out = []
    for ch in text:
        out.append(ch.lower() if (ch.isalnum() and ch.isascii()) else "-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def __main__(argv=None) -> int:
    """命令行入口（dunder 不会被注册成工具）。"""
    from openthree.cli_tool import run_cli

    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
```

### 0.3 先单独跑一下它（可选，但推荐）

```bash
python %USERPROFILE%\.openthree-profiles\base\.openthree\functions\text_kit.py --help
```

实测输出（节选）：

```
usage: text_kit.py [-h] [--json] [--quiet] [--traceback] 子命令 ...

function CLI：每个公开函数就是一个命令。

positional arguments:
  子命令
    word_stats  统计一段文本的字符数、词数、行数。
    slugify     把一段文本转成 URL / 文件名安全的 slug。
```

调用（实测）：

```bash
python text_kit.py word_stats --text "Hello world"
# 字符 11，词 2，行 1

python text_kit.py slugify --text "Quarterly Report: Q3 2026"
# quarterly-report-q3-2026
```

`--json` 是给其他 agent 的稳定接口：

```bash
python text_kit.py word_stats --json --text "abc"
# {"ok": true, "fn": "word_stats", "result": "\u5b57\u7b26 3\uff0c\u8bcd 1\uff0c\u884c 1"}
```

（`\u5b57\u7b26` 就是「字符」—— JSON 规范要求转义，不是 bug。）

> **单函数文件不需要子命令**：一个文件里只有**一个**公开函数时，参数直接平铺——
> `python team_rules.py --lang zh`，而不是 `team_rules.py commit_style --lang zh`
> （代码 `cli_tool.py:298`：`single = len(tools) == 1`）。

### 0.4 挂到项目上

```bash
openthree -C D:\work\myproj -profile %USERPROFILE%\.openthree-profiles\base "把 README 的错别字挑出来"
```

### 0.5 确认真的挂上了

```bash
openthree -C D:\work\myproj -profile %USERPROFILE%\.openthree-profiles\base --inspect
```

实测输出（节选，重点看三行）：

```
profile  …\.openthree-profiles\base（额外 2 个工具；同名时本项目优先）
工具     40 个（内置 38 / 自造 2）
         ↳ slugify  ← text_kit.py
         ↳ word_stats  ← text_kit.py
```

到这儿你已经会用 profile 了。下面讲「写什么、怎么组合、哪里会静默失效」。

---

## 1. 一个 profile 长什么样

```
~/.openthree-profiles/base/            ← 「项目根」
└── .openthree/                        ← 有它才算 profile
    └── functions/
        ├── text_kit.py                ← 每个 .py 都被加载成工具
        ├── _util.py                   ← `_` 开头：不加载成工具，可被同目录 import
        └── writing_style.py           ← 只有 __system_prompts__：贡献说明书，不贡献工具
```

| 位置 | 从 profile 读吗 | 为什么 |
|---|---|---|
| `.openthree/functions/*.py` | ✅ 自动加载 | 能力：换一个项目仍然成立 |
| `functions/*.py` 里的 `__system_prompts__` | ✅ 拼进 system 末尾 | 能力自带的使用说明，跟着能力走 |
| `.openthree/memory/`、`config.yaml`、`prompts/`、`identity.json`、`limits.json` | ❌ 一个都不读 | 身份与经历：它们描述「这具躯体」，换到别的项目就不成立 |

**这一条是静默的**：往 profile 里放 `identity.json` 或 `system-prompts.md`，内核
**不会报错，也不会读** —— 它就当那里什么都没有。

判据一句话：**能力可共享，身份不可共享**。拿不准就问「这句话在别人的项目里还成立吗」。

---

## 2. 模板库（复制即用）

### T1 单函数最小文件

```python
"""团队约定：单函数能力包的最小形态。"""


def commit_style(lang: str = "zh") -> str:
    """返回本团队的提交信息格式约定。

    Args:
        lang: "zh" 返回中文说明，"en" 返回英文说明。

    Returns:
        格式约定文本。
    """
    if lang == "en":
        return "type(scope): summary — imperative, <= 72 chars, no trailing period."
    return "type(scope): 一句话摘要 —— 祈使句，<= 72 字符，句末不加句号。"


def __main__(argv=None) -> int:
    """命令行入口。"""
    from openthree.cli_tool import run_cli

    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
```

实测：

```bash
python team_rules.py --lang zh
# type(scope): 一句话摘要 —— 祈使句，<= 72 字符，句末不加句号。
```

四条要点：

- 模块 docstring 写「这个包干什么」；
- 每个公开函数：类型注解 + `Args:` / `Returns:`（它们变成 JSON Schema 与工具描述）；
- 出错**返回字符串，不抛异常**（异常会中断调用链）；
- 尾钩 `__main__`，让它同时是一个能单独跑的命令行程序。

### T2 多函数 + 使用纪律

完整文件见 §0.2 的 `text_kit.py`。相对 T1 多两样：

**（1）多个工具。** 每个公开函数一个工具；CLI 也随之变成「子命令」形态。

**（2）`__system_prompts__`：这个能力自带的纪律。**

```python
__system_prompts__ = """
## 用 text_kit 时的纪律
- 要字数 / 行数时调 `word_stats`，不要自己数 —— 它对中英文混排按字符计，口径统一。
- 生成文件名或 URL 片段用 `slugify`，它只保留 ASCII 字母数字与连字符。
"""
```

挂上 profile，工具和说明书**一起到位**（实测：`附随提示词 2 段 / 163 字符`）。

**为什么值得写**：只给工具不给纪律，模型就会拿它乱用。说明书跟着能力走，挂到哪个
项目都带着 —— 这是 profile 相对「复制一个 .py」真正的价值。

### T3 纯提示词文件

一个文件只有 `__system_prompts__`、没有工具，**是合法用法**，不会被当成坏文件：

```python
"""只有说明书、没有工具的纯提示词文件（合法用法）。"""

__system_prompts__ = """
## 写作风格
- 中文技术文档：先结论、后依据；不用「首先 / 其次」这类空转连接词。
"""
```

实测：挂上后**工具数不变**，说明书从 1 段变 2 段（119 + 44 = 163 字符）。

**用途**：把「这一类活的共同约定」（写作风格、审查口径、目录规范）抽成一个包，
不必为了装一段文字而硬造一个工具。

### T4 辅助模块

`_` 开头的文件**不会被加载成工具**，适合放同目录工具要 `import` 的公共代码：

```python
"""辅助模块：`_` 开头的文件不会被当成工具，但同目录的工具可以 import 它。"""


def split_lines(text: str) -> list:
    """按行切分（辅助函数，不是工具）。"""
    return text.splitlines()
```

实测：`_util.py` 存在时工具数不变（代码：`registry.py` 里 `_` 前缀跳过）。

### T5 会阻塞的工具

网络 / 子进程 / socket / 轮询类工具，**超时必须自己扛**：内核调用工具没有任何超时，
一个永不返回的工具会让整场任务停在那一步（撞不到步数上限，日志停在上一行）。

写之前先调 `tool_timeout_guide()` 拿骨架：`timeout` 做成**参数**（默认值给个合理数），
用 `time.monotonic()` 算 deadline，绝不读 stdin。

### T6 交稿前自查

- [ ] 模块 docstring 有一句「这是干什么的」
- [ ] 每个公开函数有类型注解 + `Args:` / `Returns:`
- [ ] 辅助函数以 `_` 开头（否则会被注册成工具）
- [ ] 首参数没有叫 `kernel`（那会被运行期注入，模型看不到它）
- [ ] 出错返回字符串，不抛异常
- [ ] 有尾钩 `__main__` + `if __name__ == "__main__"`
- [ ] `python 这个文件.py --help` 跑得通
- [ ] 真需要 `__system_prompts__` 吗（有纪律要交代就写，没有就别凑字数）

---

## 3. 组合：只有一条规则

### 3.1 距离

| 档 | distance | 谁说了算 |
|---|---|---|
| 本项目 `.openthree/functions` | `0` | 永远最内层，**永远赢** |
| 第 1 个 `-profile` | `1` | 命令行先写的 |
| 第 2 个 `-profile` | `2` | 依次 +1 |
| `~/.openthree/functions` | `1_000_000` | 只发现，要用 `register_function` 才生效 |

### 3.2 三条结论

1. **同名 → 近者赢。**
2. **profile 之间 → 先写赢。** 顺序反了，赢家就反了。
3. **每次覆盖都留痕**：记进 `list_functions` 的「同名覆盖」一节（正常现象，不是错误）。

### 3.3 实测

**顺序决定赢家**（两个 profile 各有一个同名 `word_stats`）：

| 挂载顺序 | 谁赢 | 调用结果 |
|---|---|---|
| `-profile "base,team"` | base（d=1） | `字符 3，词 1，行 1` |
| `-profile "team,base"` | team（d=1） | `团队版：3 字符` |

**本项目压过所有 profile**（项目里也有一份 `text_kit.py`，只定义了 `word_stats`）：

| 挂载 | 额外工具 | 工具总数 | 谁赢 |
|---|---|---|---|
| 项目有 `word_stats` + `-profile base` | **1**（只剩 `slugify`） | 40（内置 38 / 自造 2） | 项目赢 `word_stats`；`slugify` 仍来自 profile |

由此得一条**必须知道**的规则：

> **同名裁决按「工具名」，不是按「文件」。**
> 项目里那个 `text_kit.py` 只定义了 `word_stats`，于是只有 `word_stats` 被盖住；
> profile 里同一个文件中的 `slugify` 照常生效。

### 3.4 两个数字口径不同（最容易看错）

`--inspect` 里有两个数，**它们不是一回事**：

```
profile  …\base（额外 1 个工具；同名时本项目优先）   ← 只数「profile 真正贡献的」
工具     40 个（内置 38 / 自造 2）                  ← 「自造」= 所有非内置，含 profile 的
```

而 `list_functions()` 里：

```
## 自造（1）—— 你的演化成果                     ← 只数 d=0（本项目自己的）
## 来自 profile（1）—— `-profile` 点名的目录，自动加载   ← profile 的单独一节
```

**三个数对不上是正常的**：横幅的「自造」含 profile，`list_functions` 的「自造」不含。
要对账，只看 `list_functions` 的「来自 profile」一节。

### 3.5 命令行怎么写

```bash
# 逗号分隔
openthree -C myproj -profile "team,base" "任务…"

# 重复给（等价，按出现顺序拼接）
openthree -C myproj -profile team -profile base "任务…"

# 混着写：a,b 然后 c → 顺序是 a, b, c
openthree -C myproj -profile "a,b" -profile c "任务…"
```

- **顺序就是优先级**：最具体、最该赢的写最左边（PATH 语义）。
- `~` **会展开**（openthree 自己做的；实测 `-profile ~/nope-prof` →
  `C:\Users\<你>\nope-prof`）。`-C` **不会**（见 §0.1）。
- 路径本身含逗号时只能重复给 —— 没有转义写法。
- **打错路径 → exit 2**，并提示怎么造；而且**不会**顺手把项目工作区建出来
  （校验发生在 Runtime 构造之前）。实测：

```
[错误] -profile 有问题：
       C:\Users\…\nope-prof：不是一个工作区（这个目录本身不存在） —— 缺 …\nope-prof\.openthree
       profile 是一个**含 .openthree/ 的项目目录**（与 -C 同构）；先用 `openthree -C <该目录> --init` 把它的工作区铺出来。
```

---

## 4. 六个配方

### R1 个人基础包（跨项目都要用）

```
~/.openthree-profiles/base/     # 文本、文件、小脚本：每个项目都挂
```

```bash
openthree -C D:\work\projA -profile %USERPROFILE%\.openthree-profiles\base "…"
```

### R2 团队包（跟着 git 走）

把 profile 放进仓库，同事 clone 后挂相对路径即可：

```
my-team-repo/
├── tools/
│   └── .openthree/functions/…     ← 团队能力包
└── …（业务代码）
```

```bash
git clone git@…:my-team-repo.git
openthree -C myproj -profile ./my-team-repo/tools "…"
```

好处：**能力包有版本、有 review、有历史**。相对路径可用（实测）。

### R3 按职能拆小包，按需挂

```
~/.openthree-profiles/base      # 每个项目都挂
~/.openthree-profiles/pdf       # 只有处理 PDF 时挂
~/.openthree-profiles/review    # 只有验收 / 评审时挂
```

```bash
openthree -C proj -profile "%USERPROFILE%\.openthree-profiles\base,%USERPROFILE%\.openthree-profiles\pdf" "…"
```

**挂得多不等于更强**：每个工具都进 schema（占 token），说明书还共享 12000 字符额度。

### R4 项目私有覆盖（只改一个工具）

```
proj/.openthree/functions/text_kit.py       # 只定义 word_stats（项目版）
~/.openthree-profiles/base/…/text_kit.py    # 定义 word_stats + slugify（共享版）
```

结果：`word_stats` 用项目版，`slugify` 用共享版（实测见 §3.3）。
**本项目 d=0 永远赢** —— 这是「局部定制」的正规姿势，不需要 fork 整个能力包。

### R5 长熟之后提升到共享层

```python
# 1) 先在项目里长出来（target 留空 = 本项目）
write_function(name="pdf_kit", code=…)

# 2) 用顺手了，提到共享层
write_function(name="pdf_kit", code=…, target="~/.openthree-profiles/pdf")
```

- `target` 收的是 **profile 根目录**，不是它的 `functions` 目录（写入会自动建 `functions/`）。
- **只能写进已挂载的 profile**：写向没挂载的地方必然是「写了不生效」，所以直接拒绝，
  并列出可写位置。
- 落到共享层时，回执会**逐个工具名**回答「它在本项目里会不会生效」。
- 提升后本项目那份（d=0）继续赢；别的项目挂上 profile 就拿到同一份。

### R6 用别人的包（先审说明书）

`__system_prompts__` 是一条**能往你 system 里写字的通道**（有上限、带来源标注）。
挂之前先看它到底写了什么，三种看法：

1. **最直接**：打开那个包里的 `.py`，看 `__system_prompts__`；
2. **看拼进去的全文**：`read_prompts()` 会带上「附随提示词」整段（代码 `builtins.py:1802-1812`）；
3. **看落盘快照**：`.openthree/logs/attached-prompts/<sha8>.md`（按内容寻址）。

已经跑起来之后，再用 `list_functions()` 的「附随提示词」一节核对来源与字符数。

---

## 5. 说明书的规矩

1. **额度**：单文件 **4000** 字符（超出截断并写明原文多长）、全部合计 **12000** 字符。
   超了按**距离由近到远**保留，被挤掉的记进 `reload_functions` 的报告 —— 不静默，但不生效。
2. **按文件收，不按「赢家」收**。两个 profile 有同名工具时：工具按距离裁决，
   **两份说明书都进 system**（包括被顶掉那一份）。
   实测：项目版与 profile 版都写了 `__system_prompts__` → `附随提示词 2 段 / 172 字符`
   （项目 53 + profile 119），**被顶掉那份的说明书仍在**。
   所以：**别让两个包对同名工具各写一套**；两份打架时没有任何东西替你裁决。
3. **不读** `.openthree/prompts/system-prompts.md`（那是身份）。要带说明书，只能走
   `__system_prompts__`。
4. 说明书与「一」（`system-prompts.md`）冲突时，**以「一」为准**。

---

## 6. 四个读数：确认真的挂上了

1. **`--inspect` 横幅**：报**绝对路径**（两个同类目录都叫 `base` 时能区分），
   后跟「额外 N 个工具；同名时本项目优先」。`↳` 行列出 profile 贡献的工具，
   但**只给文件名** —— 两个同名文件分不清，要分清楚用下一条。
2. **`list_functions()`**：分节 —— 内置 / **自造（d=0）** / **来自 profile（绝对路径 +
   `[profile:…, d=N]`）** / **同名覆盖** / **附随提示词**。实测：

```
## 自造（1）—— 你的演化成果
- `word_stats(text: string)`  ← text_kit.py

## 来自 profile（1）—— `-profile` 点名的目录，自动加载
- `slugify(text: string)`  ← …\base\.openthree\functions\text_kit.py  [profile:…\base, d=1]

## 同名覆盖（1）—— 按距离裁决，近者生效（正常现象）
- `word_stats`：text_kit.py [project] 盖住 text_kit.py [profile:…\base]

## 附随提示词（2 段）—— 拼进 system 末尾，不是工具，模型调不到它
  - …\proj\.openthree\functions\text_kit.py（project，53 字符）
  - …\base\.openthree\functions\text_kit.py（profile:…\base，119 字符）
```

3. **`lookup_functions()`**：按来源由近到远列文件与每个工具**此刻的状态**
   （已生效 / 会被更近的盖住 / 注册后会盖住谁）。它是纯发现，随便调。
4. **`write_function(target=…)` 的回执**：回答「这份在本项目里生效吗」。

反向诊断也一样：**工具不见了 → 先看「同名覆盖」，再看「加载异常」**。

---

## 7. 排错表

| 现象 | 真相 | 出路 |
|---|---|---|
| 打错 profile 路径 | exit 2，且**不会**建出项目工作区 | 修路径；先 `-C <目录> --init` 造 profile |
| `-C ~/x` 建了个名为 `~` 的目录 | `-C` 不展开 `~`（`-profile` 才展开） | 用 `%USERPROFILE%` / `$HOME` |
| `-profile` 指到项目自己 | 提示「已忽略」，不算错误（本项目本来就 d=0） | 删掉那一项 |
| 同一目录点名两次 | 按先写的（更近）算 | 无妨 |
| profile 里没有 `functions/` 目录 | 提示「暂时不贡献任何工具」，**仍算已挂载** | 直接 `write_function(target=…)`，目录会自动建 |
| profile 里放了 `system-prompts.md` / `identity.json` | 静默不读 | 说明书改走 `__system_prompts__` |
| 想让 profile 再引用 profile | 它没有「再引用」的机制（只贡献 `functions/*.py`） | 命令行上一次列全 |
| `-profile` 配 `-u/--upgrade` 或 `--check` | exit 2，刻意拒绝（它们扫的是「本项目对本机内核」） | 别带 |
| 运行期改了 profile **列表** | 不生效（构造 Runtime 时就定型了） | 重启 |
| 改了 profile 里的**文件内容**、或**新增**了文件 | 不用重启 | `reload_functions()` |
| 项目里那份同名文件**没加载成功** | **profile 那份顶上来生效**，错误进「加载异常」 | 看 `list_functions` 的加载异常 —— 「我改的没生效」有时是它压根没加载成功 |
| 工具数对不上（横幅 vs `list_functions`） | 两个数口径不同（§3.4） | 看 `list_functions` 的「来自 profile」 |
| 想让 A2A daemon 也带 profile | `-d` / `-A2A` / `-i` 共用同一个 `Runtime` | 启动时一起写上 |

几条会出现在终端里的原话，照抄下来对照（都实测）：

```text
# profile 路径不存在 → exit 2，且不会建出项目工作区
[错误] -profile 有问题：
       …\nope-prof：不是一个工作区（这个目录本身不存在） —— 缺 …\nope-prof\.openthree
       profile 是一个**含 .openthree/ 的项目目录**（与 -C 同构）；先用 `openthree -C <该目录> --init` 把它的工作区铺出来。

# -profile 与 -u/--check 同用 → exit 2
[错误] -u/--upgrade 与 --check 不接受 -profile —— 它们扫的是「本项目对本机内核」的漂移，profile 不是本项目的一部分。

# 同一目录点名两次 → 正常启动，只提示一次
[提示] …\base：重复点名，按先写的（更近）算。

# -profile 指到项目自己 → 正常启动，profile 行整个消失
[提示] …\proj2：与 -C 的项目目录是同一个，已忽略 —— 本项目本来就是最内层（d=0），不需要挂自己。
```

---

## 8. profile 不共享什么

```
✅ 共享：functions/*.py、__system_prompts__
❌ 不共享：memory / config.yaml / prompts / identity.json / limits.json / plan
```

理由：前者是**能力**（换个项目仍成立），后者是**这具躯体的经历与设置**。
这一条是静默的 —— 放了不会报错，只是不会被读。

---

## 9. 把启动命令固化

profile 列表**刻意没有**写进 `config.yaml` 或环境变量：点名即授权，
「从哪加载」不该在运行中漂移。所以固化落在外层：

```bat
:: run.cmd（Windows）
@echo off
openthree -C "%~dp0" -profile "%USERPROFILE%\.openthree-profiles\base,%USERPROFILE%\.openthree-profiles\team" %*
```

```bash
#!/usr/bin/env bash
# run.sh
exec openthree -C "$(dirname "$0")" -profile "$HOME/.openthree-profiles/base,$HOME/.openthree-profiles/team" "$@"
```

好处：**「挂了什么」永远在可见的启动命令里** —— 不是埋在某份 yaml 里的隐式状态。

---

## 10. 一页速查

```
造：     openthree -C %USERPROFILE%\.openthree-profiles\base --init
写：     write_function(name=…, code=…, target="~/…/base")    # 只能写已挂载的
组合：   openthree -C proj -profile "team,base" "任务"          # 逗号 = 重复给
优先级： 本项目(0) > 第1个(1) > 第2个(2) > … > ~/.openthree(1e6，要登记)
同名：   近者赢，按**工具名**裁决；留痕在 list_functions「同名覆盖」
说明：   __system_prompts__ 随 profile 走；单文件 4000 / 合计 12000，按文件收
不共享： memory / config / prompts / identity / limits / plan（静默不读）
查：     --inspect 横幅 ｜ list_functions ｜ lookup_functions ｜ write_function 回执
坑：     -C 不展开 ~；横幅「自造」含 profile，list_functions 不含
```

---

## 附录 A：机制与源码位置

给要读内核的人（使用者不必看）：

| 行为 | 位置 |
|---|---|
| `-profile` 定义（`action="append"`，可重复、可逗号分隔） | `openthree/cli.py:103-108` |
| `-C` 定义（只是一个路径字符串，**不** expanduser） | `openthree/cli.py:100` |
| profile 校验 + `~` 展开 | `openthree/cli.py:553`（`Path(item).expanduser().resolve()`） |
| 距离语义与常量 | `openthree/workspace.py`：`FunctionSource.distance`、`USER_FUNCTION_DISTANCE` |
| 三档来源生成 | `openthree/workspace.py`：`function_sources` |
| `_` 前缀跳过 / 纯提示词文件合法 / 说明书额度 | `openthree/registry.py`（模块头常量与 `reload`） |
| 说明书落盘痕迹 | `openthree/registry.py`：`_persist_prompts`（`logs/attached-prompts/<sha8>.md`） |
| `list_functions` 分节 | `openthree/builtins.py:1272` |
| `read_prompts` 附带「附随提示词」整段 | `openthree/builtins.py:1802-1812` |
| `write_function` 定义 / `target` 未挂载时的拒绝与可写清单 | `openthree/builtins.py:630` / `:753` |
| 单函数 CLI 参数平铺 | `openthree/cli_tool.py:298` |
| 说明书渲染进 system 末尾 | `openthree/prompt.py`：`build_attached_block` |
