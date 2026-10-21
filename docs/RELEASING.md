# 发布到 PyPI —— 完整清单

本文档记录把 openthree 发布到 [PyPI](https://pypi.org) 的全过程。
**本地该做的都做完了**（见文末「本地已验证」），剩下的是需要人肉操作的几步：
注册账号、开 2FA、拿 API token、执行上传。

---

## 0. 现状速览

| 项目 | 状态 |
| --- | --- |
| 包名 `openthree` 在 PyPI | **已占用**（本仓库所有；最新 `0.1.6`，见 <https://pypi.org/project/openthree/>） |
| 版本 | `0.1.6`（`pyproject.toml` 与 `openthree/__init__.py:__version__` 必须同步） |
| 构建 | `python -m build` ✓ 产出 `dist/openthree-<ver>-py3-none-any.whl` + `.tar.gz` |
| 元数据 | `twine check` ✓ PASSED |
| 安全 | sdist **不含 `.openthree/`**（`MANIFEST.in` 有 `prune .openthree` 安全网，API key 不外泄） |
| 测试 | 312 passed（仓库根 + 解包后的 sdist 内各跑一遍都全绿） |
| 依赖 | `openai>=1.0`、`PyYAML>=6.0`、`json_repair>=0.30` |

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

# 2) 全量测试
python -m pytest -q

# 3) 清理旧产物后重建
rmdir /s /q dist 2>nul & rmdir /s /q openthree.egg-info 2>nul
python -m build

# 4) 元数据自检（应为两个 PASSED）
python -m twine check dist/*

# 5) 安全核验：sdist 里不得出现 .openthree / config.yaml / api_key
python -c "import tarfile,glob;n=tarfile.open(glob.glob('dist/*.tar.gz')[0]).getnames();b=[x for x in n if '.openthree' in x or x.endswith('config.yaml')];print('suspect:',b or 'NONE');assert not b"
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

> `--extra-index-url` 指向正式 PyPI 是为了让 `openai`/`PyYAML` 等依赖能从正式源装到
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
- 依赖安装需联网（`openai`/`PyYAML`/`json_repair`）；断网时会失败，与包本身无关

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

