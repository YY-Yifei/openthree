"""内置工具 + 注册器：编码、语法预检、冲突记录、路径约束。"""
from __future__ import annotations

from pathlib import Path

from openthree.builtins import _decode, _split_tags, run_shell
from openthree.registry import Registry

from conftest import *  # noqa: F401,F403  复用夹具


# ---------------------------------------------------------------- tags 归一
def test_split_tags_handles_list_and_str():
    assert _split_tags("a, b") == ["a", "b"]
    assert _split_tags(["x", "y"]) == ["x", "y"]      # 模型误传 JSON 数组
    assert _split_tags("中文，标签") == ["中文", "标签"]  # 全角逗号
    assert _split_tags(None) == []
    assert _split_tags("") == []


def test_note_accepts_list_tags(make_agent):
    from openthree.builtins import note
    make_agent()
    assert note("想了一下", ["a", "b"]).startswith("[ok]")


def test_remember_accepts_list_tags(make_agent, ws):
    from openthree.builtins import remember
    make_agent()
    assert remember("记住了", ["x", "y"]).startswith("[ok]")
    assert (ws.memory_dir / "memory.jsonl").exists()


# ---------------------------------------------------------------- run_shell 编码
def test_decode_utf8_bytes():
    assert _decode("中文".encode("utf-8")) == "中文"


def test_decode_windows_bytes():
    text = "系统找不到指定的文件。"
    assert _decode(text.encode("gbk")) == text


def test_decode_none():
    assert _decode(None) == ""


def test_run_shell_reports_exit_code():
    out = run_shell("python -c \"print('hello')\"")
    assert "exit=0" in out and "hello" in out


# ------------------------------------------------------- run_shell 超时（防挂死）
# 被保护的不变量：超时必须连**子孙进程**一起杀，且在管道 EOF 到不来时也不得挂住。
# 这正是 subprocess.run(shell=True, timeout=N) 的死穴：Windows 下 shell=True 起
# cmd.exe，真实命令是它的「孙进程」；超时时只杀 cmd.exe，随后内部 communicate()
# 等 stdout/stderr 读到 EOF，而孙进程仍攥着管道写端 -> EOF 永不到来 -> 连同
# timeout 设置一起永久挂住。实测：timeout=4 的命令会卡到 60.1s；孙进程常驻则永久卡死。


def _wall(fn, *a, **kw):
    import time
    t = time.time()
    out = fn(*a, **kw)
    return time.time() - t, out


def test_run_shell_timeout_returns_promptly():
    """直接子进程超时：应在 timeout 附近返回，而不是拖到进程自然结束。"""
    elapsed, out = _wall(run_shell, "ping -n 30 127.0.0.1", timeout=3)
    assert elapsed < 12, f"超时未被及时处理，耗时 {elapsed:.1f}s"
    assert "超时" in out


def test_run_shell_kills_grandchild():
    """孙进程（常驻）持有输出管道：也必须被杀掉整棵树，绝不能挂住。"""
    elapsed, out = _wall(
        run_shell, 'python -c "import time;time.sleep(3600)"', timeout=3)
    assert elapsed < 12, f"被孙进程挂住，耗时 {elapsed:.1f}s"
    assert "超时" in out and "强杀" in out


def test_run_shell_interactive_gets_eof_not_hang():
    """交互式命令等输入时，stdin 接 DEVNULL，应立刻 EOF 退出而不是挂住。"""
    elapsed, out = _wall(run_shell, 'python -c "input()"', timeout=5)
    assert elapsed < 8, f"交互式命令挂住，耗时 {elapsed:.1f}s"
    assert "EOF" in out or "exit=" in out


def test_run_shell_bad_timeout_does_not_crash():
    """模型可能把 timeout 传成非数字，不能因此抛异常。"""
    out = run_shell('python -c "print(1)"', timeout="abc")
    assert "exit=0" in out


def test_run_shell_clamps_huge_timeout():
    """离谱的超时值被截到上限，避免一条命令占住整个循环几小时。"""
    from openthree.builtins import _SHELL_TIMEOUT_MAX
    assert _SHELL_TIMEOUT_MAX == 3600


