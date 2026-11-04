"""LLM 客户端：极薄的一层。

不引入框架，也不引入第三方 SDK：OpenAI 协议的一次调用无非「POST 一个 JSON、
读回一个 JSON」，由本文件的 `post_chat()` 用标准库完成，于是遵循该协议的一切
服务（DeepSeek / GLM / Kimi / vLLM / Ollama / ...）都能直接接。

不依赖 openai SDK 是有代价换来的：SDK 会把 pydantic 拖进来，而 pydantic 的
`pydantic-core` 是 Rust 扩展 —— 在没有编译器的环境（iOS 的 a-Shell 等）里装不上。
而这个客户端需要的全部能力就是一次 POST，没必要为此付出那个代价。

配置分层（后者覆盖前者）：
    1. 环境变量                OPENAI_API_KEY / OPENAI_BASE_URL / OPENTHREE_MODEL
    2. 用户级                  ~/.openthree/config.yaml
    3. 项目级                  ./.openthree/config.yaml

只要有任何一层给了 base_url + api_key + model 就能跑。
"""
from __future__ import annotations

import base64
import gzip
import http.client
import json
import os
import random
import re
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .multimodal import sanitize_for_log


DEFAULT_MODEL = "deepseek-v4.1-flash"

# 模型上下文窗口的默认值（单位 token）。
# 默认按 1M 上下文对待 —— 上下文预算由此推导，见 agent.resolve_context_budget。
# 换更大/更小的窗口时，请在 config.yaml 里写 context_window_tokens（如 "128k" / "4m"）。
DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000

# token ↔ 字符 的粗略换算：1 token ≈ 2 字符，够用且不依赖具体 tokenizer。
CHARS_PER_TOKEN = 2


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = DEFAULT_MODEL
    max_tokens: int = 32768
    # 模型上下文窗口（token）。上下文预算的默认值的来源。
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    temperature: float = 0.3
    # 推理强度（OpenAI 风格 `reasoning_effort`：minimal / low / medium / high）。
    # None = 请求里**不带这个键** —— 不发 null，因为严格校验的 provider 会因此 400。
    # 写在这一层而不是只丢给 `extra`：`extra` 是给「内核不认识的键」的透传口，
    # 而这个是内核认识的旋钮，要有字段、有来源、能被 --reasoning-effort 按调用覆盖。
    reasoning_effort: Optional[str] = None
    # reasoning 回传策略：`auto` / `always` / `never`，语义见
    # resolve_reasoning_placeholder()。它管的是**回传**（assistant 消息上的
    # 占位字段），与上面的 reasoning_effort（请求参数）是两件事。
    reasoning_content_replay: str = "auto"
    timeout: float = 600.0
    # connect 阶段单独设一个短超时：read=600s 是合理的（等模型生成），
    # 但「连不上主机」跟「模型想得久」是两回事——不分开就会把 connect 也
    # 抬到 600s，一次重试要干等 10 分钟。见 post_chat()。
    connect_timeout: float = 5.0
    # 重试只在 openthree 这一层发生（本文件就是全部传输层，下面没有别的东西
    # 会偷偷再重试一次），所以它给出的是全量：retries=3 ⇒ 最多 4 次真实 HTTP 调用。
    retries: int = 3
    extra: Dict[str, Any] = field(default_factory=dict)
    source: str = "env"

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


def _clean_effort(value: Any) -> Optional[str]:
    """把 reasoning_effort 归一成小写字符串；空 / None 一律当「不设置」。

    **不校验取值**：`reasoning_effort` 不是 OpenAI 独有的词表，各家 provider
    有自己的档位（`minimal` / `low` / `medium` / `high` / `none` / `xhigh`…），
    这里硬编码白名单只会把新档位挡在门外。真正合不合法由 provider 说了算，
    错了会得到一句明确的 400 —— 那比内核替它猜要诚实。

    但**大小写要归一**：OpenAI 一侧的枚举全是小写，`HIGH` 会被当成非法取值
    400 掉，而人写配置时大小写是随手的事。归一不改变词表，只抹掉这个坑。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    return text or None


# ---------------------------------------------------------------- reasoning 回传
# provider 用哪个字段名吐 reasoning，就用哪个名字回放 —— 这三个都是 OpenAI 兼容
# 生态里真实存在的写法（DeepSeek / GLM / Kimi 用 `reasoning_content`，部分网关用
# `reasoning` 或 `reasoning_text`）。顺序即优先级：取第一个非空的。
# **不硬编码单一字段名**：字段名是端点的属性，猜错等于整段 reasoning 被丢掉。
REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")

# 「每条 assistant 消息都必须带这个字段」时用的占位字段。值可以是空串 ——
# 端点校验的是**键存在**，不是内容非空。
REASONING_PLACEHOLDER_FIELD = "reasoning_content"

_REASONING_REPLAY_ALWAYS = ("always", "on", "yes", "true", "1")
_REASONING_REPLAY_NEVER = ("never", "off", "no", "false", "0")


def _clean_replay_mode(value: Any) -> str:
    """把 `reasoning_content_replay` 归一成 `auto` / `always` / `never`。

    缺省与空串 → `auto`。布尔与 0 **保留**（`false` / `0` 就是 `never`）——
    一律按「假值 → 回落 auto」处理会把「我要关掉它」悄悄翻成「自动」，
    而那正是最难查的那种配置事故。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "auto"
    return str(value).strip()


