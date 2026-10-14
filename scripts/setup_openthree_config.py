#!/usr/bin/env python3
"""把一个本地 LLM 配置（内网网关模型）搬成 openthree 的用户级配置。

幂等：可重复执行；已存在时先备份再覆盖（内容一致则跳过）。
用法： python3 setup_openthree_config.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("缺少 pyyaml：请用带 pyyaml 的解释器运行（如 ~/venvs/openthree/bin/python）")

LOCAL_LLM = Path.home() / ".local-llm" / "config.yaml"
TARGET = Path.home() / ".openthree" / "config.yaml"


def main() -> int:
    if not LOCAL_LLM.exists():
        sys.exit(f"找不到 {LOCAL_LLM}")
    src = (yaml.safe_load(LOCAL_LLM.read_text(encoding="utf-8")) or {}).get("llm") or {}
    if not isinstance(src, dict):
        sys.exit("local-llm 配置的 llm 节不是字典")
    api_key = src.get("api_key", "")
    base_url = src.get("base_url", "")
    model = src.get("model", "")
    extra = src.get("extra") or {}
    max_tokens = src.get("max_tokens")
    if not (api_key and base_url and model):
        sys.exit(f"local-llm 配置的 llm 节缺字段：api_key/base_url/model = "
                 f"{bool(api_key)}/{bool(base_url)}/{bool(model)}")

    llm: dict = {"api_key": api_key, "base_url": base_url, "model": model}
    if max_tokens:
        llm["max_tokens"] = max_tokens
    if isinstance(extra, dict) and extra:
        llm["extra"] = extra
    doc = {
        "llm": llm,
        # 内网模型的上下文窗口按默认对待；如需改，写 context_window_tokens: 128k
    }
    text = ("# openthree 用户级配置（由 ~/.local-llm/config.yaml 的 llm 节生成）\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists() and TARGET.read_text(encoding="utf-8") == text:
        print(f"已是目标内容，跳过：{TARGET}")
        return 0
    if TARGET.exists():
        bak = TARGET.with_suffix(".yaml.bak")
        shutil.copy2(TARGET, bak)
        print(f"已备份旧配置 → {bak}")
    TARGET.write_text(text, encoding="utf-8")
    print(f"已写入 {TARGET}：model={model} base_url={base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
