"""多频道管理器：按配置建实例、各开一条线程跑、出错只影响自己。

一条原则：**频道之间互不拖累**。扫码失败、token 过期、网络掉线 —— 都只让
`Status` 里那一行变红，别的频道照常收消息，控制台也照常可用。

`run()` 是阻塞的，所以每个频道一条守护线程；`stop()` 里逐个请求退出并等它收尾。
"""
from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from .base import Channel
from .config import ChannelConfig, ChannelsConfig, config_path, spec_for


class ChannelManagerError(RuntimeError):
    """没有任何可用频道 / 频道实现导入失败等「起不来」的情形。"""


@dataclass
class Status:
    """一个频道的运行时状态（`channel list` 与启动摘要都读它）。"""

    config: ChannelConfig
    channel: Optional[Channel] = None
    thread: Optional[threading.Thread] = None
    error: str = ""
    started_at: float = 0.0

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def state(self) -> str:
        if self.error:
            return f"✗ {self.error}"
        if self.thread is None:
            return "未启动"
        return "运行中" if self.alive else "已停止"

    def line(self) -> str:
        label = self.config.spec.label if self.config.spec else self.config.type
        return f"  {self.config.name:<16} {label:<18} {self.state}"


def instantiate(cfg: ChannelConfig, *, on_message, workspace_dir: Optional[Path],
                logger: Any = None) -> Channel:
    """按配置里的 `target` 字符串真去 import 并实例化频道。

    延迟 import 的好处：`openthree channel list` 不因为某个频道模块的 import
    失败而整条命令挂掉。
    """
    spec = spec_for(cfg.type)
    if spec is None:
        raise ChannelManagerError(f"频道 {cfg.name!r} 的类型 {cfg.type!r} 不受支持")
    module_name, _, class_name = spec.target.partition(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type) or not issubclass(cls, Channel):
        raise ChannelManagerError(f"{spec.target} 不是 Channel 子类")
    return cls(cfg.name, dict(cfg.options), on_message=on_message,
               logger=logger, workspace_dir=workspace_dir)


class ChannelManager:
    """一组频道 + 一个桥。"""

    def __init__(self, config: ChannelsConfig, *, bridge: Any,
                 workspace_dir: Optional[Path] = None,
                 echo: bool = True, only: Optional[List[str]] = None) -> None:
        self.config = config
        self.bridge = bridge
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        self.echo = echo
        self.only = [n for n in (only or []) if n]
        self.statuses: List[Status] = []
        self.skipped: List[str] = []

    # --- 装配 ---------------------------------------------------------
    def build(self) -> "ChannelManager":
        """按配置实例化频道；缺字段 / 未知类型 / 导入失败 → 进 `skipped`。"""
        self.statuses = []
        self.skipped = []
        for cfg in self.config.channels:
            if self.only and cfg.name not in self.only:
                continue
            if not cfg.enabled:
                self.skipped.append(f"{cfg.name}：已停用（enabled: false）")
                continue
            missing = cfg.missing()
            if missing:
                self.skipped.append(f"{cfg.name}：缺 {', '.join(missing)}")
                continue
            channel = instantiate(
                cfg,
                on_message=self._make_handler(cfg),
                workspace_dir=self.workspace_dir,
            )
            self.statuses.append(Status(config=cfg, channel=channel))
        return self

    def _make_handler(self, cfg: ChannelConfig):
        """频道每收到一条消息，就排进桥的队列（立即返回，不阻塞收消息线程）。"""
        def handler(msg) -> None:
            channel = next((s.channel for s in self.statuses if s.config.name == cfg.name), None)
            if channel is None:
                return
            self.bridge.submit(msg, channel.send_text)
        return handler

    # --- 启停 ---------------------------------------------------------
    def start(self) -> int:
        """开跑；返回真正启动的频道数。"""
        if not self.statuses:
            raise ChannelManagerError(self.skip_summary())
        for st in self.statuses:
            thread = threading.Thread(target=self._run_one, args=(st,),
                                      name=f"channel-{st.config.name}", daemon=True)
            st.thread = thread
            st.started_at = time.time()
            thread.start()
        if hasattr(self.bridge, "start"):
            self.bridge.start()
        return len(self.statuses)

    def _run_one(self, st: Status) -> None:
        try:
            st.channel.run()                     # 阻塞，直到 stop() 或不可恢复的错误
        except Exception as exc:  # noqa: BLE001 - 一个频道炸掉不该带走别的频道
            st.error = str(exc) or type(exc).__name__
            print(f"[频道:{st.config.name}] 已停止：{st.error}", flush=True)

    def stop(self) -> None:
        for st in self.statuses:
            if st.channel is not None:
                try:
                    st.channel.stop()
                except Exception:  # noqa: BLE001
                    pass
        for st in self.statuses:
            if st.thread is not None:
                st.thread.join(timeout=5.0)
        if hasattr(self.bridge, "stop"):
            try:
                self.bridge.stop()
            except Exception:  # noqa: BLE001
                pass

    def wait(self, *, poll: float = 0.3) -> str:
        """阻塞到所有频道线程结束；返回第一个出错的说明（没有则空串）。"""
        while True:
            if not any(st.alive for st in self.statuses):
                break
            time.sleep(poll)
        return next((st.error for st in self.statuses if st.error), "")

    # --- 观测 ---------------------------------------------------------
    def skip_summary(self) -> str:
        if not self.skipped:
            return "没有可启动的频道"
        return "没有可启动的频道：\n" + "\n".join(f"  - {s}" for s in self.skipped)

    def summary(self) -> str:
        lines = [f"频道 {len(self.statuses)} 个（配置 {self.config.path}）"]
        lines.extend(st.line() for st in self.statuses)
        if self.skipped:
            lines.append("未启动：")
            lines.extend(f"  - {s}" for s in self.skipped)
        return "\n".join(lines)

    @property
    def active(self) -> List[str]:
        return [st.config.name for st in self.statuses]

    # --- 便捷构造 -----------------------------------------------------
    @classmethod
    def from_workspace(cls, ws: Any, bridge: Any, *, only: Optional[List[str]] = None,
                       echo: bool = True) -> "ChannelManager":
        """读工作区里的 `channels.yaml` 并装配（不启动）。"""
        cfg = ChannelsConfig.load(config_path(ws.dir))
        mgr = cls(cfg, bridge=bridge, workspace_dir=ws.dir, echo=echo, only=only)
        mgr.build()
        return mgr


def load_manager(ws: Any, bridge: Any, *, only: Optional[List[str]] = None,
                 echo: bool = True) -> ChannelManager:
    """`ChannelManager.from_workspace` 的函数式别名（cli 里读起来短一点）。"""
    return ChannelManager.from_workspace(ws, bridge, only=only, echo=echo)


def has_configured_channels(ws: Any) -> bool:
    """工作区里是否至少有一个「启用且就绪」的频道 —— 决定要不要自动起。"""
    try:
        cfg = ChannelsConfig.load(config_path(ws.dir))
    except Exception:  # noqa: BLE001 - 配置坏了由附带启动路径去报，这里只回答「有没有」
        return False
    return bool(cfg.ready())


def describe_config(ws: Any) -> str:  # pragma: no cover - 仅调试用
    cfg = ChannelsConfig.load(config_path(ws.dir))
    return "\n".join(c.describe() for c in cfg.channels) or "（还没有频道）"


__all__ = [
    "ChannelManager", "ChannelManagerError", "Status", "instantiate",
    "load_manager", "has_configured_channels", "describe_config",
]
