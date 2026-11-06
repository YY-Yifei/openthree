# 发布到 PyPI —— 完整清单

本文档记录把 openthree 发布到 [PyPI](https://pypi.org) 的全过程。
**本地该做的都做完了**（见文末「本地已验证」），剩下的是需要人肉操作的几步：
注册账号、开 2FA、拿 API token、执行上传。

---

## 0. 现状速览

| 项目 | 状态 |
| --- | --- |
| 包名 `openthree` 在 PyPI | **已占用**（本仓库所有；线上最新 `0.6.0`，见 <https://pypi.org/project/openthree/>） |
| 版本 | `0.6.0`，**已发布**（`pyproject.toml` 与 `openthree/__init__.py:__version__` 必须同步；已上传的版本不可覆盖，改动只能发新版本号） |
| 构建 | `python -m build` ✓ 产出 `dist/openthree-<ver>-py3-none-any.whl` + `.tar.gz` |
| 元数据 | `twine check` ✓ 两个 PASSED（0.6.0 产物） |
| 安全 | sdist **不含 `.openthree/`**（`MANIFEST.in` 有 `prune .openthree` 安全网，API key 不外泄）；`recursive-include docs *.md` 按**磁盘**收集而非 git，未跟踪的废案靠 `MANIFEST.in` 里显式 `exclude` 挡住 |
| 测试 | 650 passed（仓库根，0.6.0 实跑） |
| 依赖 | `PyYAML>=6.0`、`json_repair>=0.30` —— **`openai` 已移除**：LLM 传输由 `openthree/llm.py` 用标准库 `http.client` 实现，于是 `pydantic` / `pydantic-core`（Rust）/ `httpx` / `anyio` 都不再进入依赖树；频道层同样只用标准库 |

> ⚠️ `0.1.0` 一旦上传到 PyPI **不可覆盖**（只能发新版本号）。
> 所以务必先走 TestPyPI 演练一遍。

---

## 1. 一次性准备

### 1.1 注册 PyPI 账号

- 正式：<https://pypi.org/account/register/>
- 测试：<https://test.pypi.org/account/register/>（与正式账号**相互独立**，需分别注册）

### 1.2 开启 2FA

PyPI 强制要求双因素认证。用任意 TOTP App（Google Authenticator / 1Password 等）扫码。
**务必保存好 recovery codes。**

### 1.3 创建 API token（不要用账号密码上传）

- 正式：<https://pypi.org/manage/account/token/>
- 测试：<https://test.pypi.org/manage/account/token/>

首次发布时，token 作用域选 **“Entire account”**（项目还不存在，无法选具体项目）；
发布成功后，**建议重建一个只针对 `openthree` 项目的 token**，把账户级 token 删掉。

token 形如 `pypi-XXXXXXXX...` 的一长串随机字符，**只显示一次**，立刻存好。

### 1.4 配置凭据（二选一）

**方式 A —— `~/.pypirc` 文件**（Windows：`%USERPROFILE%\.pypirc`）

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-填写你的正式token

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-填写你的测试token
```

> Windows 上请把该文件权限收紧（只允许自己读）。注意 `.gitignore` 不要把它提交进去。

**方式 B —— 环境变量 / 交互输入**

```bash
# bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-xxxxxxx
```

Windows cmd：

```bat
set TWINE_USERNAME=__token__
set TWINE_PASSWORD=pypi-xxxxxxx
```

> `username` 必须写成字面量 `__token__`，password 才是 token 本身。

### 1.5 安装构建/上传工具

```bash
python -m pip install --upgrade build twine
```

本项目验证时用的是 `build 1.5.0` / `twine 7.0.0`。

---

## 2. 每次发布前：本地自检

在仓库根目录依次执行（全部应通过）：

```bash
# 1) 版本号一致性（两处必须相同）
#    pyproject.toml: [project] version
#    openthree/__init__.py: __version__
python -c "import openthree,re,pathlib;v=re.search(r'version\s*=\s*\"([^\"]+)\"',pathlib.Path('pyproject.toml').read_text()).group(1);print('pyproject',v,'| __init__',openthree.__version__);assert v==openthree.__version__, '版本号不一致!'"

# 2) 全量测试（pyproject.toml 的 addopts 已带 -q，别重复传）
python -m pytest

# 3) 清理旧产物后重建
rmdir /s /q dist 2>nul & rmdir /s /q openthree.egg-info 2>nul
python -m build

# 4) 元数据自检（应为两个 PASSED）
python -m twine check dist/*

# 5) 安全核验：sdist 里不得出现 .openthree / config.yaml / api_key
python -c "import tarfile,glob;n=tarfile.open(glob.glob('dist/*.tar.gz')[0]).getnames();b=[x for x in n if '.openthree' in x or x.endswith('config.yaml')];print('suspect:',b or 'NONE');assert not b"

# 6) 内容核验：sdist 只应收 git 跟踪的文件
#    MANIFEST.in 的 recursive-include 按**磁盘**收集，不看 git ——
#    磁盘上留着但没提交的草稿会跟着一起发布出去。这里排掉 setuptools
#    自己生成的 PKG-INFO / setup.cfg / *.egg-info。
python -c "import subprocess,glob,tarfile;t=tarfile.open(glob.glob('dist/*.tar.gz')[0]);pre=t.getnames()[0].split('/')[0]+'/';packed={m.name[len(pre):] for m in t.getmembers() if m.isfile() and m.name.startswith(pre)};gen=lambda x:x in ('PKG-INFO','setup.cfg') or x.startswith('openthree.egg-info/');tracked={p.replace(chr(92),'/') for p in subprocess.run(['git','ls-files'],capture_output=True,text=True).stdout.split()};bad=sorted(x for x in packed-tracked if not gen(x));print('not in git:',bad or 'NONE');assert not bad"
```

---

## 3. 试发布到 TestPyPI（强烈建议，尤其首版）

```bash
python -m twine upload --repository testpypi dist/*
```

然后在**干净的虚拟环境**里安装验证：

```bash
python -m venv /tmp/tp && source /tmp/tp/bin/activate     # Windows: 用对应 Scripts\activate
pip install -i https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ openthree
openthree --inspect     # 应打印工作区/工具/上下文信息，无异常
```

> `--extra-index-url` 指向正式 PyPI 是为了让 `PyYAML`/`json_repair` 等依赖能从正式源装到
> （TestPyPI 上不一定有）。

---

## 4. 正式发布到 PyPI

```bash
python -m twine upload dist/*
```

看到 `View at: https://pypi.org/project/openthree/0.1.0/` 即成功。

> **首版提示**：若用账户级 token 上传，上传成功后会收到一封 “A new release has been
> created” 邮件；建议随后把账户级 token 撤销、换成项目级 token。

### 实际发布记录

- **2026-10-14**：`openthree 0.1.0` 已正式上传 PyPI（`twine upload` 输出
  `View at: https://pypi.org/project/openthree/0.1.0/`）。
  发布前把 README 里会原样渲染到 PyPI 页面的 `<repo>` 占位符去掉了，并**重建产物**后重传。
- 之后从 PyPI 全新 venv 实装 `openthree` 成功，`openthree --inspect` → 31 工具就绪。
- **续**：`0.1.0` 的产物是「净化本机痕迹 / 停止追踪工作区」之前打的，事后解包复核确认
  它带着当时 docs/README 里的**本机路径示例**（Windows 上的项目绝对路径、WSL 的挂载盘路径）
  ——**无凭据**，但不该带。`0.1.0` 不可覆盖，处置见下一节。
- **2026-10-14**（`openthree 0.1.2`）：把 `0.1.1` 之后合入的三项修复正式发到 PyPI ——
  ① 横幅改显示智能体自己长出来的名字（`6c7b78e`）、② 模型调用重试收敛到一层 + connect
  超时分离 + 补 500 分类（`9e67e66`）、③ 「读取用户输入」不再读到文件存储格式 / 上一次的
  输入（`beab5e1`）。`0.1.1` 无凭据/本机痕迹问题，**无需 yank**。
- **2026-10-15**（`openthree 0.1.3`）：把「`-i` 是默认模式」与四处机制修复正式发到 PyPI（`52035f4`）——
  ① 不带参数、且 stdin 连着终端时直接进交互模式（等价 `-i`），stdin 非终端（管道 / CI）自动退回单次，新增 `--no-interactive` 强制退回；
  ② **P1** 纯图片输入（`:img X` + 空回车 / `--image` 不给任务）不再回落到磁盘上的旧任务；
  ③ **P2** 交互模式退出（`:q` / EOF / Ctrl-C）在 `finally` 里 `end_session()`，不再泄漏会话记忆；
  ④ **P3** `--auto` 只把「本次接力动过的」`plan.md` 未完成项算作还没做完，陈旧计划不再拖满轮数；
  ⑤ **P4** `--auto` 触到轮数上限而仍有活时退出码非 0（`auto_unfinished`），CI 判得出「没做完」。
  测试数 256 → 309。`0.1.2` 无凭据/本机痕迹问题，**无需 yank**。
  **状态：已正式上传 PyPI**（`twine upload` 输出 `View at: https://pypi.org/project/openthree/0.1.3/`）。
  发布后验证：PyPI JSON API 可见 `0.1.3` 与两个文件（whl 129173 B / sdist 208004 B，与本地一致）；
  另建全新 venv 从 `https://pypi.org/simple/` 实装 `openthree==0.1.3` → `import` 报 `0.1.3`、`python -m openthree --inspect` → 33 工具就绪 ✓
- **2026-10-20**（`openthree 0.1.4`）：把 `0.1.3` 之后合入的三项运行边界变更正式发到 PyPI ——
  ① `max_tokens` 内核默认 8192 → **32768**（无任何配置层级提供时生效；`LLMConfig` 字段默认与
  `load_config` 回落值同源，`b449f16`）；
  ② `max_rounds` 内核默认 3 → **10**（整场自动接力轮数，每轮 200 步 ≈ 最长 2000 步，`459ad12`）；
  ③ `run_shell` 超时兜底不再对已交给读线程的管道 `close()`（旧写法与读线程争同一把锁，
  在「子进程退出但孙进程仍攥着写端」的形态下会让 `timeout` 失效、函数挂死；现在放弃读取并
  如实报「输出管道未能收尾」，`4080ce9`）。
  测试数 309 → **312**。三项都只是默认值与健壮性变化，**不改变已显式配置的行为**
  （`config.yaml` / `.openthree/limits.json` / 启动参数优先级不变）。
  升级建议：`0.1.4` 无凭据/本机痕迹问题，`0.1.3` **无需 yank**。
  **状态：已正式上传 PyPI**（`twine upload` 输出 `View at: https://pypi.org/project/openthree/0.1.4/`）。
  发布后验证：PyPI JSON API 的 `releases` 含 `0.1.4`、`latest=0.1.4`；远端两个文件的 sha256 与本地
  逐字节一致（whl `2152f10b…` / sdist `193f54df…`）；另建全新 venv 从
  `https://pypi.org/simple/` 实装 `openthree==0.1.4` → `import` 报 `0.1.4`、
  `python -m openthree --inspect` → 33 工具就绪、`run_shell(timeout=2)` 2.4s 返回并报「命令超时」。
- **2026-10-20**（`openthree 0.1.5`）：把「测试自身不留垃圾」这一条修好再发 ——
  `tests/test_builtins.py` 的 `test_run_shell_survives_orphan_holding_pipe` 刻意造一个
  脱离的孙进程持有输出管道（被保护的不变量要求这个形态），但它是刻意脱离的，
  杀树够不着它，于是每跑一次测试就在机器上留下一个 `sleep(3600)` 的进程。
  现在孙进程把 pid 写进 tmp 目录、用例在 `finally` 里按 pid 结束它，并断言收尾确实
  发生（pid 没记下来或杀不掉即判失败）；睡眠时长同时降到 120s，作为收尾失败时的兜底。
  运行期代码与 `0.1.4` **完全一致**（`whl` 内四个关键模块逐字节相同），
  只有 sdist 里的这份测试变了 —— 所以 `0.1.4` **无需 yank**：装 whl 的人拿到的代码一样，
  从 `0.1.4` sdist 从源码装的人才会拿到那个会留下孤儿进程的测试。
  测试数仍为 312。
  **状态：已正式上传 PyPI**（`twine upload` 输出 `View at: https://pypi.org/project/openthree/0.1.5/`）。
  发布后验证：PyPI JSON API 的 `releases` 含 `0.1.5`、`latest=0.1.5`；远端两个文件的 sha256
  与本地逐字节一致（whl `dbc26c42…` / sdist `64425332…`）；另建全新 venv 从
  `https://pypi.org/simple/` 实装 `openthree==0.1.5` → `import` 报 `0.1.5`、
  `python -m openthree --inspect` → 33 工具就绪、`run_shell(timeout=2)` 2.4s 返回并报「命令超时」、
  包内 `DEFAULT_MAX_ROUNDS=10` / `LLMConfig.max_tokens=32768`。
- **2026-10-21**（`openthree 0.1.6`）：把「静态前缀 + 自维护段 + 处境尾消息」这层提示词重组发到 PyPI ——
  此前每一步重新组装的 system 让工具 schema 等大块常量每步都重算，跨步的前缀缓存命中被压到
  94% 左右（截断后重发同一条消息时更低）。现在请求按三层组装：静止的 system 常量、
  模型自己会改的自维护段（计划 / 记忆 / 演化史）、每步刷新的处境尾消息；
  `build_runtime_block` 保留为兼容壳。
  「改一下，下一步就生效」的七条链路（计划 / 记忆 / 系统提示词 / 任务 / 名字 / 自造工具 / 运行边界）
  已用 stub 模型实跑 ReAct 循环逐条验证，并由 `tests/test_layered_prompt_evolution.py` 钉住 ——
  防止为了命中率把会变的东西藏起来或晚一步再给模型看。测试数 312 → **324**。
  **状态：已正式上传 PyPI**（`twine upload` 输出 `View at: https://pypi.org/project/openthree/0.1.6/`）。
  发布后验证：PyPI JSON API 的 `releases` 含 `0.1.6`、`latest=0.1.6`；远端两个文件的 sha256
  与本地逐字节一致（whl `43df6733…` / sdist `8fd20e24…`）；另建全新 venv 从
  `https://pypi.org/simple/` 实装 `openthree==0.1.6` → `import` 报 `0.1.6`（`__file__` 指向
  site-packages）、`python -m openthree --inspect` → 33 工具就绪。
  上传过程两处环境坑：① 首次 `twine upload` 在 Windows 控制台因 rich 进度条撞 GBK
  （`UnicodeEncodeError: 'gbk' codec can't encode character '\u2022'`）中断，核对 PyPI JSON API
  确认 `0.1.6` 不存在 —— 文件未上传，改用 `PYTHONUTF8=1` + `--disable-progress-bar` 后成功；
  ② 本机 pip 默认源是阿里云镜像，索引同步滞后会报「找不到 0.1.6」，那是镜像而非发布失败，
  实装验证须显式 `--index-url https://pypi.org/simple/`。
- **2026-10-23**（`openthree 0.1.7`）：把「本轮输入回到 history 末尾」这层修复打进产物 ——
  此前「# 当前任务（三）」是 system 常量的一部分，而 history 之后只追加自维护段与处境段，
  于是模型读到的最后一句人话永远是**上一轮**的 user 消息（表现为 `-i` 连发多条时像在处理上一条），
  且任务正文每条消息都变、把工具 schema 的前缀缓存每轮打断。
  现在任务正文是一条 user 消息，由 `Agent._sync_current_input()` 每步维护在 history 末尾
  （幂等：没变不动，变了原地刷新同一条，已被上下文折叠丢掉则重新追加）；
  `prompt.py` 新增 `CURRENT_INPUT_TEMPLATE`，`STATIC_TEMPLATE` 不再含任务正文。
  计划段另加一句：计划跨轮存活、用户的话不跨轮，两者冲突时以当前任务为准。
  测试数 312 → **331**（新增 `tests/test_current_input_position.py` 守 5 条不变量；
  另修掉 `test_prompt_prefix_cache` 一处秒级竞态 —— 三处各自 `datetime.now()`，已钉住时钟）。
  本地验收：`twine check` 两个 PASSED；sdist 无 `.openthree/` / `config.yaml` / `api_key`；
  仓库根与解包后的 sdist 内各跑一遍 **331 passed**；whl 装到 `_live/wheelcheck` 后用中性 cwd
  探针验收（`__file__` 指向安装目录而非源码、`__version__=0.1.7`、包内含 `CURRENT_INPUT_TEMPLATE`
  与 `_sync_current_input`、内置工具 33 个）。
  **状态：已正式上传 PyPI**（`twine upload` 输出 `View at: https://pypi.org/project/openthree/0.1.7/`）。
  发布后验证：PyPI JSON API 的 `releases` 含 `0.1.7`、`latest=0.1.7`、两个文件 `yanked` 均为 `False`；
  `pip download --no-deps` 从 `https://pypi.org/simple` 拉回的 whl 与本地逐字节一致
  （133136 B / sha256 `4a67f5eb…`），sdist 同理（236974 B / `f8f9d517…`）——
  即线上产物与本地验收过的那份完全相同，未经中间层重新打包。
  本次上传一次通过：`PYTHONUTF8=1` + `--disable-progress-bar` 避开了 `0.1.6` 那次的 GBK 崩溃，
  `~/.pypirc` 凭据直接生效，无需交互。
  注：线上 sdist 内附的本文是**上传前**的快照，其「现状速览 / 本段」仍写着「待上传」——
  这是构建早于上传的必然结果；`0.1.7` 不可覆盖，故不回填，以仓库内本文为准。

### 发布上线后的「撤回 / 重发」记录

- **0.1.0 → 0.1.1**：0.1.0 的两个发行包（whl 的 `METADATA`、sdist 的 `README.md` /
  `docs/wsl-setup.md` / `scripts/wsl-setup.sh`）含本机路径示例，已在 `0.1.1` 全部改为
  通用占位（`<你的项目>`、`/mnt/...`）。0.1.0 **建议 yank**（见第 6 节），
  `pip` 未锁版本时会自动跳过它、装到 0.1.1。
- 注：yank 只是「不推荐」，**已发布文件无法删除**；真正让 `pip install openthree`
  装到干净代码的办法就是发一版新的。

---

## 5. 发布后验证

```bash
# 1) 直接从 PyPI 安装（几十秒后索引刷新）
pip install openthree

# 2) 端到端冒烟
openthree --version
openthree --inspect
```

也可在浏览器打开 <https://pypi.org/project/openthree/> 确认 README 渲染正常
（本项目 README 已无 PyPI 上会失效的仓库相对链接）。

---

## 6. 版本升级流程（后续每版）

1. 改 `pyproject.toml` 的 `version` **和** `openthree/__init__.py` 的 `__version__`（两处同步）。
2. 在仓库里记一条 CHANGELOG（若建立的话）。
3. 重复「第 2 节 本地自检」→「第 4 节 上传」。
4. **绝不覆盖已发布版本**；即使误传了内容，也只能把该版本 *yank* 掉并重发新号。

### 撤回 / Yank

PyPI 上的版本不能删除，只能 *yank*（标记为“不建议使用”，`pip` 在未锁版本时会跳过它）：

- 网页：项目 → *Manage* → *Releases* → 目标版本 → *Options* → *Yank*
- 或 `python -m twine upload` 无法撤回；只能靠网页操作。

> 若把**含密钥**的内容误传，务必尽快 yank，并立刻在 PyPI 上**删除该文件/发布**（极端情况下联系 PyPI 支持）。

---

## 7. 常见坑

| 现象 | 原因 / 处置 |
| --- | --- |
| `400 File already exists` | 版本号已用过。改版本号重新构建上传，不能覆盖。 |
| `403 Invalid or non-existent authentication` | token 写错，或用户名没写成 `__token__`。 |
| `400 The description failed to render` | README 里有 PyPI 渲染不支持的语法；跑 `twine check` 复现。 |
| TestPyPI 装不上依赖 | 加 `--extra-index-url https://pypi.org/simple/`。 |
| 刚发布却 `Could not find a version ... (from versions: none)` | 多半是 pip 走了**国内镜像**（如 `mirrors.aliyun.com`）尚未同步。用 `pip config list` 确认；临时直连官方源验证：`pip install --index-url https://pypi.org/simple/ openthree`。官方索引已收录可查 <https://pypi.org/simple/openthree/>。 |
| sdist 缺 `tests/conftest.py` 之类 | 由 `MANIFEST.in` 控制；本项目已 `recursive-include tests *.py`。 |
| 误把 `.openthree/`（含 API key）打进包 | 本项目 `MANIFEST.in` 已 `prune .openthree`；上传前仍应跑第 2 节第 5 步复核。 |
| README/描述渲染乱码 | `pyproject.toml` 里 `description` 含中文没问题，但确保文件都是 UTF-8。 |

---

## 8. 本地已验证（本仓库实测记录）

以下均在本次准备中实跑通过，可直接作为发布依据：

- `python -m build` → 成功，**无 setuptools 弃用警告**（license 已用 SPDX 表达式 + `setuptools>=77`）
- `python -m twine check dist/*` → **两个 PASSED**
- 全新 venv 装 **wheel** → `import openthree` OK，`openthree --inspect` → **33 工具**就绪
- 全新 venv 装 **sdist**（走 PEP 517 重新构建）→ 成功
- 解包 sdist 后在其内跑 `pytest` → **309 passed**
- sdist 清单 **62 项**：含 `tests/conftest.py`、`self_test.py`、`docs/`、`scripts/`；**不含任何 `.openthree/`，也扫不到 `api_key` / `sk-` 形迹**
- 依赖安装需联网（`PyYAML`/`json_repair`）；断网时会失败，与包本身无关

### 0.1.3 实跑补充（2026-10-15）

- `pyproject.toml` / `openthree/__init__.py` 版本号一致（`0.1.3`）✓
- `python -m build` → 产出 `openthree-0.1.3-py3-none-any.whl` + `.tar.gz` ✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- sdist 62 项，**无 `.openthree/`、无 `config.yaml`、无 `api_key`/`sk-` 形迹** ✓
- **全新 venv**（`_pubvenv013`）装 **wheel**：`import openthree` → `0.1.3`（`__file__` 指向 site-packages）、`python -m openthree --inspect` → 33 工具就绪 ✓
- 解包 sdist 后在其内跑 `pytest` → **309 passed** ✓
- ⚠️ **已知环境怪癖**：在 Windows 上以 `Scripts\openthree.exe`（console script 启动器）重定向 stdout 时可能零输出（`--version` / `--inspect` 均如此，**rc 仍为 0**）——**0.1.2 的 venv 同样复现**，故非本次引入；用 `python -m openthree` 一切正常。

### 0.1.5 实跑补充（2026-10-20）

- `pyproject.toml` / `openthree/__init__.py` 版本号一致（`0.1.5`）✓
- `python -m build` → `openthree-0.1.5-py3-none-any.whl`（129,003 B）+ `.tar.gz`（209,660 B）✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- **whl 与 sdist 的 `agent.py` / `llm.py` / `builtins.py` / `__init__.py` 逐字节相同** ✓
- 包内关键值：`DEFAULT_MAX_ROUNDS=10`、`LLMConfig.max_tokens=32768`（`load_config` 回落同值）、
  超时兜底无 `stream.close()`、含「输出管道未能收尾」表述 ✓
- sdist 无越界目录段 / 无 `config.yaml`、`limits.json`、`identity.json`、`.env` /
  本机绝对路径 0 命中 / 真实形态 api key 0 命中（README 里 7 处 `sk-xxx` 是占位符示例）✓
- **仓库根全量 pytest → 312 passed**；**解包 sdist 内全量 pytest → 312 passed**（核 junit 计数）✓
- sdist 内孤儿用例已是收尾版：有 `pidfile` / `_reap`、睡 120s，跑完机器上无残留进程 ✓
- **全新 venv**（`_live/venv015`）从 `https://pypi.org/simple/` 装 `openthree==0.1.5`：
  `import` → `0.1.5`（`__file__` 指向 site-packages）、`python -m openthree --inspect` → 33 工具就绪、
  `run_shell(timeout=2)` → 2.4s 返回并报「命令超时（2s），已强杀进程树」✓
- 发布后 PyPI JSON API：`latest=0.1.5`、两个文件 sha256 与本地逐字节一致 ✓

### 0.1.8 实跑补充（2026-10-27）

本版内容（四项，均可从 git 历史逐条核对）：

- **每个 function 文件同时是命令行程序**：模型造出的 `.py` 可被人和别的 agent 直跑；
  CLI 参数表与工具 schema 同源（都从签名 + docstring 推导）。命名收敛成唯一判据
  `is_registrable`（`__x__` 协议 / `_x` 私有 / `x` 公开），注册器、静态审计、
  子进程验收、命令行入口四处共用。
- **`-u` 升级模式**：让**这个工作区**追平本机已装内核（不是让 openthree 本身变新，
  那是 `pip install -U openthree`）。改动先进待应用区，人确认 `:apply` 才落盘，
  `:undo` 可撤销。`-u --check` 只扫描不改文件，有漂移退出码 1，适合 CI。
- **自省两条通道**：`whoami` 补上内核版本 / 结构 schema / 工作区完整性（与名字同属
  「我是谁」，不新增工具、提示词净增 0 字符）；`--help` 加尾注答「怎么查自己」，
  静态、任何 cwd 下逐字节相同。
- 老工作区（无 `workspace.json`）调 `whoami` 如实说「无版本戳」，**不把缺失的 schema
  报成 0** —— 0 会被读成「结构停在第 0 版」。

验收：

- `pyproject.toml` / `openthree/__init__.py` 版本号一致（`0.1.8`，`tomllib` 与
  `import` 双向核实）✓
- **仓库根全量 pytest → 402 passed / 0 failed / 0 error / 0 skipped**（核 junit 计数，77.6s）✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- sdist 76 项，**无 `.openthree/`、无 `config.yaml`、无 `api_key` / `identity.json` /
  `limits.json`**（按路径逐条筛，违规 0 项）✓
- **全新 venv**（`_pub018`）装 **wheel**，在仓库之外的中性 cwd 验收：
  `import openthree` → `0.1.8`，`__file__` 指向 site-packages ✓
  `python -m openthree --help` → 尾注「自省」三条正确打印 ✓
  `python -m openthree --version` → `openthree 0.1.8` ✓
  绑一个空工作区后 `whoami()` → `内核：0.1.8（本工作区由 0.1.8 铺出；结构 schema 1/1）`、
  `工作区：17/17 项齐备` ✓
  `python -m openthree -u --check` 在无工作区处 → **退出码 2** 且提示「先跑一次 openthree
  （或 --init）」，未凭空造出工作区 ✓

> 上传后的体积与 sha256 记在文末「上传后核对」小节：它们描述的是**已发布、不可变**的
> 产物，所以不会因为本文自身随后被改动而失效。

### 0.2.0 实跑补充（2026-10-29）

本版内容：**频道层** —— 微信（iLink Bot 长轮询）与钉钉（机器人 Stream 模式）两种渠道，
`openthree -d` 后台常驻、`-i` 与频道并存。频道消息走的就是 `-i` 里那一句
`Runtime.run()`，内核未改。详见 README「频道」一节与提交 `7c0ba7d`。

验收（仓库根实跑）：

- `pyproject.toml` / `openthree/__init__.py` 版本号一致（`0.2.0`）✓
- 全量 pytest → **542 passed / 0 failed**（新增 `tests/test_channels.py` 53 条）✓
- `python -m build` → `openthree-0.2.0-py3-none-any.whl` + `.tar.gz`；先清空 `dist/`
  与 `openthree.egg-info` 再重建，`dist/` 只留本版 ✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- wheel 内 `openthree/channels/` **12 个模块**齐备（`__init__` / `base` / `config` /
  `bridge` / `manager` / `wechat` / `dingtalk` / `cli` / `_http` / `_ws` / `_qr` /
  `_qrencode`）✓
- sdist **92 项**，按路径逐条筛**违规 0 项**（无 `.openthree/`、无 `config.yaml`、
  无 `api_key` / `identity.json` / `limits.json`）；`tests/` 与 `docs/` 照常包含 ✓
- **全新 venv** 装 **wheel**，在仓库之外的**中性 cwd** 验收：`import openthree` →
  `0.2.0` 且 `__file__` 指向 site-packages；`openthree channel --help` 列出
  `add / list / show / enable / disable / remove / test / path` 八个子命令 ✓
- **上传**：已上传 PyPI，见文末「上传后核对」；`0.1.9` 从未上传，其产物已被本版取代 ✓

### 0.3.0 实跑记录（2026-10-29）

本版内容：**`-profile` 多层 function 来源** —— 命令行可多次点名 profile 目录，
project(0) 与 profile(1..N) 按 distance 裁决，祖先目录的自动加载移除。详见
`docs/profile-functions-proposal.md` 与 README「profile」一节。

- `pyproject.toml` / `openthree/__init__.py` 版本号一致（`0.3.0`）✓
- 全量 pytest → **558 passed / 0 failed** ✓
- `python -m build` → wheel 252,984 B + sdist 462,404 B（先清空 `dist/` 再构建）✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- sdist 内容核验：与 `git ls-files` 比对，**未跟踪文件 0 项** —— 未跟踪的废案
  `docs/memory-chroma-proposal.md` 由 `MANIFEST.in` 的显式 `exclude` 挡住 ✓
- `twine upload --non-interactive` 一次成功 →
  <https://pypi.org/project/openthree/0.3.0/> ✓
- 双向核验：下表 0.3.0 两行与本地 `dist/` **全串一致**；`/pypi/openthree/json` 的
  `info.version` 从 `0.2.0` 翻到 `0.3.0`（CDN 缓存约需数十秒）✓

### 0.4.0 实跑记录（2026-11-02）

本版内容：**function 文件可附随系统提示词** —— 一个 function 文件除了公开函数，
还可以用 `__system_prompts__`（`str` / `list[str]` / 零参可调用三种形态）附随一段说明；
它在 `reload` 时求值一次，拼进 system 的**最后一段**。**只在写了这个符号时生效**：
不写它，system 与 0.3.0 逐字节相同（没有开关、没有配置项，代码路径根本不进）。
详见 `docs/function-system-prompts-proposal.md` 与 `docs/function-system-prompts-self-review.md`。

- `pyproject.toml` / `openthree/__init__.py` 版本号一致（`0.4.0`）✓
- 全量 pytest → **582 passed / 0 failed**（新增 `tests/test_attached_prompts.py` 13 条、
  `tests/test_prompt_only_file.py` 5 条）✓
- `python -m build` → wheel 262,992 B + sdist 501,009 B（先清空 `dist/` 与 egg-info）✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- sdist 内容核验：93 个文件，与 `git ls-files` 比对**未跟踪 0 项**；废案
  `docs/memory-chroma-proposal.md` 未混入（由 `MANIFEST.in` 显式 `exclude` 挡住）✓
- sdist 安全核验：**无 `.openthree/`、无 `config.yaml`、无 `api_key` / `identity.json` /
  `limits.json`**（按路径逐条筛，违规 0 项）✓
- `twine upload --non-interactive` 一次成功 →
  <https://pypi.org/project/openthree/0.4.0/> ✓
- 双向核验：下表 0.4.0 两行与本地 `dist/` **全串一致**；`/pypi/openthree/json` 的
  `info.version` 从 `0.3.0` 翻到 `0.4.0`，两个文件 `yanked` 均为 `False` ✓

**线上产物端到端探针**（全新 venv `_live/venv040`，从中性 cwd 跑，`__file__` 指向
site-packages 而非源码）：写一个只含 `__system_prompts__`、没有任何公开函数的 `kb.py` 后，
`Registry.reload()` 收集到 1 条附随提示词，system 增量 274 字符且原文出现在末尾
（此前缀逐字节不变）；`logs/attached-prompts/<sha8>.md` 的文件名等于内容 sha256 的前 8 位、
内容可原样读回；再 reload 不新增文件；删掉该文件后 system **逐字节**退回基线；
该文件**没有**被注册成工具 ✓

**本次实跑踩到的三处环境坑**（供下次发布绕开）：

1. `set PYTHONUTF8=1 && …` 在 cmd 里会把 `&&` 前的空格算进值，Python 直接
   `invalid PYTHONUTF8 environment variable value` —— 必须写成 `set "PYTHONUTF8=1"`。
2. 在仓库根用 `python -c "import openthree"` 检查 venv 是**假的**：`sys.path` 含当前目录，
   会 import 到源码、报出源码的版本与路径。核验安装版必须换到中性 cwd，或直接断言
   `openthree.__file__` 里有 `site-packages`。
3. 从 pypi.org 直装依赖在本机 600s 超时（openai / pydantic / httpx2 等一长串）。
   本版改为**只从 PyPI 取 openthree 本体**（下载件 sha256 与本地逐字节一致，已核），
   依赖走镜像 —— 「线上产物可用」这条结论仍由 openthree 本体本身承担。

> 线上 sdist 内附的本文是**上传前**的快照，其「现状速览」与本段仍写着「待上传」——
> 构建早于上传的必然结果，`0.4.0` 不可覆盖，故不回填，以仓库内本文为准。


### 0.5.0 实跑记录（2026-11-04）

本版内容：**LLM 传输层自实现，摘掉 `openai` SDK** —— 依赖树里的
`openai → pydantic → pydantic-core` 整条移除（`pydantic-core` 是 Rust 扩展，
没有 iOS / a-Shell 可用的轮子，而 iOS 上没有可用的 Rust 编译链），
主依赖只剩 `PyYAML` 与 `json_repair`。

- `openthree/llm.py` 新增 `post_chat()`：用标准库 `http.client` 发一次
  `/chat/completions`。选它而不是 `urllib` 是为了保住**超时分离** ——
  先按 `connect_timeout`（5s）建连，连上后把 socket 超时换成 `timeout`（600s），
  于是「主机连不上」秒失败、「模型想得久」仍能等满。
- 新增 `TransportError(status=…)`：非 2xx 带状态码与正文；`_error_brief()`
  从 `error.message` 取出 provider 的原话（只看状态码无法排查）。
- `_retryable()` 改为**按状态码裁决**：408/409/429/5xx 与 `status=0`
  （DNS/连接/超时，根本没走到 HTTP）重试；其它 4xx 立刻返回 ——
  重试一个鉴权失败只是把同一个错误再发一遍。
- **代理**（`_proxy_for()`）：旧实现底层是 httpx，`trust_env=True`，会读
  `HTTP_PROXY` / `HTTPS_PROXY` / `no_proxy`；`http.client` 完全不读。不补这一步，
  企业网 / 代理环境会从「能连」变成「连不上」而看不出原因。现在 HTTPS 目标走
  CONNECT 隧道、HTTP 目标走绝对 URI，`no_proxy` 照旧生效，且代理下超时分离同样成立。
- 删掉 `_ensure_client()`、`model_dump()` 分支与 `max_retries` 字段。最后那个只为
  钉住 SDK 自身的重试层级而存在；没有 SDK 就没有第二层，留着只是多一条要维护的路径。
- 测试数 582 → **593**：新增真本地 HTTP 服务端端到端往返；401 不重试 / 429 重试；
  400 正文带 "timeout" 字样也不重试；代理解析、`no_proxy`、CONNECT 隧道的白盒断言。
  设计取舍与自评审见 `docs/llm-transport-self-review.md`。

验收（仓库根实跑）：

- `pyproject.toml` / `openthree/__init__.py:__version__` / `import openthree` 三处均为 `0.5.0` ✓
- 全量 pytest → **593 passed / 0 failed**（86.78s）✓
- `python -m build` → `openthree-0.5.0-py3-none-any.whl`（266,134 B）+
  `.tar.gz`（514,019 B）（先清空 `dist/` 与 `openthree.egg-info`）✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- sdist 94 项：**无** `.openthree/`、`config.yaml`、`api_key`、`identity.json`、
  `limits.json`、`memory.jsonl`；与 `git ls-files` 比对**未跟踪 0 项** ✓
- sdist 的 `PKG-INFO` 里 `Requires-Dist` 只剩 `PyYAML>=6.0`、`json_repair>=0.30`
  与 dev extra 的 `pytest>=7`，**无 `openai` / `pydantic` / `httpx`** ✓
- 干净 venv（只装 `PyYAML` + `json_repair` + openthree）里用真实 `LLM.chat()`
  打到本地 mock 端点：tool_calls / `reasoning_content` 回传 / usage / `Bearer` 头
  全部正确，且 `sys.modules` 中**无** `openai` / `httpx` / `pydantic` ✓

- **上传**：`twine upload --non-interactive --disable-progress-bar` 一次成功 →
  <https://pypi.org/project/openthree/0.5.0/> ✓
- **双向核验**：`/pypi/openthree/0.5.0/json` 的 `info.version` 为 `0.5.0`；两个文件的
  `size` 与 `digests.sha256` 与本地 `dist/` **全串一致**，`yanked` 均为 `False` ✓

**升级提示**：本版不改变对外行为与配置项 —— 只把 LLM 传输从 SDK 换成标准库。
唯一可见差异是失去 httpx 的连接复用，每次 `chat()` 多一次 TCP(+TLS) 握手
（本机 ~10ms，移动网络 ~100–300ms），相对模型生成时间可忽略。

### 0.6.0 实跑记录（2026-11-06）

本版内容：**内置工具 `decide`（可选）** —— 把「拿不准的判定」交给一个独立的决策模型
（JEV），取回一个分布（`choice` 多选一 / `noul` 0–1 连续值 / `score` 各等级期望值）。
内核只搬运、只如实转达：不设阈值、不四舍五入、不解释概率、不转发 `usage`。

- **默认不存在**：`JEV_BASE_URL` / `JEV_API_KEY` / `JEV_MODEL` 三个环境变量齐全且
  base 是 http(s) 绝对地址才注册。判据只有一处 —— `builtins.installed_builtins()`
  = 常驻集 `BUILTIN_FUNCTIONS` + 条件集 `decide.optional_builtins()`，
  `Runtime.__init__` 与 `cli_tool.setup_offline_kernel` 两个注册期调用点都走它。
  未配置时它不在工具清单、不在请求的 `tools` 数组、system 里**零字节**，也没有
  「为什么不在」的诊断工具 —— 没注册就没痕迹。
- **配置只从环境变量读**，不读 `config.yaml`、没有默认端点、没有默认模型：
  默认端点等于替你决定数据发给谁。改环境变量要重启进程（注册期判定一次，
  会话中途增减工具会让 system 前缀每步都变）。
- **传输层复用**：`llm.post_json()` / `llm.post_chat()` 拆出「路径从哪来」这一层参数，
  代理 / TLS / 超时分离 / 错误摘要 / 重试判据（`_retryable` / `_backoff`）仍是**一份**。
  在 `openthree/decide.py` 里复制一遍这些逻辑，401 会被重试、429 会被吞，而两处都自称对。
- **安全边界写进 README**：配好 `JEV_*` 之后 `decide` 会把 `state` 与 `questions`
  发往 `JEV_BASE_URL` 指向的第三方端点 —— 那不在 openthree 的安全边界内。
- **内核种子未动**：`openthree/workspace.py` 一个字节没改，`decide` 进模型视野靠的是
  `Registry.digest()` 从注册表长出，不是往提示词里写句子。
- 测试数 593 → **650**（新增 `tests/test_decide.py` 57 条，全部不打真网：
  假 JEV 端点走 `http.server`，门控/归一/错误分型/零痕迹逐条钉住）。
  设计取舍与自评审见 `docs/decide-tool-proposal.md` 与
  `docs/decide-tool-proposal-self-review.md`。

验收（仓库根实跑）：

- `pyproject.toml` / `openthree/__init__.py:__version__` / `import openthree` 三处均为 `0.6.0` ✓
- 全量 pytest → **650 passed / 0 failed**（67.09s）✓
- `python -m build` → `openthree-0.6.0-py3-none-any.whl` + `.tar.gz`
  （先清空 `dist/` 与 `openthree.egg-info`；体积与 sha256 见文末「上传后核对」表）✓
- `python -m twine check dist/*` → **两个 PASSED** ✓
- sdist 无 `.openthree/`、`config.yaml`、`api_key`、`identity.json`、`limits.json`、
  `memory.jsonl`；与 `git ls-files` 比对未跟踪 0 项（`docs/memory-chroma-proposal.md`
  仍由 `MANIFEST.in` 的显式 `exclude` 挡住，不进包）✓
- 门控探针（`.openthree/tmp/probe_review_pack.py`）：未配置 → 37 工具，
  `digest()` 与 `schemas()` 都无 `decide`；配置三个变量后 → 38 工具，两处都有 ✓

**升级提示**：本版对已装用户**行为不变** —— 不配 `JEV_*` 时工具集与 `0.5.0`
完全一致（零字节、零痕迹）。要用 `decide` 才有新增网络出口，README「安全边界」
已列明它发往哪里。

- **上传**：`twine upload --non-interactive --disable-progress-bar` 一次成功 →
  <https://pypi.org/project/openthree/0.6.0/> ✓
- **双向核验**：`/pypi/openthree/0.6.0/json` 的 `info.version` 为 `0.6.0`；两个文件的
  `size` 与 `digests.sha256` 与本地 `dist/` **全串一致**，`yanked` 均为 `False` ✓
- **安装版验收**：干净 venv（`_live/venv060`）从 `https://pypi.org/simple/` 装 `0.6.0`，
  在仓库之外的**中性 cwd** 跑：`import openthree` → `0.6.0` 且 `__file__` 指向
  site-packages、`python -m openthree --version` → `openthree 0.6.0`、`--inspect` →
  37 工具；同一探针配好三个 `JEV_*` 再跑 → **38 工具且 `decide` 已注册**，
  不配则 37 且没有 `decide` ✓


### 上传后核对（按版本）

sha256 与体积取自 PyPI JSON API（`https://pypi.org/pypi/openthree/<ver>/json` 的
`urls[].digests.sha256`），与本地 `dist/` 逐串比对。它们描述的是**已发布、不可变**的
产物，所以本文随后被改动也不会让这些数字失效。

| 版本 | 产物 | 字节 | sha256 |
| --- | --- | --- | --- |
| 0.1.8 | `openthree-0.1.8-py3-none-any.whl` | 166,856 | `bb6662c6d752e5f559ac536c214be17ba637592421a73b9ad436f97ea0aadfbd` |
| 0.1.8 | `openthree-0.1.8.tar.gz` | 312,618 | `a8d9e169484d0f4967b5ba047b236be11bd5fe4d9400eccb58fc00ae861f3823` |
| 0.2.0 | `openthree-0.2.0-py3-none-any.whl` | 247,643 | `17e1b65f2dbfdbb9ea59fa358334e3482805a349e72c68a319c3066f7b8e077a` |
| 0.2.0 | `openthree-0.2.0.tar.gz` | 416,365 | `722aaa783ab159252e62d806fe4a79cd197941226f89c27f185d44eb607b29d8` |
| 0.3.0 | `openthree-0.3.0-py3-none-any.whl` | 252,984 | `2687e36edc765364df1188c8b3447b9f0088383243b8eb0185dc1705e4072510` |
| 0.3.0 | `openthree-0.3.0.tar.gz` | 462,404 | `f5f108538c73dcec44ab99467126c06c03566e7b50bd89047e16e201ce4e504c` |
| 0.4.0 | `openthree-0.4.0-py3-none-any.whl` | 262,992 | `eeb0d778d337e561c3e48d3ce6a956f18007bc4904c9c987a0242c35b19d67cb` |
| 0.4.0 | `openthree-0.4.0.tar.gz` | 501,009 | `73509dbec6bdbc67200ab2ff15f9327d12ffc63c13d0bcb7a6da469fd7d53401` |
| 0.5.0 | `openthree-0.5.0-py3-none-any.whl` | 266,134 | `bd8a99e94306ec9febee70f3e4b64f39a11c42e65181995c1f28c75bc0ed639f` |
| 0.5.0 | `openthree-0.5.0.tar.gz` | 514,019 | `0988c46573410d1cd6c1884bc9dfbdeac57413f33059043dcd564b3849c27db3` |
| 0.6.0 | `openthree-0.6.0-py3-none-any.whl` | 276,790 | `7cf8c045a6c235a3993572777cafd3159305a97c4fb5b7c4d03ba8310ab73df4` |
| 0.6.0 | `openthree-0.6.0.tar.gz` | 565,114 | `cb4a1b6c6f66943097bb0ae77e729ed925605ee14ebe348f1434bcbb1097df24` |




