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
