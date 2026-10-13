"""注册器：扫描 `.openthree/functions/*.py`，把里面的函数变成模型的工具。

这是「二」的落地，也是自我演化的关键：
模型写出新文件 → reload() → 新工具立刻可用。

约定（极简）：
    * 模块里**所有**公开函数都注册，除非带 `_` 前缀。
    * 函数首个参数若命名为 `kernel`（或注解为 Kernel），由运行期注入，模型不可见。
    * 同名工具后者覆盖前者，覆盖情况记入 load_errors 供自省。
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .tool import Tool, build_tool
from .workspace import Workspace

_MODULE_PREFIX = "openthree_user_functions"


@dataclass
class LoadError:
    file: str
    error: str
    traceback: str = ""


@dataclass
class Registry:
    """工具名 → Tool。"""

    workspace: Workspace
    tools: Dict[str, Tool] = field(default_factory=dict)
    load_errors: List[LoadError] = field(default_factory=list)
    loaded_files: List[str] = field(default_factory=list)
    builtins: Dict[str, Tool] = field(default_factory=dict)

    # ---------- 内置 ----------
    def register_builtin(self, name: str, func: Callable[..., Any]) -> Tool:
        t = build_tool(func, origin="builtin")
        t.name = name
        self.builtins[name] = t
        self.tools[name] = t
        return t

    def freeze_builtins(self) -> None:
        """记录内置工具，便于后续区分「模型自造」的工具。"""
        self.builtins = {k: v for k, v in self.tools.items() if v.origin == "builtin"}

    # ---------- 加载 ----------
    def reload(self) -> "Registry":
        """重新扫描并加载全部用户 function。内置工具始终保留。"""
        # 先清缓存，再加载：保证 reload 时执行的一定是新代码，
        # 也避免用户函数 import 到上一次的陈旧辅助模块。
        self._purge_modules()
        self.tools = dict(self.builtins)
        self.load_errors = []
        self.loaded_files = []

        funcs_dir = str(self.workspace.functions_dir)
        added_to_path = False
        if funcs_dir not in sys.path:
            sys.path.insert(0, funcs_dir)   # 让用户函数能 import 同目录的辅助模块
            added_to_path = True

        try:
            for path in self.workspace.list_function_files():
                self._load_file(path)
        finally:
            if added_to_path:
                try:
                    sys.path.remove(funcs_dir)
                except ValueError:
                    pass
        return self

    def _purge_modules(self) -> None:
        """清掉本目录加载过的模块（含用户自建的辅助模块），避免陈旧代码。"""
        for mod_name in list(sys.modules):
            if mod_name.startswith(_MODULE_PREFIX):
                sys.modules.pop(mod_name, None)

    def _module_name_for(self, path: Path) -> str:
        stem = path.stem
        return f"{_MODULE_PREFIX}.{stem}"

    def _load_file(self, path: Path) -> None:
        mod_name = self._module_name_for(path)
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法为 {path} 创建模块 spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            self.load_errors.append(
                LoadError(file=str(path), error=f"{type(exc).__name__}: {exc}",
                          traceback=traceback.format_exc())
            )
            sys.modules.pop(mod_name, None)
            return

        count = 0
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if not callable(obj):
                continue
            if isinstance(obj, type):
                continue
            if getattr(obj, "__module__", None) != mod_name:
                continue  # 只收本文件定义的函数，排除 import 进来的
            try:
                t = build_tool(obj, origin="user", source_file=str(path))
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(
                    LoadError(file=str(path), error=f"签名解析失败 {attr_name}: {exc}")
                )
                continue
            prev = self.tools.get(t.name)
            if prev is not None:
                # 同名后者覆盖前者：记录下来供自省（否则模型会以为新工具没有生效）
                self.load_errors.append(LoadError(
                    file=str(path),
                    error=(f"工具名冲突：{t.name} 覆盖了已有的"
                           f"{prev.origin} 工具（{Path(prev.source_file).name if prev.source_file else '内置'}）"),
                ))
            self.tools[t.name] = t
            count += 1

        self.loaded_files.append(str(path))
        if count == 0:
            self.load_errors.append(
                LoadError(file=str(path), error="文件中没有可注册的公开函数")
            )

    # ---------- 查询 ----------
    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def names(self) -> List[str]:
        return sorted(self.tools.keys())

    def user_tools(self) -> List[Tool]:
        return [t for t in self.tools.values() if t.origin == "user"]

    def digest(self) -> str:
        """给提示词用的工具清单。"""
        lines = []
        for t in sorted(self.tools.values(), key=lambda x: x.name):
            lines.append(f"- `{t.signature_hint()}` — {_first(t.description)}")
        return "\n".join(lines) if lines else "（当前没有任何 function，请用 write_function 造一个）"

    def __len__(self) -> int:
        return len(self.tools)


def _first(text: str) -> str:
    text = (text or "").strip().splitlines()
    return text[0] if text else ""
