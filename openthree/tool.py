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

**这里是工具层的唯一造具入口**（内置 / 自造 / 登记 / 命令行四条路径都走
`build_tool`）。它的产物会 `json.dumps` 后随请求发给模型 API，所以两条不变量
必须守住，漏一条都会在很远的地方炸：

1. 注解要翻对。翻错不会报错，只会让模型传错类型，直到 Python 里才 TypeError。
2. schema 必须可序列化。默认值里一个 `Path(".")` 就够让**整个请求**失败。
"""
from __future__ import annotations

import collections.abc
import enum
import inspect
import math
import os
import re
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    tuple: "array",
    set: "array",
    frozenset: "array",
    dict: "object",
    type(None): "null",
}

# 这些 origin 在 JSON 里都是数组：模型对 array 会传 JSON 数组，
# 标成 string 它会传 "a,b" 这种。
_SEQ_ORIGINS = (
    list, tuple, set, frozenset,
    collections.abc.Sequence, collections.abc.MutableSequence,
    collections.abc.Set, collections.abc.MutableSet,
    collections.abc.Iterable, collections.abc.Collection,
)
_MAP_ORIGINS = (dict, collections.abc.Mapping, collections.abc.MutableMapping)

# 「这个默认值转不成 JSON」的哨兵。用独立对象而不是 None ——
# None 本身是个合法的默认值，两者不能混。
_UNSERIALIZABLE = object()


def _is_union(origin: Any) -> bool:
    """是不是一个联合类型。

    `Optional[X]` 与 `X | None` 在类型系统里完全等价，但 `get_origin` 给出
    两个不同的东西：前者是 `typing.Union`，后者是 `types.UnionType`（PEP 604）。
    只判前者，新式写法就会一路掉到最后的 `{"type": "string"}` 兜底 ——
    静默、不报错、只让模型传错类型。
    """
    return origin is typing.Union or origin is types.UnionType


def _literal_schema(values: List[Any]) -> Dict[str, Any]:
    """`Literal[...]` → `enum`，并附上类型（非字符串的取值也要带 type）。

    `enum` 里的值同样要能序列化：`Literal[b"x"]`、或成员值是 `Path` 的 `Enum`，
    都会让**整个请求**在 `json.dumps` 时炸掉。转不了就只留类型、丢掉约束 ——
    少一条约束只是「模型可能传错」，毒化的 schema 是「这一步一定失败」。
    """
    if not values:
        return {}
    kinds = {type(v) for v in values}
    schema: Dict[str, Any] = {}
    if len(kinds) == 1:
        base = _json_type(next(iter(kinds)))
        if base.get("type"):
            schema["type"] = base["type"]
    safe_values = [_json_safe(v) for v in values]
    if all(v is not _UNSERIALIZABLE for v in safe_values):
        # 混合取值：enum 仍然是有用的约束，但不敢断言某一种 type。
        schema["enum"] = safe_values
    return schema


def _json_type(annotation: Any) -> Dict[str, Any]:
    """把 python 类型注解翻译成 JSON Schema 片段。

    认不出的注解退化成 `{"type": "string"}`（历史行为，也是最重要的情形：
    路径、bytes、自定义类都在这里落地成字符串），但**绝不抛异常** ——
    一个奇怪的注解不该让整个工具注册失败。
    """
    # 无注解：不猜，按历史默认当字符串。
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    # 显式 Any 的意思是「什么都能传」。标成 string 会把它能接受的取值砍掉一大半。
    if annotation is Any:
        return {}

    origin = typing.get_origin(annotation)

    # Annotated[X, ...] → 看 X：标注里的元信息（说明、校验器）对模型没有意义。
    if origin is typing.Annotated:
        args = typing.get_args(annotation)
        return _json_type(args[0]) if args else {}

    # Optional[X] / X | None / 多分支联合
    if _is_union(origin):
        branches = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(branches) == 1:
            return _json_type(branches[0])
        parts = [_json_type(b) for b in branches]
        if not parts or any(not p for p in parts):
            # 有一支说不清，就别编一个约束 —— 空的 schema 是「随便」，
            # 编出来的错 schema 是「骗模型」。
            return {}
        return {"anyOf": parts}

    # Literal[...] → enum
    if origin is typing.Literal:
        return _literal_schema(list(typing.get_args(annotation)))

    # 数组类容器（tuple[int, ...] 的 Ellipsis 不算一个类型分支）
    if origin in _SEQ_ORIGINS:
        branches = [a for a in typing.get_args(annotation) if a is not Ellipsis]
        items = [s for s in (_json_type(a) for a in branches) if s]
        out: Dict[str, Any] = {"type": "array"}
        if len(items) == 1:
            out["items"] = items[0]
        elif len(items) > 1:
            out["items"] = {"anyOf": items}
        return out

    # 映射类容器 → object（value 类型不进 schema：JSON 的 additionalProperties
    # 语义比 python 的 Dict[K, V] 粗，写进去反而会把模型吓住）
    if origin in _MAP_ORIGINS:
        return {"type": "object"}

    # 基础标量：用**身份**比较，不用 `annotation in dict` ——
    # 那要求注解可 hash，一个奇怪的可迭代注解会让这里抛 TypeError。
    for py_type, json_type in _PY_TO_JSON.items():
        if annotation is py_type:
            return {"type": json_type}

    # 枚举类 → enum（值取成员的 value）
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return _literal_schema([m.value for m in annotation])

    # 其余一切（Path / bytes / 自定义类）→ 字符串
    return {"type": "string"}


def _json_safe(value: Any) -> Any:
    """把一个默认值转成能塞进 JSON schema 的形式；做不到返回 `_UNSERIALIZABLE`。

    为什么需要它：`Tool.schema()` 会随请求 `json.dumps`。默认值里只要有一个
    `Path(".")`，**整个请求**就在序列化时抛 TypeError —— 而错误现场离写函数的人
    很远（他只会看到「模型没回话」）。宁可少写一个 default，也不能让 schema 变毒。

    `Path` 被转成字符串而不是丢掉：`Path(".")` → `"."` 对模型是有意义的信息，
    丢掉反而可惜。
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN / inf 不是合法 JSON：`json.dumps` 默认会写出裸的 `NaN`，
        # 而 requests 用 `allow_nan=False` 序列化请求体 —— 整个请求会抛 ValueError。
        return value if math.isfinite(value) else _UNSERIALIZABLE
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, enum.Enum):
        return _json_safe(value.value)
    if isinstance(value, (list, tuple, set, frozenset)):
        out = []
        for item in value:
            safe = _json_safe(item)
            if safe is _UNSERIALIZABLE:
                return _UNSERIALIZABLE
            out.append(safe)
        return out
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _UNSERIALIZABLE
            safe = _json_safe(item)
            if safe is _UNSERIALIZABLE:
                return _UNSERIALIZABLE
            out[key] = safe
        return out
    return _UNSERIALIZABLE


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
        if pname == "kernel":
            # 只按**参数名**判：运行期注入用的键就是 "kernel"（见 Agent._execute）。
            # 不能按类型判 —— 类型叫 Kernel 而参数名不叫 kernel 时，注入的键对不上，
            # 那条分支只会把参数悄悄吞掉，函数在调用时缺参报错的现场反而更难查。
            needs_kernel = True
            continue
        schema = _json_type(ann)
        if pname in arg_docs:
            schema["description"] = arg_docs[pname]
        if param.default is not inspect.Parameter.empty:
            # 只写**能序列化**的默认值；`None` 保持不写（既有语义：可省略已由
            # required 表达，写 `"default": null` 会改变一大批工具的形状）。
            if param.default is not None and "enum" not in schema:
                safe = _json_safe(param.default)
                if safe is not _UNSERIALIZABLE:
                    schema["default"] = safe
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
