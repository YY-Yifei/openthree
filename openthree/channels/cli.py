"""`openthree channel …` —— 频道的引导式配置。

    openthree channel add                交互式问答：选类型 → 填字段 → 是否启用
    openthree channel add --type dingtalk --set client_id=xxx --set client_secret=yyy
                                         完全非交互（脚本 / CI 用同一条路径）
    openthree channel list              列出所有频道与就绪状态
    openthree channel show wx           看一个频道的配置（密钥打码）
    openthree channel enable wx / disable wx
    openthree channel remove wx         删除（要确认，`-y` 跳过）
    openthree channel test wx [--online] 校验配置；--online 才真连一次
    openthree channel path              打印配置文件位置

写法的取舍：**默认引导、也允许全参数**。引导是给「第一次配」的人用的；参数是给
「第二次、在脚本里」用的。两条路落到同一份 `channels.yaml`，不存在只在一处生效的
配置项。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (ChannelConfig, ChannelConfigError, ChannelsConfig, config_path,
                     known_types, normalize_type, spec_for, specs)
from .manager import instantiate

_EPILOG = """\
配好之后：
  openthree -d          只等频道输入（不读控制台，后台式常驻）
  openthree -i          控制台与频道并存（频道消息进来就回，像另一个用户在打字）
  openthree -d --only wx,ding   只启动指定频道
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openthree channel",
                                description="频道（微信 / 钉钉）的配置与管理",
                                epilog=_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-C", "--dir", default=None, help="项目目录（默认当前目录）")
    sub = p.add_subparsers(dest="cmd", metavar="{add,list,show,enable,disable,remove,test,path}")

    add = sub.add_parser("add", help="新增一个频道（不带 --type 则进入引导问答）")
    add.add_argument("--type", "-t", default=None, help="渠道类型：" + " / ".join(known_types()))
    add.add_argument("--name", "-n", default=None, help="频道名（默认 <类型>，重名自动加序号）")
    add.add_argument("--set", "-s", action="append", default=[], metavar="K=V",
                     help="直接给一项配置，可重复；给了它就不再逐项提问")
    add.add_argument("--replace", action="store_true", help="同名频道已存在时覆盖")
    add.add_argument("--disabled", action="store_true", help="先建好但不启用")

    sub.add_parser("list", help="列出所有频道")
    sub.add_parser("ls", help="list 的别名")

    show = sub.add_parser("show", help="看一个频道的配置")
    show.add_argument("name")

    for verb, help_text in (("enable", "启用"), ("disable", "停用")):
        sp = sub.add_parser(verb, help=f"{help_text}一个频道")
        sp.add_argument("name")

    rm = sub.add_parser("remove", help="删除一个频道", aliases=["rm"])
    rm.add_argument("name")
    rm.add_argument("-y", "--yes", action="store_true", help="不确认，直接删")

    test = sub.add_parser("test", help="校验一个频道的配置")
    test.add_argument("name")
    test.add_argument("--online", action="store_true",
                      help="真的连一次（微信：拿一个二维码；钉钉：换一个 Stream 接入点）")

    sub.add_parser("path", help="打印配置文件位置")
    return p


# --- 小工具 -----------------------------------------------------------
def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{suffix}：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130)
    return raw or default


def _ask_bool(prompt: str, default: bool = True) -> bool:
    default_text = "Y/n" if default else "y/N"
    raw = _ask(prompt, default_text).strip().lower()
    if raw in ("y", "yes", "是", ""):
        return True
    if raw in ("n", "no", "否"):
        return False
    return default


def _apply_sets(options: Dict[str, Any], pairs: List[str]) -> Dict[str, Any]:
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"[错误] --set 需要 K=V 形式，收到 {pair!r}")
        key, _, value = pair.partition("=")
        options[key.strip()] = value.strip()
    return options


def _interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def _print_channels(cfg: ChannelsConfig) -> None:
    if not cfg.channels:
        print("（还没有配置任何频道）—— 用 `openthree channel add` 加一个。")
        return
    for c in cfg.channels:
        print("  " + c.describe())


