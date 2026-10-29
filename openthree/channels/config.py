"""频道配置：`.openthree/channels.yaml` 的读、写、校验与「类型规格」。

一份文件描述**多个**频道：每个频道一条记录（`name` / `type` / `enabled` / `options`）。
`name` 是频道实例的名字（同一份文件里唯一），也是日志里「这条消息是谁收的」。

这里只做**声明**，不 import 任何具体频道实现：类型规格里的 `target` 写成
`"模块:类"` 字符串，由 `manager` 在真正启动时才解析。于是
`openthree channel list` 不会因为某个渠道的可选依赖缺失而炸掉。

配置读得**宽容**、写得**原子**：手改坏的一条记录不会让其余频道一起消失，
坏在哪会作为 warning 回传（而不是抛异常打断启动）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..workspace import atomic_write

CONFIG_FILENAME = "channels.yaml"
SCHEMA = 1


class ChannelConfigError(RuntimeError):
    """配置文件本身坏到读不下去（YAML 语法错 / 顶层不是映射）。"""


@dataclass(frozen=True)
class Field:
    """一个配置项：既是向导里问的那一行，也是 `channel list` 的校验依据。"""

    key: str
    label: str
    default: str = ""
    secret: bool = False
    required: bool = False
    hint: str = ""


@dataclass(frozen=True)
class ChannelSpec:
    """一种渠道类型：人类可读的名字 + 要问哪些配置项 + 实现类在哪。"""

    type: str
    label: str
    summary: str
    target: str
    fields: Tuple[Field, ...] = ()

    def field(self, key: str) -> Optional[Field]:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def defaults(self) -> Dict[str, Any]:
        return {f.key: f.default for f in self.fields}

    def required_keys(self) -> List[str]:
        return [f.key for f in self.fields if f.required]


_SPECS: Dict[str, ChannelSpec] = {
    "wechat": ChannelSpec(
        type="wechat",
        label="微信（iLink Bot）",
        summary="扫码登录后长轮询收消息、HTTP 发消息；纯 HTTP，无需公网地址",
        target="openthree.channels.wechat:WeChatChannel",
        fields=(
            Field("bot_token", "bot_token",
                  hint="留空则首次启动打印二维码，扫码登录后自动写入 token 文件"),
            Field("base_url", "base_url", default="https://ilinkai.weixin.qq.com",
                  hint="iLink API 根地址；登录成功后可能被服务端指定的地址覆盖"),
            Field("token_file", "token_file",
                  hint="token / 最近会话游标的落盘位置（默认 .openthree/channels/<名字>.json）"),
            Field("allow_from", "allow_from",
                  hint="允许的发送者 id，逗号分隔；留空 = 不限制"),
        ),
    ),
    "dingtalk": ChannelSpec(
        type="dingtalk",
        label="钉钉（机器人 Stream 模式）",
        summary="自建应用机器人 + Stream 模式收消息，用 sessionWebhook 回复；无需公网地址",
        target="openthree.channels.dingtalk:DingTalkChannel",
        fields=(
            Field("client_id", "client_id", required=True,
                  hint="钉钉开放平台「应用信息」里的 AppKey / Client ID"),
            Field("client_secret", "client_secret", required=True, secret=True,
                  hint="钉钉开放平台「应用信息」里的 AppSecret / Client Secret"),
            Field("require_mention", "群聊需 @", default="true",
                  hint="群聊里只回应 @ 了机器人的消息（true/false）"),
            Field("allow_from", "allow_from",
                  hint="允许的发送者 staffId，逗号分隔；留空 = 不限制"),
        ),
    ),
}

# 人类嘴里的写法与配置里的规范名不一定一致：`--type 钉钉` / `dingding` 都该认。
_ALIASES: Dict[str, str] = {
    "dingding": "dingtalk",
    "dd": "dingtalk",
    "钉钉": "dingtalk",
    "wx": "wechat",
    "weixin": "wechat",
    "weixinbot": "wechat",
    "微信": "wechat",
}


def normalize_type(raw: str) -> str:
    """把用户输入的渠道类型收敛成规范名；认不出就原样小写返回（交给 manager 报未知）。"""
    key = str(raw or "").strip().lower()
    key = _ALIASES.get(key, key)
    return _ALIASES.get(key, key)


def known_types() -> List[str]:
    return sorted(_SPECS)


def spec_for(type_name: str) -> Optional[ChannelSpec]:
    return _SPECS.get(normalize_type(type_name))


def specs() -> List[ChannelSpec]:
    return [_SPECS[k] for k in known_types()]


@dataclass
class ChannelConfig:
    """一个频道实例的配置。"""

    name: str
    type: str
    enabled: bool = True
    options: Dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> Optional[ChannelSpec]:
        return spec_for(self.type)

    def missing(self) -> List[str]:
        """必填却没值的项（未知类型返回空表 —— 那由 manager 报「未知类型」）。"""
        spec = self.spec
        if spec is None:
            return []
        out: List[str] = []
        for key in spec.required_keys():
            if not str(self.options.get(key) or "").strip():
                out.append(key)
        return out

    def unknown_options(self) -> List[str]:
        spec = self.spec
        if spec is None:
            return []
        known = {f.key for f in spec.fields}
        return sorted(k for k in self.options if k not in known)

    def is_ready(self) -> bool:
        return self.spec is not None and not self.missing()

    def option(self, key: str, default: str = "") -> str:
        value = self.options.get(key, "")
        if value is None or str(value).strip() == "":
            spec = self.spec
            field_ = spec.field(key) if spec else None
            return field_.default if field_ else default
        return str(value)

    def as_bool(self, key: str, default: bool = False) -> bool:
        raw = str(self.options.get(key, "")).strip().lower()
        if not raw:
            spec = self.spec
            field_ = spec.field(key) if spec else None
            raw = str(field_.default).strip().lower() if field_ else ""
        if not raw:
            return default
        return raw in ("1", "true", "yes", "y", "on", "是", "开")

    def redacted(self) -> Dict[str, Any]:
        """给人看的选项：secret 字段打码（日志 / `channel show` 用）。"""
        spec = self.spec
        out: Dict[str, Any] = {}
        for key, value in self.options.items():
            field_ = spec.field(key) if spec else None
            text = "" if value is None else str(value)
            if field_ is not None and field_.secret and text:
                out[key] = text[:4] + "…" + text[-2:] if len(text) > 8 else "***"
            else:
                out[key] = text
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "enabled": bool(self.enabled),
            "options": dict(self.options),
        }

    def describe(self) -> str:
        spec = self.spec
        label = spec.label if spec else f"未知类型 {self.type!r}"
        state = "启用" if self.enabled else "停用"
        falta = self.missing()
        if spec is None:
            health = "✗ 未知类型（看 `openthree channel list`）"
        elif falta:
            health = "✗ 缺 " + ", ".join(falta)
        else:
            health = "✓ 就绪"
        return f"{self.name:<16} {label:<18} {state}  {health}"

    @classmethod
    def from_dict(cls, raw: Any, index: int = 0) -> "ChannelConfig":
        if not isinstance(raw, dict):
            raise ValueError("每条频道配置必须是一个映射")
        name = str(raw.get("name") or "").strip()
        type_name = normalize_type(str(raw.get("type") or ""))
        options = raw.get("options")
        if options is None:
            # 允许把选项直接写在条目上（比 options: 少一层缩进，手改更省事）
            options = {k: v for k, v in raw.items() if k not in ("name", "type", "enabled")}
        if not isinstance(options, dict):
            raise ValueError("options 必须是映射")
        if not name:
            name = type_name or f"channel-{index + 1}"
        return cls(name=name, type=type_name,
                   enabled=bool(raw.get("enabled", True)),
                   options={str(k): ("" if v is None else str(v)) for k, v in options.items()})


@dataclass
class ChannelsConfig:
    """一整份 `channels.yaml`。"""

    path: Path
    channels: List[ChannelConfig] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # --- 查询 ---------------------------------------------------------
    def get(self, name: str) -> Optional[ChannelConfig]:
        for c in self.channels:
            if c.name == name:
                return c
        return None

    def enabled(self) -> List[ChannelConfig]:
        return [c for c in self.channels if c.enabled]

    def ready(self) -> List[ChannelConfig]:
        return [c for c in self.channels if c.enabled and c.is_ready()]

    def names(self) -> List[str]:
        return [c.name for c in self.channels]

    def unique_name(self, base: str) -> str:
        base = (base or "channel").strip() or "channel"
        if base not in self.names():
            return base
        i = 2
        while f"{base}-{i}" in self.names():
            i += 1
        return f"{base}-{i}"

    # --- 修改 ---------------------------------------------------------
    def add(self, cfg: ChannelConfig, *, replace: bool = False) -> None:
        old = self.get(cfg.name)
        if old is not None:
            if not replace:
                raise ValueError(f"频道名 {cfg.name!r} 已存在")
            self.channels[self.channels.index(old)] = cfg
        else:
            self.channels.append(cfg)

    def remove(self, name: str) -> bool:
        cfg = self.get(name)
        if cfg is None:
            return False
        self.channels.remove(cfg)
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        cfg = self.get(name)
        if cfg is None:
            return False
        cfg.enabled = bool(enabled)
        return True

    # --- 落盘 ---------------------------------------------------------
    def to_doc(self) -> Dict[str, Any]:
        return {"schema": SCHEMA,
                "channels": [c.to_dict() for c in self.channels]}

    def to_yaml(self) -> str:
        header = (
            "# openthree 频道配置 —— 一份文件可以描述多个频道。\n"
            "# 由 `openthree channel add` 引导生成，也可以手改；改完下次启动即生效。\n"
            "# 启动时自动加载：`openthree -d` 只等频道输入，`openthree -i` 与频道并存。\n"
        )
        return header + yaml.safe_dump(self.to_doc(), allow_unicode=True,
                                       sort_keys=False, default_flow_style=False)

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, self.to_yaml())
        return self.path

    @classmethod
    def load(cls, path: Path) -> "ChannelsConfig":
        path = Path(path)
        doc = cls(path=path, channels=[], warnings=[])
        if not path.exists():
            return doc
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ChannelConfigError(f"{path} 不是合法 YAML：{exc}") from exc
        if not isinstance(raw, dict):
            raise ChannelConfigError(f"{path} 顶层必须是映射（含 channels 列表）")
        entries = raw.get("channels") or []
        if not isinstance(entries, list):
            raise ChannelConfigError(f"{path} 的 channels 必须是列表")
        seen: set[str] = set()
        for i, entry in enumerate(entries):
            try:
                cfg = ChannelConfig.from_dict(entry, index=i)
            except ValueError as exc:
                doc.warnings.append(f"第 {i + 1} 条频道配置已忽略：{exc}")
                continue
            if cfg.name in seen:
                doc.warnings.append(f"频道名 {cfg.name!r} 重复，第 {i + 1} 条已忽略")
                continue
            seen.add(cfg.name)
            if cfg.spec is None:
                doc.warnings.append(
                    f"频道 {cfg.name!r} 的类型 {cfg.type!r} 不受支持（已知："
                    + ", ".join(known_types()) + "）")
            doc.channels.append(cfg)
        return doc


def config_path(workspace_dir: Path) -> Path:
    """配置文件的唯一位置：工作区根下的 `channels.yaml`。"""
    return Path(workspace_dir) / CONFIG_FILENAME
