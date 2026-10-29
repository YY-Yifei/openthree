"""Tool：把一个 python 函数变成模型能调用的工具。

不需要 schema、不需要装饰器参数、不需要注册代码。
类型注解 = 参数类型，docstring = 描述，函数名 = 工具名。

    @tool
    def read_file(path: str, encoding: str = "utf-8") -> str:
        \"\"\"读取文本文件。

        Args:
            path: 文件绝对路径
            encoding: 编码，默认 utf-8
        \"\"\"
        return open(path, encoding=encoding).read()
"""
from __future__ import annotations

import inspect
import re
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _json_type(annotation: Any) -> Dict[str, Any]:
    """把 python 类型注解翻译成 JSON Schema 片段。"""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "string"}

    origin = typing.get_origin(annotation)

    # Optional[X] / Union[X, None]
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return {}
    if origin in (list, List):
        args = typing.get_args(annotation)
        item = _json_type(args[0]) if args else {}
        return {"type": "array", "items": item} if item else {"type": "array"}
    if origin in (dict, Dict):
        return {"type": "object"}
    if origin is typing.Literal:
        vals = list(typing.get_args(annotation))
        if vals and all(isinstance(v, str) for v in vals):
            return {"type": "string", "enum": vals}
        return {}

    if annotation in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[annotation]}
    if isinstance(annotation, type):
        return {"type": "string"}
    return {"type": "string"}


@dataclass
class Tool:
    """一个被注册的工具。"""

    name: str
    description: str
    func: Callable[..., Any]
    parameters: Dict[str, Any] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)
    source_file: Optional[str] = None
    origin: str = "user"          # user / builtin
    needs_kernel: bool = False
    # 来源标识：project / `profile:<目录>` / user / builtin。
    # 它来自 `workspace.FunctionSource.label`，是给人看的一行说明，**不是**判据 ——
    # 判据是下面的 distance，别拿它的字符串做相等比较。
    source_kind: str = "project"
    source_distance: int = 0      # 同名裁决用的距离（越小越近，见 workspace.FunctionSource）

    def schema(self) -> Dict[str, Any]:
        """OpenAI function-calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }

    def signature_hint(self) -> str:
        """给提示词用的一行签名。"""
        parts = []
        for pname, pschema in self.parameters.items():
            t = pschema.get("type", "any")
            mark = "" if pname in self.required else "="
            parts.append(f"{pname}: {t}{mark}")
        return f"{self.name}({', '.join(parts)})"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tool {self.name} args={list(self.parameters)}>"


def _parse_args_section(doc: Optional[str]) -> Dict[str, str]:
    """解析 docstring 的 Args 段落 → {参数名: 说明}。"""
    out: Dict[str, str] = {}
    if not doc:
        return out
    lines = doc.strip().splitlines()
    in_args = False
    current = None
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(Args|Arguments|参数)\s*:\s*$", stripped)
        if m:
            in_args = True
            continue
        if in_args:
            if re.match(r"^(Returns?|Yields?|Raises|Examples?|Notes?|返回|示例)\s*:", stripped):
                break
            if not stripped:
                continue
            m2 = re.match(r"^(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$", stripped)
            if m2:
                current = m2.group(1).lstrip("*")
                out[current] = m2.group(2).strip()
            elif current and stripped:
                out[current] = (out[current] + " " + stripped).strip()
    return out


def build_tool(func: Callable[..., Any], *, origin: str = "user",
               source_file: Optional[str] = None) -> Tool:
    """由函数构造 Tool。"""
    sig = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = {}

    doc = inspect.getdoc(func)
    long_doc = doc or ""
    arg_docs = _parse_args_section(doc)

    parameters: Dict[str, Any] = {}
    required: List[str] = []
    needs_kernel = False

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        ann = hints.get(pname, param.annotation)
        if pname == "kernel" or (isinstance(ann, str) and ann == "Kernel") or ann.__class__.__name__ == "Kernel":
            # kernel 由运行期注入，不暴露给模型
            needs_kernel = True
            continue
        schema = _json_type(ann)
        if pname in arg_docs:
            schema["description"] = arg_docs[pname]
        if param.default is not inspect.Parameter.empty:
            if param.default is not None and "enum" not in schema:
                schema.setdefault("default", param.default)
        else:
            required.append(pname)
        parameters[pname] = schema

    # 描述里带上参数说明，模型看得更明白
    extra = [f"- {k}: {v}" for k, v in arg_docs.items() if k in parameters]
    if extra:
        long_doc = long_doc.rstrip() + "\n\n参数：\n" + "\n".join(extra)

    return Tool(
        name=func.__name__,
        description=long_doc.strip(),
        func=func,
        parameters=parameters,
        required=required,
        source_file=source_file,
        origin=origin,
        needs_kernel=needs_kernel,
    )


def tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """装饰器：给函数打个记号。注册由加载器统一完成。"""
    setattr(func, "__openthree_tool__", True)
    return func


_DUNDER = re.compile(r"^__.+__$")


def is_registrable(module_name: Optional[str], attr: str, obj: Any) -> bool:
    """一个模块属性是否该成为「模型的工具」。全仓库唯一判据。

    三档命名空间：

    1. ``__x__``  dunder —— 协议 / 入口（如 ``__main__``）。永不注册。
    2. ``_x``     单下划线 —— 私有辅助。永不注册。
    3. ``x``      公开 —— 作为候选，再按「必须是本模块定义的函数」过滤。

    注册器（registry）、写函数时的静态审计（builtins._static_audit）、
    子进程验收（_VERIFY_SCRIPT）与命令行入口（cli_tool）都走这一份判据，
    否则「模型看到的工具」与「人/审计看到的函数」会慢慢发散。

    Args:
        module_name: 目标模块的 ``__name__``，用于排除 import 进来的函数
        attr: 模块属性名
        obj: 该属性取到的对象

    Returns:
        该属性是否应被注册为工具
    """
    if _DUNDER.match(attr) or attr.startswith("_"):
        return False
    if not callable(obj) or isinstance(obj, type):
        return False
    return getattr(obj, "__module__", None) == module_name