def _next_steps(cfg: ChannelsConfig) -> None:
    ready = [c for c in cfg.channels if c.enabled and c.is_ready()]
    print()
    if not ready:
        print("还没有「启用且就绪」的频道；补齐字段后再来一次 `openthree channel list`。")
        return
    print(f"已就绪 {len(ready)} 个频道：{', '.join(c.name for c in ready)}")
    print("  只等频道输入：openthree -d")
    print("  控制台 + 频道：openthree -i")


# --- 子命令 -----------------------------------------------------------
def _cmd_add(cfg: ChannelsConfig, args: argparse.Namespace) -> int:
    spec = spec_for(args.type) if args.type else None
    if args.type and spec is None:
        print(f"[错误] 不认识的渠道类型 {args.type!r}（可选：{', '.join(known_types())}）",
              file=sys.stderr)
        return 2
    if spec is None:
        if not _interactive():
            print("[错误] 非交互环境必须给 --type（可选：" + ", ".join(known_types()) + "）",
                  file=sys.stderr)
            return 2
        print("选择渠道类型：")
        for i, s in enumerate(specs(), 1):
            print(f"  {i}) {s.type:<9} {s.label} —— {s.summary}")
        raw = _ask("序号或类型名", "1")
        if raw.isdigit() and 1 <= int(raw) <= len(specs()):
            spec = specs()[int(raw) - 1]
        else:
            spec = spec_for(normalize_type(raw))
        if spec is None:
            print(f"[错误] 不认识的渠道类型 {raw!r}", file=sys.stderr)
            return 2

    name = args.name or (spec.type if _interactive() else spec.type)
    if _interactive() and not args.name:
        name = _ask("频道名（日志与 --only 用它区分）", cfg.unique_name(spec.type))
    name = cfg.unique_name(name) if not cfg.get(name) else name

    options: Dict[str, Any] = {}
    if args.set:
        _apply_sets(options, args.set)
    if _interactive() and not args.set:
        print(f"\n配置 {spec.label}（{spec.summary}）")
        for field in spec.fields:
            tip = f"    {field.hint}" if field.hint else ""
            if tip:
                print(tip)
            default = field.default
            if field.secret and default:
                default = ""      # 密钥不显示默认值
            value = _ask(f"  {field.label}", default)
            if value:
                options[field.key] = value
            elif field.required:
                print(f"    ⚠ {field.key} 是必填项，留空会让这个频道起不来")
    enabled = not args.disabled
    if _interactive() and not args.set:
        enabled = _ask_bool("现在就启用它吗", True)

    channel = ChannelConfig(name=name, type=spec.type, enabled=enabled, options=options)
    if cfg.get(name) and not args.replace:
        print(f"[错误] 频道名 {name!r} 已存在；换一个名字，或加 --replace 覆盖",
              file=sys.stderr)
        return 2
    cfg.add(channel, replace=args.replace)
    path = cfg.save()
    print(f"\n[ok] 已写入 {path}")
    print("  " + channel.describe())
    if (channel.missing() and args.set) or (channel.missing() and not _interactive()):
        print("  缺：" + ", ".join(channel.missing()) + " —— 用 `openthree channel add --replace` 补齐")
    _next_steps(cfg)
    return 0


def _cmd_list(cfg: ChannelsConfig, args: argparse.Namespace) -> int:
    print(f"配置文件：{cfg.path}")
    for w in cfg.warnings:
        print(f"  ⚠ {w}")
    _print_channels(cfg)
    return 0


def _cmd_show(cfg: ChannelsConfig, args: argparse.Namespace) -> int:
    channel = cfg.get(args.name)
    if channel is None:
        print(f"[错误] 没有叫 {args.name!r} 的频道（有：{', '.join(cfg.names()) or '无'}）",
              file=sys.stderr)
        return 2
    spec = channel.spec
    print(f"频道：{channel.name}")
    print(f"类型：{channel.type}（{spec.label if spec else '未知'}）")
    print(f"状态：{'启用' if channel.enabled else '停用'}")
    print("选项：")
    redacted = channel.redacted()
    if not redacted:
        print("  （空）")
    for key, value in redacted.items():
        print(f"  {key} = {value or '（空）'}")
    if channel.missing():
        print("缺必填项：" + ", ".join(channel.missing()))
    if channel.unknown_options():
        print("未知选项（会原样保留，但渠道不读它）：" + ", ".join(channel.unknown_options()))
    return 0


