# openthree/attach.py —— 与 openthree/decide.py 同构：独立模块，由
# `builtins.installed_builtins(a2a=…)` 在**注册期**按条件并入工具集。
# 不落 `.openthree/functions/`：它随包发布，用户升级后 `-A2A` 起来就有。
"""A2A 回合里给答复附文件。

分界只有一句：**工具负责记下，频道负责发**。`attach_file` 往「当前回合」那条
`InboundMessage.attachments` 里追加一项；路径白名单、体积上限、名字消毒全部在
频道层（`channels/a2a.py` 的 `_outbound_file_parts`）—— 安全判定只该有一份，
而且不能被智能体手上的任何工具绕过。

「当前回合」由桥发布到 `KERNEL.state[("attach-turn", 线程 id)]`：桥在同一个线程里
跑 `Runtime.run()`，ReAct 也就在这个线程里调工具，所以工具拿到的正是那条 msg。
键里带线程 id 是防串台 —— 同进程里两个频道的桥不会互相覆盖。没有这个键，就
意味着这次答复发不出文件（控制台回合、微信 / 钉钉回合、没起任何频道），那时
工具必须说实话，不能假装已经附上了。
"""
from __future__ import annotations

import threading
from pathlib import Path

from .kernel import KERNEL

# 桥与工具共用的键前缀：桥侧 import 这一个常量，两边不会各写一个字符串。
TURN_SLOT = "attach-turn"

# 一条答复最多附几个文件。**真源在这里**：`attach_file` 用它决定「还收不收」，
# 频道层（`channels/a2a.py`）import 同一个值做同样的判断。两处各写一个 4，
# 回执与行为迟早发散 —— 工具说「已记下」，频道层再说「发不出去」。
MAX_OUTBOUND_FILES = 4


def attach_file(path: str, name: str = "") -> str:
    """把一个本地文件附到本次 A2A 答复上（对方会收到一个 file part）。

    只在对方明确要文件时用；对方只是问一句结论，就别附。`path` 给绝对路径
    （相对路径按进程 cwd 解析，你多半猜不对）。回执说「已记下」只是记下了：
    真正发不发得出去由频道层校验 —— 根目录外、超过体积上限、命中密钥黑名单
    都会被拒，原因会写进答复正文，所以别在正文里另说「见附件」。

    Args:
        path: 文件路径（建议绝对路径）。
        name: 对方看到的文件名；留空用原文件名。

    Returns:
        一句话回执：「已记下 …」或「附不上：…」；这次答复不支持文件时说明原因。
    """
    turn = KERNEL.state.get((TURN_SLOT, threading.get_ident()))
    if turn is None:
        return ("这次答复发不出文件：当前不是一个支持出站文件的频道回合"
                "（没起频道，或这个频道不支持）。请在正文里说明文件在哪，"
                "别写「见附件」—— 那是骗人。")
    raw = str(path or "").strip()
    if not raw:
        return "附不上：path 是空的。"
    # `expanduser()` 也在 try 里：家目录解析不出来时它抛 RuntimeError（容器 / cron /
    # systemd 里 `$HOME` 没设就常见），而 `is_file()` / `stat()` 对含 NUL 的路径抛
    # ValueError。漏掉任何一种，工具就违约 —— 模型拿到的是一段 traceback 而不是
    # 「附不上：…」。频道层对同一件事用的是同一组异常类型。
    try:
        p = Path(raw).expanduser()
        if not p.is_file():
            return f"附不上：{raw} 不是文件（或不存在）。"
        size = p.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        detail = getattr(exc, "strerror", None) or exc
        return f"附不上：{raw} 读不到（{type(exc).__name__}: {detail}）。"
    if size <= 0:
        return f"附不上：{p} 是空文件（0 字节）。"
    # 数量上限在这里判，不是只在频道层：「已记下、会随本次答复发出」这句话必须
    # 为真。留到频道层，第 5 个文件的回执就是假的（它一定发不出去）。
    if len(turn.attachments) >= MAX_OUTBOUND_FILES:
        return (f"附不上：这次答复已经附满 {MAX_OUTBOUND_FILES} 个文件了。"
                "要发更多就分成几条答复，或者先在本地打包成一个文件。")
    turn.attachments.append({"path": str(p), "name": str(name or "")})
    return (f"已记下 {p.name}（{size} 字节），会随本次答复发出。"
            "不在允许目录内或体积超限的话会被拒，原因会写进正文。")


def __main__(argv=None) -> int:
    """命令行入口（`python -m openthree.attach --help`）。"""
    from .cli_tool import run_cli

    return run_cli(globals(), argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
