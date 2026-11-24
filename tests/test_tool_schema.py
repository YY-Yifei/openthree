"""工具层：类型注解 → JSON Schema 的翻译质量，与 schema 的**可序列化性**。

为什么这批断言值得钉：`build_tool` 是**所有**工具的唯一造具入口（内置 / 自造 /
登记 / 命令行四条路径都走它），它的产物会 `json.dumps` 后随请求发给模型 API。

因此这里错一处，后果分两种，而且都很远：

* 类型翻错 —— 模型看到 `{"type": "string"}` 而函数其实要整数，它会传 `"5"`；
  等到 Python 里 `"5" + 1` 才炸，错误现场离写函数的人隔了两层。
* 默认值不可序列化 —— **不是工具报错，是这一步的 LLM 请求整个失败**。
  `def f(p: Path = Path("."))` 就够：`schema["properties"]["p"]["default"]`
  是个 `WindowsPath`，`requests` 在序列化请求体时才抛 `TypeError`。
"""
from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

from openthree.builtins import installed_builtins
from openthree.tool import _json_type, build_tool

from conftest import *  # noqa: F401,F403  复用夹具


# ---------------------------------------------------------------- 类型翻译
# 「同一件事的两种写法必须得到同一个 schema」是最省事、也最有力的判据：
# PEP 604 的 `X | None` 与 PEP 484 的 `Optional[X]` 在类型系统里完全等价，
# 翻出来不一样就一定是内核漏了一条入口。
def test_pep604_optional_matches_typing_optional():
    assert _json_type(int | None) == {"type": "integer"}
    assert _json_type(int | None) == _json_type(Optional[int])


def test_pep604_none_union_for_str_matches_optional():
    assert _json_type(str | None) == {"type": "string"}
    assert _json_type(str | None) == _json_type(Optional[str])


def test_multi_branch_union_becomes_anyof():
    """`int | str` 不是 string，也不是「猜一个」—— 它有两个合法形状。"""
    assert _json_type(int | str) == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    assert _json_type(Optional[int | str]) == _json_type(int | str)


def test_sequence_like_containers_become_arrays():
    """模型对 array 会传 JSON 数组；标成 string 它会传 `"a,b"` 这种。"""
    assert _json_type(tuple[int, ...]) == {"type": "array", "items": {"type": "integer"}}
    assert _json_type(set[str]) == {"type": "array", "items": {"type": "string"}}
    assert _json_type(frozenset[str]) == {"type": "array", "items": {"type": "string"}}
    assert _json_type(Sequence[str]) == {"type": "array", "items": {"type": "string"}}
    # 裸容器（无参数）也要给出 array 本身
    assert _json_type(list) == {"type": "array"}
    assert _json_type(tuple) == {"type": "array"}


def test_fixed_tuple_gives_items_matching_any_branch():
    got = _json_type(tuple[int, str])
    assert got["type"] == "array"
    assert got["items"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}


def test_literal_keeps_type_for_non_strings():
    """`Literal[1, 2]` 旧形返回 `{}` —— 约束和类型一起丢了。"""
    assert _json_type(Literal[1, 2]) == {"type": "integer", "enum": [1, 2]}
    assert _json_type(Literal["a", "b"]) == {"type": "string", "enum": ["a", "b"]}
    # 混合取值：enum 照留，但不敢断言某一种 type
    mixed = _json_type(Literal["a", 1])
    assert mixed.get("enum") == ["a", 1] and "type" not in mixed


def test_annotated_sees_through_to_base_type():
    from typing import Annotated

    assert _json_type(Annotated[int, "说明"]) == {"type": "integer"}
    assert _json_type(Annotated[Optional[str], "x"]) == {"type": "string"}


def test_any_is_unconstrained_rather_than_string():
    """`Any` 的意思是「什么都行」。标成 string 会把可接受的取值凭空砍掉一半。"""
    assert _json_type(Any) == {}


def test_unknown_annotation_still_degrades_safely():
    """认不出的注解退化成 string（历史行为），但绝不能抛异常。"""

    class Whatever:
        pass

    assert _json_type(Whatever) == {"type": "string"}


# ---------------------------------------------------------------- 默认值
def test_path_default_lands_in_schema_as_string():
    def pick(p: Path = Path(".")) -> str:
        """挑路径。

        Args:
            p: 路径
        """
        return str(p)

    t = build_tool(pick)
    assert t.parameters["p"]["default"] == "."
    json.dumps(t.schema())          # 旧形在此抛 TypeError


def test_opaque_default_is_dropped_never_breaks_the_request():
    class Weird:
        def __repr__(self) -> str:  # pragma: no cover
            return "<weird>"

    def touch(x: Any = Weird()) -> str:
        """碰一下。

        Args:
            x: 任意
        """
        return "ok"

    t = build_tool(touch)
    assert "default" not in t.parameters["x"]
    json.dumps(t.schema())


