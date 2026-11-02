"""附随提示词：function 文件自带、`reload` 时进 system 末尾。

被保护的不变量（动这一层必须守住）：

  A1  **不写就是零**：文件里没有 `__system_prompts__` 时，system 与「没有这个
      功能」逐字节相同 —— 少一个字节和多一个字节一样糟，前者悄悄改了行为，
      后者每步重算工具 schema。
  A2  它**不是工具**：不进 `names()` / `schemas()` / `digest()`，模型调不到它。
  A3  写坏了只记错：求值抛异常、类型不对、带参数，都只进 `prompt_errors`，
      不连累这个文件的工具，也不连累别的文件的提示词。
  A4  长度有上限：单文件截断；总量按「近者优先」保留，被挤掉的记错而不是静默丢。
  A5  每次 reload 都按内容寻址留一份**能读回来**的痕迹
      （`logs/attached-prompts/<sha8>.md`）—— `.openthree/` 被 gitignore 排除，
      没有它，「模型改了自己的 system」就是一件它自己看不见的事。文件名必须
      等于文件字节的 sha256（按字节写，不按文本写）。
  A6  **求值在 reload，不在每一步**：同一次 reload 里 system 逐字节稳定，
      改了文件而不 reload 不该改变 system —— 这是它能进缓存前缀的前提。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from openthree.cli_tool import run_tools
from openthree.prompt import (build_attached_block, build_static_block,
                              build_system_block)
from openthree.registry import (MAX_ATTACHED_PROMPT_CHARS, MAX_ATTACHED_PROMPT_TOTAL,
                                Registry)
from openthree.workspace import Workspace

from conftest import *  # noqa: F401,F403  复用 ws / registry 夹具


def _write(ws: Workspace, name: str, body: str, in_dir=None) -> Path:
    """往某个 functions 目录写一个文件（默认本项目）。"""
    d = Path(in_dir) if in_dir else ws.functions_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def _plain(name: str, prompt: str = "", *, module_doc: str = "测试用文件。",
           raw: str = "") -> str:
    """拼一个「一个工具 + 可选提示词」的最小文件。

    `prompt` 走 `repr()`（普通文本）；要写**非字符串**或精确长度的表达式时用
    `raw` —— 直接拼进源码，不经过 repr。两者都不给就没有这个符号。
    """
    body = f'"""{module_doc}"""\n\n\ndef {name}() -> str:\n    """做个动作。"""\n    return "{name}"\n'
    expr = raw or (repr(prompt) if prompt else "")
    if expr:
        body += f"\n\n__system_prompts__ = {expr}\n"
    return body


# ---------------------------------------------------------------- 收集
def test_three_forms_are_collected(ws, registry):
    """三种形态（str / 零参函数 / list）都能收，且顺序与段数对得上。"""
    _write(ws, "a_str", _plain("alpha", "## 甲的纪律\n先 alpha 再 beta。"))
    _write(ws, "b_list", _plain("beta", "") + '''

__system_prompts__ = ["## 第一段", "## 第二段"]
''')
    _write(ws, "c_callable", _plain("gamma", "") + '''

def __system_prompts__() -> str:
    """给内核的附随说明（不会注册成工具）。"""
    return "## 丙的纪律"
''')
    registry.reload()

    by_file = {Path(p.file).stem: p for p in registry.prompts}
    assert set(by_file) == {"a_str", "b_list", "c_callable"}
    assert "甲的纪律" in by_file["a_str"].text
    assert by_file["b_list"].parts == 2
    assert "第一段" in by_file["b_list"].text and "第二段" in by_file["b_list"].text
    assert "丙的纪律" in by_file["c_callable"].text
    assert by_file["c_callable"].parts == 1

    block = build_attached_block(registry)
    assert "来自 function 的附随说明" in block
    for want in ("甲的纪律", "第一段", "丙的纪律"):
        assert want in block
    # 来源标注：读者要能看出这段话是谁写的、在哪一档
    assert "project" in block


def test_prompt_text_lands_in_system(ws, registry):
    """收集到的文本必须真的出现在 `messages[0]` 里（而不是只躺在 registry）。"""
    _write(ws, "a_str", _plain("alpha", "## 甲的纪律\n先 alpha 再 beta。"))
    registry.reload()
    system = build_system_block(ws, registry, model="m")
    assert "甲的纪律" in system
    # 位置：拼在最后 —— system 以内核常量段开头，以附随段收尾
    assert system.endswith(build_attached_block(registry))
    assert system.index("# 你的处境") < system.index("甲的纪律")


# ---------------------------------------------------------------- A1 逐字节
def test_system_is_byte_identical_without_the_symbol(ws, registry):
    """A1：没有那个符号时，system 与「没有这个功能」逐字节相同。"""
    _write(ws, "plain", _plain("delta", "", module_doc="没有任何提示词的文件。"))
    registry.reload()

    system = build_system_block(ws, registry, model="m")
    old_way = (f"{ws.read_system_prompt().strip()}\n\n"
               f"{build_static_block(ws, registry, model='m')}")
    assert system == old_way
    assert build_attached_block(registry) == ""


# ---------------------------------------------------------------- A2 不是工具
def test_prompt_is_not_a_tool(ws, registry):
    """A2：它不进工具清单 / schema / digest —— dunder 天然不被注册。"""
    _write(ws, "a_str", _plain("alpha", "## 甲的纪律"))
    _write(ws, "c_callable", _plain("gamma", "") + '''

def __system_prompts__() -> str:
    """说明。"""
    return "## 丙的纪律"
''')
    registry.reload()

    assert "alpha" in registry.names() and "gamma" in registry.names()
    assert not [n for n in registry.names() if "system_prompts" in n]
    assert not [s for s in registry.schemas()
                if "system_prompts" in s.get("function", {}).get("name", "")]
    assert "__system_prompts__" not in registry.digest()


# ---------------------------------------------------------------- A3 失败隔离
def test_wrong_type_is_recorded_but_tool_stays(ws, registry):
    """类型不对：记 `prompt_errors`，工具照常注册。"""
    _write(ws, "bad", _plain("epsilon", raw="42"))
    registry.reload()

    assert "epsilon" in registry.names()
    assert registry.prompts == []
    assert any("只接受" in e.error for e in registry.prompt_errors)


def test_parameterized_prompt_is_rejected_but_tool_stays(ws, registry):
    """带参数的函数：零参约束被违反 → 记错，不静默当成「没有提示词」。"""
    _write(ws, "param", _plain("zeta", "") + '''

def __system_prompts__(n: int) -> str:
    """错：附随说明不需要知道此刻在干什么。"""
    return "x" * n
''')
    registry.reload()

    assert "zeta" in registry.names()
    assert registry.prompts == []
    assert any("求值失败" in e.error for e in registry.prompt_errors)


def test_raising_prompt_is_isolated(ws, registry):
    """一个文件抛异常，别的文件照常进 system，两边工具都在。"""
    _write(ws, "boom", _plain("eta", "") + '''

def __system_prompts__() -> str:
    """炸。"""
    raise RuntimeError("boom")
''')
    _write(ws, "fine", _plain("theta", "## 正常"))
    registry.reload()

    assert {"eta", "theta"} <= set(registry.names())
    assert any("boom" in e.error for e in registry.prompt_errors)
    assert [p.text for p in registry.prompts] == ["## 正常"]


def test_empty_prompt_adds_nothing(ws, registry):
    """空串 / 全空白：不占位、不多字节。"""
    _write(ws, "empty", _plain("iota", ""))
    registry.reload()
    assert registry.prompts == []
    assert registry.prompt_errors == []
    assert build_attached_block(registry) == ""


# ---------------------------------------------------------------- A4 上限
def test_single_file_is_truncated(ws, registry):
    """单文件超限：截断并写明原文长度，不静默砍掉。"""
    long_text = "x" * (MAX_ATTACHED_PROMPT_CHARS + 500)
    _write(ws, "long", _plain("kappa", raw=repr(long_text)))
    registry.reload()

    p = registry.prompts[0]
    assert p.original_chars == MAX_ATTACHED_PROMPT_CHARS + 500
    assert len(p.text) < p.original_chars
    # 上限**包含**那句截断说明：常量说的是「进 system 的上限」，不是「保留多少原文」。
    assert len(p.text) <= MAX_ATTACHED_PROMPT_CHARS
    assert "已截断" in p.text and str(p.original_chars) in p.text


def test_total_limit_keeps_nearest(tmp_path):
    """A4：总量超限时按「近者优先」保留，被挤掉的记错 —— profile 的让给 project。"""
    prof = tmp_path / "prof"
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)
    ws = Workspace(tmp_path / "proj", profiles=(prof,)).init()
    reg = Registry(ws)

    chunk = "y" * (MAX_ATTACHED_PROMPT_CHARS - 500)
    for i in range(3):                       # project：3 × 3500 = 10500
        _write(ws, f"local{i}", _plain(f"fn{i}", repr(chunk)))
    _write(ws, "shared", _plain("shared_fn", repr(chunk)),
           in_dir=prof / ".openthree" / "functions")   # profile：再来 3500 → 超 12000

    reg.reload()

    kept = [Path(p.file).stem for p in reg.prompts]
    assert kept == ["local0", "local1", "local2"]
    assert sum(len(p.text) for p in reg.prompts) <= MAX_ATTACHED_PROMPT_TOTAL
    dropped = [e for e in reg.prompt_errors if "超出总量上限" in e.error]
    assert [Path(e.file).stem for e in dropped] == ["shared"]


# ---------------------------------------------------------------- 排序
def test_project_prompts_come_before_profile(tmp_path):
    """顺序按 distance 升序：project 先读，profile 后读（与同名工具同一根轴）。"""
    prof = tmp_path / "prof"
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)
    ws = Workspace(tmp_path / "proj", profiles=(prof,)).init()
    reg = Registry(ws)

    _write(ws, "local", _plain("local_fn", "## 本项目"))
    _write(ws, "shared", _plain("shared_fn", "## profile"),
           in_dir=prof / ".openthree" / "functions")
    reg.reload()

    assert [Path(p.file).stem for p in reg.prompts] == ["local", "shared"]
    assert reg.prompts[0].label == "project"
    assert reg.prompts[1].distance > reg.prompts[0].distance
    block = build_attached_block(reg)
    assert block.index("本项目") < block.index("profile")


# ---------------------------------------------------------------- A5 痕迹
def test_content_addressed_trace_is_readable_and_idempotent(ws, registry):
    """A5：落盘的文件能读回同样的文本；反复 reload 不重复落。"""
    _write(ws, "a_str", _plain("alpha", "## 甲的纪律\n第二行"))
    registry.reload()

    p = registry.prompts[0]
    out_dir = ws.logs_dir / "attached-prompts"
    path = out_dir / f"{p.sha256[:8]}.md"
    assert path.exists()
    # 按**字节**断言，不用 `read_text`：后者会把 CRLF 归一化成 `\n`，于是
    # 「文件里其实是 CRLF、文件名的 sha256 与内容对不上」这种错它一个字都
    # 看不出来 —— Windows 上 `write_text` 的默认换行正是这么来的。
    data = path.read_bytes()
    assert data == p.text.encode("utf-8")
    assert hashlib.sha256(data).hexdigest() == p.sha256

    count = len(list(out_dir.glob("*.md")))
    registry.reload()
    assert len(list(out_dir.glob("*.md"))) == count
    # `prompts` 必须在 reload 开头清空：跨 reload 累积是这类代码最常见的一次性 bug
    assert len(registry.prompts) == 1, "列表跨 reload 累积了"
    assert build_attached_block(registry).count("甲") >= 1


# ---------------------------------------------------------------- 求值时机
def test_prompt_is_evaluated_at_reload_not_every_step(ws, registry):
    """A6：system 的字节只在 reload 时变 —— 这是它能进缓存前缀的前提。

    「每步读一次文件」「每步调一次那个零参函数」都不改变内容对不对，但会让
    system 每步都变，于是它后面一万多 token 的工具 schema 与全部历史每步重算。
    求值时机因此是**行为的一部分**，不是实现细节：用「改了文件但不 reload」
    把它钉住。
    """
    path = _write(ws, "a_str", _plain("alpha", "## 第一版"))
    registry.reload()
    first = build_system_block(ws, registry, model="m")
    assert first == build_system_block(ws, registry, model="m"), "步与步之间必须逐字节相同"

    _write(ws, "a_str", _plain("alpha", "## 第二版"))
    assert build_system_block(ws, registry, model="m") == first, "没 reload 就不该变"

    registry.reload()
    second = build_system_block(ws, registry, model="m")
    assert "## 第二版" in second and second != first, "reload 之后才换"
    assert path.exists()


# ---------------------------------------------------------------- 端到端
PDF_KIT = '''\
"""pdf_kit —— 抽取 PDF 文本，附带用法纪律。"""


def pdf_info(path: str) -> str:
    """先看页数与是不是扫描版。

    Args:
        path: PDF 路径

    Returns:
        一行摘要。
    """
    return "1 page"


def __system_prompts__() -> str:
    """给内核的附随说明（不会注册成工具）。"""
    return "## 用 pdf_kit 时的纪律\\n- 先 pdf_info 再 pdf_text。"


def __main__(argv=None) -> int:
    """命令行入口。"""
    from openthree.cli_tool import run_cli
    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
'''


def test_end_to_end_write_function_then_reload(make_agent, ws):
    """整条链路：`write_function` → `reload_functions()` → 下一步的 system 里就有它。

    这是这个功能唯一会被用户看见的形态，所以它不该只由单元测试覆盖。
    """
    from openthree.builtins import reload_functions, write_function
    from openthree.kernel import KERNEL
    from openthree.prompt import build_messages

    make_agent()
    msg = write_function(name="pdf_kit", code=PDF_KIT)
    assert msg.startswith("[ok]")
    # 落盘那一刻就告诉它：你写的不是注释，是会进 system 的文本
    assert "附随提示词" in msg

    reload_functions()
    system = build_messages(ws, KERNEL.registry, [], model="m", iteration=1,
                            max_iterations=10)[0]["content"]
    assert "用 pdf_kit 时的纪律" in system
    assert "pdf_kit.py" in system          # 来源标注：谁写的
    assert "pdf_info" in KERNEL.registry.names()