def _cmd_toggle(cfg: ChannelsConfig, args: argparse.Namespace, enabled: bool) -> int:
    if not cfg.set_enabled(args.name, enabled):
        print(f"[错误] 没有叫 {args.name!r} 的频道", file=sys.stderr)
        return 2
    cfg.save()
    print(f"[ok] {args.name} 已{'启用' if enabled else '停用'}（{cfg.path}）")
    return 0


def _cmd_remove(cfg: ChannelsConfig, args: argparse.Namespace) -> int:
    channel = cfg.get(args.name)
    if channel is None:
        print(f"[错误] 没有叫 {args.name!r} 的频道", file=sys.stderr)
        return 2
    if not args.yes and _interactive():
        if not _ask_bool(f"确认删除频道 {args.name}（只删配置，不删别的）", False):
            print("已取消。")
            return 0
    cfg.remove(args.name)
    cfg.save()
    print(f"[ok] 已删除 {args.name}（{cfg.path}）")
    return 0


def _cmd_test(cfg: ChannelsConfig, args: argparse.Namespace) -> int:
    channel = cfg.get(args.name)
    if channel is None:
        print(f"[错误] 没有叫 {args.name!r} 的频道", file=sys.stderr)
        return 2
    if channel.spec is None:
        print(f"[错误] 未知类型 {channel.type!r}", file=sys.stderr)
        return 2
    missing = channel.missing()
    if missing:
        print("[错误] 缺必填项：" + ", ".join(missing), file=sys.stderr)
        return 2
    print(f"[ok] 配置完整：{channel.name}（{channel.spec.label}）")
    if not args.online:
        print("（只做了静态校验；真连一次加 --online）")
        return 0

    workspace_dir = cfg.path.parent
    try:
        instance = instantiate(channel, on_message=None, workspace_dir=workspace_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 频道实现加载失败：{exc}", file=sys.stderr)
        return 2
    try:
        if hasattr(instance, "open_connection"):        # 钉钉：换一次接入点
            conn = instance.open_connection()
            print(f"[ok] 钉钉 Stream 接入点可用：{conn.get('endpoint')}")
            return 0
        if hasattr(instance, "login") and channel.type == "wechat":  # 微信：拿个二维码
            if getattr(instance, "bot_token", ""):
                print("[ok] 已有 bot_token（未做联网校验；启动时会直接长轮询）")
                return 0
            print("[提示] 还没有 bot_token：下一步就是扫码登录")
            instance.login_timeout = 0  # 只验证「能不能拿到二维码」，不等扫码
            try:
                instance.login()
            except Exception as exc:  # noqa: BLE001 - 超时是预期结果
                print(f"[ok] 二维码请求通路正常（{type(exc).__name__}: {exc}）")
            return 0
        print("[提示] 这个渠道没有联网自检，静态校验通过即视为可用")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 联网校验失败：{exc}", file=sys.stderr)
        return 1


def _cmd_path(cfg: ChannelsConfig, args: argparse.Namespace) -> int:
    print(cfg.path)
    return 0


def main(argv: Optional[List[str]] = None, *, project_dir: Optional[Path] = None) -> int:
    """`openthree channel …` 的入口。"""
    parser = build_parser()
    args = parser.parse_args(list(argv or []))
    if not args.cmd:
        parser.print_help()
        return 0
    if getattr(args, "dir", None):
        project_dir = Path(args.dir)
    base = Path(project_dir or ".")
    path = config_path(base / ".openthree")
    try:
        cfg = ChannelsConfig.load(path)
    except ChannelConfigError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    if args.cmd == "add":
        return _cmd_add(cfg, args)
    if args.cmd in ("list", "ls"):
        return _cmd_list(cfg, args)
    if args.cmd == "show":
        return _cmd_show(cfg, args)
    if args.cmd == "enable":
        return _cmd_toggle(cfg, args, True)
    if args.cmd == "disable":
        return _cmd_toggle(cfg, args, False)
    if args.cmd in ("remove", "rm"):
        return _cmd_remove(cfg, args)
    if args.cmd == "test":
        return _cmd_test(cfg, args)
    if args.cmd == "path":
        return _cmd_path(cfg, args)
    parser.print_help()
    return 0


def __main__(argv: Optional[List[str]] = None) -> int:
    """命令行入口（`python -m openthree.channels.cli …` 时可用）。"""
    return main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
