"""频道层：把 openthree 接到聊天软件上（微信 / 钉钉），内核一行都不改。

分工：
  * `base.Channel`     —— 一个渠道要实现的三个方法（收 / 发 / 停）
  * `config`           —— `channels.yaml`：多频道、引导式配置、原子落盘
  * `bridge.AgentBridge` —— 把频道消息喂进 `Runtime.run()`，与 `-i` 里敲字同一条路
  * `manager`          —— 多频道线程化启停、错误隔离
  * `wechat` / `dingtalk` —— 两个具体渠道（纯标准库，无额外依赖）

典型用法（CLI 已经把它接好了，这里是给嵌入者的）：

    from openthree.runtime import Runtime
    from openthree.channels import AgentBridge, ChannelManager

    rt = Runtime(project_dir); rt.begin_session()
    bridge = AgentBridge(rt)
    mgr = ChannelManager.from_workspace(rt.ws, bridge)
    mgr.start()
    mgr.wait()
    mgr.stop()
"""
from __future__ import annotations

from .base import Channel, InboundMessage
from .bridge import AgentBridge, compose_input
from .config import (ChannelConfig, ChannelConfigError, ChannelSpec, ChannelsConfig,
                     Field, config_path, known_types, normalize_type, spec_for, specs)
from .manager import (ChannelManager, ChannelManagerError, Status, has_configured_channels,
                      instantiate, load_manager)

__all__ = [
    "Channel", "InboundMessage", "AgentBridge", "compose_input",
    "ChannelConfig", "ChannelsConfig", "ChannelSpec", "Field", "ChannelConfigError",
    "config_path", "known_types", "normalize_type", "spec_for", "specs",
    "ChannelManager", "ChannelManagerError", "Status", "instantiate",
    "load_manager", "has_configured_channels",
]