def extract_reasoning(message: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """找出这条 message 里的 reasoning，返回 `(字段名, 文本)`；没有则 None。

    按 `REASONING_FIELDS` 的顺序取**第一个非空**的。空串与非字符串一律跳过 ——
    端点偶尔会吐 `reasoning_content: ""`，那不算「有 reasoning」。
    """
    for field in REASONING_FIELDS:
        value = (message or {}).get(field)
        if isinstance(value, str) and value:
            return field, value
    return None


def _looks_deepseek(base_url: str, model: str) -> bool:
    """端点像不像 DeepSeek 家族（pi-ai 的探测规则，放宽一处）。

    pi-ai 只认 `baseUrl.includes("deepseek.com")`。openthree 的 base_url 却常是
    自建代理（`https://gw.internal/deepseek/v1`），域名里根本没有 deepseek.com，
    所以这里改判「base_url 里出现 deepseek，或 model 名以 deepseek 开头」，
    并始终保留显式覆盖（`always` / `never`）—— 私有网关的 URL 说明不了什么，
    最终得由人说了算。
    """
    return ("deepseek" in (base_url or "").lower()
            or (model or "").lower().startswith("deepseek"))


def is_always_replay(mode: Any) -> bool:
    """`reasoning_content_replay` 是否落在「无条件补占位」那一档（含别名）。

    单独暴露出来，是为了让「谁算 always」只有一份定义：`resolve_reasoning_placeholder`
    与 Runtime 的身份卡都判这一处，日后加别名不会改一处漂一处。
    """
    return str(mode if mode is not None else "").strip().lower() in _REASONING_REPLAY_ALWAYS


def is_never_replay(mode: Any) -> bool:
    """是否落在「永不补占位」那一档（含别名）。"""
    return str(mode if mode is not None else "").strip().lower() in _REASONING_REPLAY_NEVER


def is_known_replay_mode(mode: Any) -> bool:
    """这个取值是不是认识的（`auto` / `always` / `never` 及其别名，空 = auto）。

    不认识的取值不会报错 —— `resolve_reasoning_placeholder` 按 `auto` 处理。
    这里只是让调用方能如实告诉人「你写的那个词没用上」。
    """
    normalized = str(mode if mode is not None else "").strip().lower()
    return (not normalized) or normalized == "auto" \
        or is_always_replay(mode) or is_never_replay(mode)


def resolve_reasoning_placeholder(mode: Any, *, base_url: str = "", model: str = "",
                                  reasoning_active: bool = False) -> Optional[str]:
    """要不要在每条 assistant 消息上补一个 reasoning 占位字段？返回字段名或 None。

    某些 OpenAI 兼容端点在推理模式下要求**每一条** assistant 消息都带
    `reasoning_content`，否则下一次请求就会被 400 拒掉
    （"The reasoning_content in the thinking mode must be passed back to the API"）。
    难点在于：模型并不是每轮都吐 reasoning —— 没有 reasoning 的那一轮若把字段
    一起省掉，下一请求就挂了。所以「没内容」时要发一个空串占位。

    三档（config.yaml 的 `llm.reasoning_content_replay`）：

    * `auto`（默认）：端点像 DeepSeek 家族 **且** 推理确实开着时才补；
    * `always`：无条件补 —— 逃生口，给「代理 URL 看不出厂商 + 模型默认就在想
      却不吐 delta」这种 `auto` 判不出来的组合；
    * `never`：永不补。

    **三档管的都只是「占位」**：已经收到的 reasoning 仍然照 provider 的字段名
    原样回放（见 `extract_reasoning` 与 `Agent._attach_reasoning`）—— `never`
    的名字容易被读成「完全不回传」，它不是那个意思。

    「推理确实开着」由调用方判断（`reasoning_active`）：openthree 没有模型注册表，
    无法像 pi-ai 那样查 `model.reasoning`，只能从「人类设了推理档位（含
    `config.extra` 透传）」或「本场已经见过 reasoning」这些真实信号推断。
    不认识的取值按 `auto` 处理。
    """
    if is_never_replay(mode):
        return None
    if is_always_replay(mode):
        return REASONING_PLACEHOLDER_FIELD
    if not reasoning_active or not _looks_deepseek(base_url, model):
        return None
    return REASONING_PLACEHOLDER_FIELD


def parse_token_count(value: Any) -> Optional[int]:
    """把 "1M" / "128k" / "1,000,000" / 1000000 这类写法解析成整数 token 数。

    解析不出来时返回 None（由调用方决定回退到默认值）。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "").replace("_", "").lower()
    if not text:
        return None
    scale = 1
    for suffix, mult in (("k", 1_000), ("m", 1_000_000), ("g", 1_000_000_000)):
        if text.endswith(suffix):
            scale = mult
            text = text[:-1]
            break
    try:
        return int(float(text) * scale)
    except ValueError:
        return None


def _load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _pick_llm_section(doc: Dict[str, Any]) -> Dict[str, Any]:
    """取出配置文件里的 `llm:` 节（缺失或类型不对时返回空 dict）。"""
    sec = doc.get("llm")
    return sec if isinstance(sec, dict) else {}


def load_config(project_dir: Optional[Path] = None, model_override: Optional[str] = None) -> LLMConfig:
    layers: List[Dict[str, Any]] = []

    # 1. 环境变量
    env = {
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": os.getenv("OPENAI_BASE_URL", ""),
        "model": os.getenv("OPENTHREE_MODEL", "") or os.getenv("OPENAI_MODEL", ""),
    }
    env = {k: v for k, v in env.items() if v}
    if env:
        layers.append(env)

    # 2. 用户级
    layers.append(_pick_llm_section(_load_yaml(Path.home() / ".openthree" / "config.yaml")))

    # 3. 项目级
    if project_dir is not None:
        proj = Path(project_dir) / ".openthree" / "config.yaml"
        layers.append(_pick_llm_section(_load_yaml(proj)))
        # 也允许放在工作区根
        layers.append(_pick_llm_section(_load_yaml(Path(project_dir) / "openthree.yaml")))

    merged: Dict[str, Any] = {}
    for layer in layers:
        for k, v in layer.items():
            if v in (None, ""):
                continue
            merged[k] = v

    # 上下文窗口：支持 "1M" / "128k" / 1000000 等写法；缺省按 DEFAULT_CONTEXT_WINDOW_TOKENS。
    window = (parse_token_count(merged.get("context_window_tokens"))
              or parse_token_count(merged.get("context_window"))
              or parse_token_count(merged.get("context_window_size"))
              or DEFAULT_CONTEXT_WINDOW_TOKENS)

    cfg = LLMConfig(
        api_key=str(merged.get("api_key", "") or ""),
        base_url=str(merged.get("base_url", "") or ""),
        model=str(model_override or merged.get("model") or DEFAULT_MODEL),
        max_tokens=int(parse_token_count(merged.get("max_tokens")) or 32768),
        context_window_tokens=int(window),
        temperature=float(merged.get("temperature", 0.3) or 0.3),
        reasoning_effort=_clean_effort(merged.get("reasoning_effort")),
        reasoning_content_replay=_clean_replay_mode(
            merged.get("reasoning_content_replay",
                       merged.get("requires_reasoning_content"))),
        timeout=float(merged.get("timeout", 600) or 600),
        connect_timeout=float(merged.get("connect_timeout", 5) or 5),
        retries=int(merged.get("retries", 3) or 0),
        extra=dict(merged.get("extra") or {}),
    )
    return cfg


# ------------------------------------------------------------------ 传输层
# OpenAI 协议的一次调用 = POST 一个 JSON、读回一个 JSON。这里用标准库自己发，
# 只提两个要求：connect 与 read 的超时要能分开，且失败时要带得出状态码
# —— 上层 `_retryable()` 靠它区分「抖动，重试」与「4xx，别浪费次数」。
USER_AGENT = "openthree/1.0 (+https://github.com/YY-Yifei/openthree)"
_CHAT_PATH = "/chat/completions"

# 可选：OpenSSL 找不到系统 CA 证书时（iOS / 精简容器里很常见）借 certifi 那份。
# 它是纯 Python 包、不需要编译，所以只做「装了就用」，不列为主依赖。
try:  # pragma: no cover - 取决于环境里有没有 certifi
    import certifi as _certifi

    _CA_FILE: Optional[str] = _certifi.where()
except Exception:  # noqa: BLE001
    _CA_FILE = None


# 建好的 SSL 上下文缓存。SSLContext 一旦创建就可以安全地被多处复用来建连接，
# 而 `create_default_context()` 每次都要读一遍整份 CA bundle —— 一次重试就要重复
# 几次，在手机上尤其不值。
_SSL_CONTEXT: Optional["ssl.SSLContext"] = None


def _ssl_context() -> "ssl.SSLContext":
    """带 CA 的 SSL 上下文：优先 certifi（iOS 常见），退回系统默认。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    ctx = None
    if _CA_FILE:
        try:
            ctx = ssl.create_default_context(cafile=_CA_FILE)
        except Exception:  # noqa: BLE001  # pragma: no cover
            ctx = None
    _SSL_CONTEXT = ctx or ssl.create_default_context()
    return _SSL_CONTEXT


class TransportError(RuntimeError):
    """一次 HTTP 调用失败。

    `status` = 0 表示**根本没走到 HTTP**（DNS / 连接 / 超时），这类失败值得重试；
    非 0 就是服务端真回了那个状态码，4xx 是调用方的问题，重试只是把它重复一遍。
    """

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _body_text(raw: bytes, encoding: str) -> str:
    """按 content-encoding 解压正文（只处理 gzip；解不开就按原样读）。"""
    if encoding == "gzip" and raw:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode("utf-8", "replace")


def _error_brief(text: str) -> str:
    """把错误正文压成一句：优先 OpenAI 风格的 error.message，否则截前 300 字。"""
    fallback = text.strip()[:300] or "(空响应)"
    try:
        doc = json.loads(text)
    except Exception:  # noqa: BLE001
        return fallback
    if not isinstance(doc, dict):
        return fallback
    err = doc.get("error")
    if isinstance(err, dict):
        for key in ("message", "type", "code"):
            val = err.get(key)
            if val:
                return str(val)[:300]
    if isinstance(err, str) and err:
        return err[:300]
    for key in ("message", "msg", "detail"):
        val = doc.get(key)
        if isinstance(val, str) and val:
            return val[:300]
    return fallback


def _proxy_for(scheme: str, host: str) -> Optional[Tuple[str, int, Optional[str]]]:
    """这个目标该走哪个代理？返回 `(主机, 端口, 认证头)`，直连则返回 None。

    读的是 `urllib.request.getproxies()` 那一套来源（环境变量 + 各平台系统设置），
    并在命中 `no_proxy` 时放行直连 —— 那正是 httpx 的 `trust_env=True` 做的事。
    换掉 SDK 不该顺带把代理能力弄丢：在有代理的机器上，那等于「从能连变成连不上」，
    而且用户看不出为什么。
    """
    try:
        if urllib.request.proxy_bypass(host):
            return None
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001  # pragma: no cover - 平台差异
        return None
    raw = proxies.get(scheme) or proxies.get("all")
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    if not parts.hostname:
        return None
    auth = None
    if parts.username:
        cred = f"{parts.username}:{parts.password or ''}"
        auth = "Basic " + base64.b64encode(cred.encode("utf-8")).decode("ascii")
    port = parts.port or (443 if parts.scheme == "https" else 8080)
    return (parts.hostname, port, auth)


def post_chat(base_url: str, api_key: str, payload: Dict[str, Any], *,
              timeout: float = 600.0,
              connect_timeout: float = 5.0) -> Dict[str, Any]:
    """向 OpenAI 兼容端点发一次 `/chat/completions`，返回解析后的 JSON。

    为什么用 `http.client` 而不是 `urllib.request.urlopen`：后者只有一个
    timeout，会同时管 connect 与 read。这里先把 TCP（+TLS）连上，这一步受
    `connect_timeout` 约束；连上之后把 socket 换成 `timeout` —— 于是「连不上
    主机」5 秒就失败，「模型想得久」仍能等满。这正是「一次重试不该干等十分钟」
    所依赖的那条分离。

    代理（`HTTPS_PROXY` / `HTTP_PROXY` / `no_proxy`）同样生效：HTTPS 目标走
    CONNECT 隧道，HTTP 目标走绝对 URI —— 都在 `http.client` 里，所以上面那条
    超时分离在有代理时也成立。

    失败一律抛 `TransportError`（带 status 与正文）：正文里通常有 provider 的
    说法，光看状态码没法排查。
    """
    url = base_url.rstrip("/") + _CHAT_PATH
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise TransportError(
            f"base_url 协议无法识别：{scheme or '(空)'}（只认 http / https）：{base_url}")
    host = parts.hostname or ""
    if not host:
        raise TransportError(f"base_url 里没有主机名：{base_url}")
    port = parts.port or (443 if scheme == "https" else 80)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
        "accept": "application/json",
        "accept-encoding": "gzip",
        "user-agent": USER_AGENT,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    proxy = _proxy_for(scheme, host)
    request_path = path
    if scheme == "https":
        p_host, p_port, p_auth = proxy if proxy else (host, port, None)
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            p_host, p_port, timeout=connect_timeout, context=_ssl_context())
        if proxy:
            # CONNECT 隧道：先连代理，再让它把字节转发给目标主机。
            conn.set_tunnel(host, port,
                            headers={"Proxy-Authorization": p_auth} if p_auth else None)
    else:
        p_host, p_port, p_auth = proxy if proxy else (host, port, None)
        conn = http.client.HTTPConnection(p_host, p_port, timeout=connect_timeout)
        if proxy:
            # 明文目标经代理：请求行要用绝对 URI，认证头挂在请求上。
            request_path = urllib.parse.urlunsplit(
                ("http", f"{host}:{port}", path, "", ""))
            if p_auth:
                headers["proxy-authorization"] = p_auth

    try:
        conn.connect()  # 建连（+ CONNECT + TLS 握手）：只受 connect_timeout 管
        if conn.sock is not None:
            conn.sock.settimeout(timeout)  # 连上之后：等模型的那个 timeout
        conn.request("POST", request_path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
        encoding = (resp.getheader("content-encoding") or "").lower()
    except TimeoutError as exc:
        raise TransportError(
            f"超时（connect {connect_timeout:g}s / read {timeout:g}s）：{exc}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()

    text = _body_text(raw, encoding)
    if not (200 <= status < 300):
        raise TransportError(f"HTTP {status}: {_error_brief(text)}",
                             status=status, body=text)
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransportError(f"响应不是 JSON：{text[:300]}", status=status, body=text) from exc


class LLM:
    """一次调用的薄封装。返回 assistant message。"""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def chat(self, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             tool_choice: str = "auto",
             max_tokens: Optional[int] = None,
             temperature: Optional[float] = None,
             reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
        """返回 {message, usage, elapsed_ms, raw_request, raw_response, attempts}。

        `attempts` 是**本次调用真正打到线上的次数**（含重试；>1 说明重试过且最终
        成功/放弃）。失败与成功两条路径都会带上它，供日志与进度显示用。

        `max_tokens` / `temperature` / `reasoning_effort` 可**按次覆盖**配置里的值
        —— 调用方（Agent）由此把「模型自管的运行边界」按调用传进来，而不必去改
        `self.config`。这很关键：Runtime 在 `-i` / `--auto` 下复用同一个 LLM，
        若靠原地改 config，这一轮写进去的边界会污染下一轮的「内核默认」。

        `reasoning_effort` 的优先级是**显式 > config 字段 > config.extra**：
        显式值必须能压过 `extra` 里那份（否则人类在命令行给的值会被配置文件
        里顺手写的透传值悄悄吃掉）。
        """
        eff_max_tokens = (self.config.max_tokens if max_tokens is None
                          else int(max_tokens))
        eff_temperature = (self.config.temperature if temperature is None
                           else float(temperature))
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": eff_max_tokens,
            "temperature": eff_temperature,
        }
        for k, v in (self.config.extra or {}).items():
            payload.setdefault(k, v)
        # 推理强度放在 extra **之后**：显式参数 > 配置字段 > extra 透传。
        # 三项都没有时一个键都不加 —— 不发 null，严格校验的 provider 会因此 400。
        eff_effort = _clean_effort(reasoning_effort) or _clean_effort(
            self.config.reasoning_effort)
        if eff_effort is not None:
            payload["reasoning_effort"] = eff_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        started = time.time()
        attempts = max(0, self.config.retries) + 1
        used = 0
        data: Dict[str, Any] = {}
        for attempt in range(attempts):
            used = attempt + 1
            try:
                data = post_chat(self.config.base_url, self.config.api_key, payload,
                                 timeout=self.config.timeout,
                                 connect_timeout=self.config.connect_timeout)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == attempts - 1 or not _retryable(exc):
                    elapsed = (time.time() - started) * 1000
                    return {
                        "message": {"role": "assistant",
                                    "content": f"[LLM 调用失败] {type(exc).__name__}: {exc}"},
                        "usage": {},
                        "elapsed_ms": elapsed,
                        "raw_request": _redact(payload),
                        "raw_response": {"error": f"{type(exc).__name__}: {exc}"},
                        "error": f"{type(exc).__name__}: {exc}",
                        "attempts": used,
                    }
                time.sleep(_backoff(attempt))
        elapsed = (time.time() - started) * 1000

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        finish_reason = choice.get("finish_reason")

        # 上下文被 max_tokens 截断：tool_calls 极可能是不完整的非法 JSON，
        # 直接交给模型续写会得到半截参数。这里如实反馈，让 agent 知道要重来。
        error = None
        error_kind = None
        if finish_reason == "length":
            error = (f"模型输出因 max_tokens={eff_max_tokens} 被截断"
                     f"（finish_reason=length）")
            error_kind = "truncated"

        return {
            "message": message,
            "usage": usage,
            "elapsed_ms": elapsed,
            "raw_request": _redact(payload),
            "raw_response": data,
            "error": error,
            "error_kind": error_kind,
            "finish_reason": finish_reason,
            "attempts": used,
        }


# 可重试的信号。用**词边界正则**而不是裸子串：裸 "500" 会把 "5000 tokens"
# 这类无关文本误判成 5xx，词边界能挡掉它。
_RETRYABLE_MARKERS = (
    "timeout", "timedout", "connection", "connect", "ratelimit", "rate_limit",
    "internalserver", "servererror", "serviceunavailable", "apiconnection",
    "temporarily",
)
# 值得重试的状态码：408 请求超时 / 409 冲突 / 429 限流 / 5xx 服务端。
_RETRYABLE_STATUS_SET = frozenset({408, 409, 429, 500, 502, 503, 504})
_RETRYABLE_STATUS = re.compile(
    r"(?<!\d)(408|409|429|500|502|503|504)(?!\d)")


def _retryable(exc: BaseException) -> bool:
    """网络抖动 / 限流 / 5xx 才重试；参数错误、鉴权失败等立刻返回。

    有状态码时**只看状态码**：正文里出现 "timeout" 的 400 仍然是 400（重试只是
    把同一个错误再发一遍、再等一遍）；反过来，状态码为 0 的 `TransportError`
    表示根本没走到 HTTP（DNS / 连接 / 超时），那正是重试存在的理由。
    """
    if isinstance(exc, TransportError):
        return exc.status == 0 or exc.status in _RETRYABLE_STATUS_SET
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()
    if any(m in text for m in _RETRYABLE_MARKERS):
        return True
    return bool(_RETRYABLE_STATUS.search(text))


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动，避免惊群。"""
    return min(30.0, (2 ** attempt)) + random.uniform(0, 0.5)


# 日志里每段 reasoning 保留多少字符。reasoning 会被**回放**进后续每个请求，
# 全量落盘会让日志体积随步数近似平方增长（每一步都重带前面所有轮的 reasoning）。
_LOG_REASONING_CHARS = 200


def _shrink_reasoning(msg: Any) -> Any:
    """把一条消息里的 reasoning 截成「头 N 字符 + 省略计数」—— **只用于日志**。

    与图片 base64 同一个思路：真实请求照发全文，日志只留可辨识的痕迹。
    返回新 dict，绝不原地改 —— 这份 payload 还要真的发出去。
    """
    if not isinstance(msg, dict):
        return msg
    out = dict(msg)
    for fld in REASONING_FIELDS:
        val = out.get(fld)
        if isinstance(val, str) and len(val) > _LOG_REASONING_CHARS:
            out[fld] = (f"{val[:_LOG_REASONING_CHARS]}…"
                        f"<日志省略 {len(val) - _LOG_REASONING_CHARS} 字符>")
    return out


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    """日志里不留 api_key；图片 base64 折叠、reasoning 截断，避免日志被撑爆。"""
    out = dict(payload)
    out.pop("api_key", None)
    msgs = out.get("messages")
    if isinstance(msgs, list):
        out["messages"] = [_shrink_reasoning(m) for m in msgs]
    return sanitize_for_log(out)