def _reap(pidfile):
    """结束本用例留下的孙进程，返回被结束的 pid。

    这个孙进程是刻意脱离的：它已经不是 `run_shell` 的子进程，杀树够不着它，
    只能由用例自己收尾。pid 文件缺失或进程已自行退出时返回 None。
    """
    import os
    import signal

    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None
    return pid


def test_run_shell_survives_orphan_holding_pipe(tmp_path):
    """孙进程攥着管道而直接子进程已退出时，也必须按时返回、不得挂死。

    被保护的不变量：**超时兜底路径本身不得二次阻塞**。危险形态是：命令自己
    先退出，把一个继承了 stdout/stderr 的孤儿孙进程留下 —— 于是直接子进程
    已死（没有 PID 可杀，`_kill_tree` 只能报 already-exited），而管道写端仍被
    攥着，EOF 永不到来。此刻若去 `close()` 那两个管道对象，就会和
    `communicate()` 里正阻塞在读上的 daemon 读线程抢同一把缓冲区锁而互锁，
    把「超时」变成永久挂死。正确做法是放弃读取、立刻返回。

    孙进程由用例自己收尾（`_reap`）；它只睡 120s 而非常驻，是收尾万一失败时
    的兜底 —— 跑一次测试不会在机器上留下过夜垃圾。
    """
    pidfile = tmp_path / "holder.pid"
    spawner = tmp_path / "orphan_spawner.py"
    spawner.write_text(
        "import subprocess, sys\n"
        "# 孙进程继承本进程的 stdout/stderr（即 run_shell 给的那根管道）。\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "                     close_fds=False)\n"
        "open(sys.argv[1], 'w').write(str(p.pid))\n"
        "print(p.pid, flush=True)\n",
        encoding="utf-8",
    )
    reaped = None
    try:
        elapsed, out = _wall(
            run_shell, 'python "%s" "%s"' % (spawner, pidfile), timeout=3)
        assert elapsed < 20, f"孤儿孙进程把超时兜底拖成了挂死，耗时 {elapsed:.1f}s"
        assert "超时" in out
    finally:
        reaped = _reap(pidfile)
    # 收尾必须真发生：pid 没记下来或杀不掉，这个用例就会在机器上攒垃圾。
    assert reaped is not None, "孙进程 pid 未被记录，用例收尾失效"


# ---------------------------------------------------------------- write_function
def test_write_function_rejects_bad_syntax(make_agent, ws):
    from openthree.builtins import write_function
    make_agent()
    msg = write_function("broken_tool", "def broken(:\n  pass\n")
    assert msg.startswith("[错误]") and "line" in msg


def test_write_function_accepts_good_syntax(make_agent, ws):
    from openthree.builtins import write_function
    make_agent()
    msg = write_function("good_tool", "def good(a: int) -> str:\n    '''ok'''\n    return str(a)\n")
    assert msg.startswith("[ok]")
    assert (ws.functions_dir / "good_tool.py").exists()


# ---------------------------------------------------------------- 注册器
def test_registry_records_name_conflict(ws):
    (ws.functions_dir / "clash.py").write_text(
        "def read_file(path: str):\n    '''shadow'''\n    return 'shadow'\n", encoding="utf-8")
    reg = Registry(ws)
    from openthree.builtins import BUILTIN_FUNCTIONS
    for name, fn in BUILTIN_FUNCTIONS.items():
        reg.register_builtin(name, fn)
    reg.freeze_builtins()
    reg.reload()
    # 同名覆盖不许静默 —— 但它记在 overrides（距离裁决的正常结果），
    # 不是 load_errors（真故障）。两者混在一起，模型会把正常覆盖当异常。
    assert any(o.tool == "read_file" and o.loser_kind == "builtin" for o in reg.overrides)
    assert reg.get("read_file").origin == "user"


def test_registry_loads_user_tool(ws):
    (ws.functions_dir / "hello.py").write_text(
        "def hello(name: str = '世界') -> str:\n"
        "    '''\'打招呼\'\n\n    Args:\n        name: 名字\n    '''\n"
        "    return f'你好 {name}'\n",
        encoding="utf-8",
    )
    reg = Registry(ws)
    reg.reload()
    tool = reg.get("hello")
    assert tool is not None and tool.origin == "user"
    assert "name" in tool.parameters