def test_nested_paths_in_container_default_are_converted():
    def listy(x: list = [Path("a"), Path("b")]) -> str:  # noqa: B006
        """列表。

        Args:
            x: 一组路径
        """
        return "ok"

    t = build_tool(listy)
    assert t.parameters["x"]["default"] == ["a", "b"]
    json.dumps(t.schema())


def test_required_reflects_signature_defaults():
    def f(a: str, b: Optional[int] = None, c: str = "x") -> str:
        """示范。

        Args:
            a: 必填
            b: 可空
            c: 有默认
        """
        return a

    t = build_tool(f)
    assert t.required == ["a"]
    # None 默认值不写进 schema（保持既有语义，别顺手改）
    assert "default" not in t.parameters["b"]
    assert t.parameters["c"]["default"] == "x"


# ---------------------------------------------------------------- 注入参数
def test_kernel_param_is_injected_not_exposed():
    def f(path: str, kernel=None) -> str:  # noqa: ANN001
        """示范。

        Args:
            path: 路径
        """
        return path

    t = build_tool(f)
    assert t.needs_kernel is True
    assert "kernel" not in t.parameters
    assert t.required == ["path"]


# ---------------------------------------------------------------- 序列化护栏
# `enum` 里的取值也要 `json.dumps`。Literal / Enum 的取值不像默认值那么「显然安全」：
# `Literal[b"x"]`、或成员值是 `Path` 的 Enum，都能把整个请求毒死 —— 与「默认值里
# 一个 Path」是同一类缺陷，只是从另一个入口进来。
def test_enum_members_are_converted_not_poisoned():
    class PathChoice(enum.Enum):
        A = Path("a")
        B = Path("b")

    got = _json_type(PathChoice)
    # Path 成员值转成字符串（与默认值同一条规则），enum 保留才有约束力
    assert got == {"type": "string", "enum": ["a", "b"]}
    json.dumps(got)


def test_enum_members_that_cannot_convert_drop_the_enum():
    class Opaque:
        pass

    class OpaqueChoice(enum.Enum):
        A = Opaque()

    got = _json_type(OpaqueChoice)
    assert got.get("type") == "string"
    assert "enum" not in got, "转不了的值不能进 schema"
    json.dumps(got)


def test_literal_with_unserializable_value_keeps_type_drops_enum():
    got = _json_type(Literal[b"a", b"b"])
    assert got.get("type") == "string"
    assert "enum" not in got
    json.dumps(got)


def test_non_finite_float_default_is_dropped():
    """NaN / inf 不是合法 JSON：requests 用 allow_nan=False 序列化请求体。"""

    def measure(x: float = float("nan")) -> str:
        """量一下。

        Args:
            x: 数
        """
        return str(x)

    t = build_tool(measure)
    assert "default" not in t.parameters["x"]
    json.dumps(t.schema(), allow_nan=False)


# ---------------------------------------------------------------- 回归护栏
def test_every_builtin_schema_is_json_serializable():
    """39 个内置工具的 schema 必须都能真的发给 API。

    这条不是同义反复：`default` 里塞进任何不可序列化的对象，都会在这里当场炸，
    而不是等到某天有人在 Windows 上跑起来才发现「模型没回话」。
    """
    builtins = installed_builtins()
    assert len(builtins) >= 35, f"内置工具少了？只拿到 {len(builtins)} 个"
    for name, fn in builtins.items():
        t = build_tool(fn, origin="builtin")
        try:
            # allow_nan=False：与 requests 序列化请求体时的严格度一致 ——
            # 裸 `NaN` 在 json.dumps 默认档能过，到 requests 那里才炸。
            json.dumps(t.schema(), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 失败时给出是哪个工具
            raise AssertionError(f"{name} 的 schema 不可序列化：{exc}") from exc


def test_user_function_with_pep604_annotation_reaches_the_model(ws, registry):
    """端到端：模型自造的 function 用新式注解，模型看到的必须是 integer。

    走真实注册路径（写文件 → reload → `reg.get`），而不是只调 `_json_type`：
    要防的正是「翻译函数对了、注册路径没用上」这类分叉。
    """
    (ws.functions_dir / "picker.py").write_text(
        "def pick(path: str, limit: int | None = None) -> str:\n"
        "    '''挑一个。\n\n"
        "    Args:\n"
        "        path: 路径\n"
        "        limit: 上限\n"
        "    '''\n"
        "    return f'{path}:{limit}'\n",
        encoding="utf-8",
    )
    registry.reload()
    tool = registry.get("pick")
    assert tool is not None, "自造工具没注册上"
    assert tool.parameters["limit"] == {"type": "integer", "description": "上限"}
    assert tool.parameters["limit"]["type"] != "string"
    json.dumps(tool.schema())
