#!/usr/bin/env bash
# 在 WSL 里把 openthree 装好、并让它随处可用（幂等，可重复执行）。
#
# 前提：已经 clone 了仓库，并在仓库根目录执行本脚本：
#     git clone file:///path/to/openthree ~/openthree
#     cd ~/openthree && bash scripts/wsl-setup.sh
#
# 它做三件事：
#   1) 建独立 venv（~/venvs/openthree）并 editable 安装 —— git pull 后无需重装；
#   2) 在 ~/.local/bin 放一个 openthree 软链（该目录已在 ~/.profile / ~/.bashrc 的 PATH 里）；
#   3) 从 ~/.local-llm/config.yaml 生成用户级 ~/.openthree/config.yaml（幂等、先备份）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/venvs/openthree"

echo "== 仓库：$REPO"

# 1) venv + editable 安装
if command -v uv >/dev/null 2>&1; then
  [ -d "$VENV" ] || uv venv "$VENV"
  uv pip install --python "$VENV/bin/python" -e "$REPO"
else
  echo "未找到 uv，回退到 python -m venv + pip"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install -U pip >/dev/null
  "$VENV/bin/python" -m pip install -e "$REPO"
fi

# 2) 命令随处可用
mkdir -p "$HOME/.local/bin"
ln -sfn "$VENV/bin/openthree" "$HOME/.local/bin/openthree"
echo "== 软链：$HOME/.local/bin/openthree -> $VENV/bin/openthree"

# 3) 用户级模型配置（由 local-llm 生成内网网关配置）
"$VENV/bin/python" "$REPO/scripts/setup_openthree_config.py"

echo
echo "== 完成。验证："
bash -lc 'which openthree && openthree -V'
echo
echo "提示：请在 WSL 自己的目录（~/...）里运行 openthree，不要在 /mnt/<挂载盘> 下运行"
echo "     （cwd 会遮蔽 editable 安装的包导入）。"
