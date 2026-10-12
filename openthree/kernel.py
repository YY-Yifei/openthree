"""内核：运行期的全局把手。

用户写的 function 通过 `from openthree import KERNEL` 拿到运行中的
registry / workspace / recorder，从而具备「自我演化」与「自省」的能力。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class Kernel:
    """一个进程内唯一的运行期上下文。"""

    def __init__(self) -> None:
        self.workspace: Any = None      # Workspace
        self.registry: Any = None       # Registry
        self.recorder: Any = None       # Recorder
        self.agent: Any = None          # Agent
        self.config: Dict[str, Any] = {}
        self.state: Dict[str, Any] = {}  # 给用户 function 自由使用的草稿纸

    @property
    def ready(self) -> bool:
        return self.registry is not None and self.workspace is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Kernel ready={self.ready} tools={len(self.registry.tools) if self.registry else 0}>"


KERNEL = Kernel()
