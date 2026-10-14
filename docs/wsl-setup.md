# 在 WSL 里部署 / 使用 openthree

> 场景：仓库在 Windows 侧（或任意本地 checkout），希望在 WSL（Ubuntu）里也能用 openthree，
> 并且**以 git 形式跟随更新**（`git pull` 即得新代码）。

## 一次性部署

```bash
# 1) 克隆仓库（也可用 https/ssh 远程地址；本地 checkout 用 file:// 亦可）
git clone file:///path/to/openthree ~/openthree

# 2) 独立 venv + editable 安装（editable ⇒ git pull 后无需重装）
uv venv ~/venvs/openthree
uv pip install --python ~/venvs/openthree/bin/python -e ~/openthree

# 3) openthree 命令随处可用（~/.local/bin 已在 ~/.profile / ~/.bashrc 的 PATH 里）
ln -sfn ~/venvs/openthree/bin/openthree ~/.local/bin/openthree
```

模型配置：openthree 读三层配置（`环境变量 < 用户级 < 项目级`，见 README「配置」）。
把 `api_key` / `base_url` / `model` 写进 `~/.openthree/config.yaml` 即可。
若你另有生成用户级配置的习惯（例如从别的工具搬运凭据），可用
`scripts/setup_openthree_config.py`（见文末）。

## 日常使用

```bash
cd 任意目录
openthree "你的任务"            # 单次任务
openthree -i                    # 交互会话（多轮）
openthree --auto 5 "长期任务"    # 最多 5 轮自动接力
openthree --inspect             # 只看工作区/工具/预算，不调模型
```

## 更新

```bash
cd ~/openthree && git pull
```

## 坑（都是踩过的）

- **不要在 Windows 挂载盘（WSL 下的 `/mnt/...`）目录里运行 openthree**。那里 cwd 会被
  Python 当成包路径，遮蔽 editable 安装，`import openthree` 会解析到挂载盘里的
  `.../openthree/__init__.py`。在 WSL 自己的目录（`~/...`）里跑才正常。
- 写进挂载盘的脚本若带 CRLF，`bash script.sh` 会报 `syntax error near \r`；
  用 `sed -i 's/\r$//' script.sh` 处理，或直接用 `bash -c`。
- 模型配置分三层，后者覆盖前者：环境变量 < `~/.openthree/config.yaml` < `<项目>/.openthree/config.yaml`。

## 附：`scripts/setup_openthree_config.py`

从 `~/.local-llm/config.yaml` 的 `llm` 节取 `api_key` / `base_url` / `model` /
`max_tokens` / `extra`，写成 openthree 的用户级 `~/.openthree/config.yaml`。
幂等：内容一致则跳过，不一致则先备份为 `config.yaml.bak` 再覆盖。
