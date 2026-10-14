"""LLM 客户端：极薄的一层。

不引入框架。openai SDK 已是事实标准，也兼容一切 OpenAI 协议的服务
（DeepSeek / GLM / Kimi / vLLM / Ollama / ...）。

配置分层（后者覆盖前者）：
    1. 环境变量                OPENAI_API_KEY / OPENAI_BASE_URL / OPENTHREE_MODEL
    2. 用户级                  ~/.openthree/config.yaml
    3. 项目级                  ./.openthree/config.yaml

只要有任何一层给了 base_url + api_key + model 就能跑。
"""
from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    max_tokens: int = 8192
    # 模型上下文窗口（token）。上下文预算的默认值的来源。
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    temperature: float = 0.3
    timeout: float = 600.0
    # connect 阶段单独设一个短超时：read=600s 是合理的（等模型生成），
    # 但「连不上主机」跟「模型想得久」是两回事——不分开就会把 connect 也
    # 抬到 600s，一次重试要干等 10 分钟。见 LLM._ensure_client。
    connect_timeout: float = 5.0
    retries: int = 3
    # openai SDK 自带的传输层重试。默认 0：重试只在 openthree 这一层发生，
    # 次数才可预期（retries=3 就是最多 4 次调用）。若交给 SDK 兜底，
    # 两层会相乘：真实 HTTP 次数 = (retries+1) × (sdk_max_retries+1)。
    max_retries: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)
    source: str = "env"

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


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
        max_tokens=int(parse_token_count(merged.get("max_tokens")) or 8192),
        context_window_tokens=int(window),
        temperature=float(merged.get("temperature", 0.3) or 0.3),
        timeout=float(merged.get("timeout", 600) or 600),
        connect_timeout=float(merged.get("connect_timeout", 5) or 5),
        retries=int(merged.get("retries", 3) or 0),
        max_retries=int(merged.get("max_retries", 0) or 0),
        extra=dict(merged.get("extra") or {}),
    )
    return cfg


class LLM:
    """一次调用的薄封装。返回 assistant message。"""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 openai 依赖，请执行： pip install openai"
            ) from exc
        kwargs: Dict[str, Any] = {"api_key": self.config.api_key}
        # 关键的分离：只给一个 float 超时，httpx 会把它同时当作 read / write /
        # pool / **connect** 的超时——于是「连不上主机」也要等整整 600s。
        # 显式构造 httpx.Timeout：connect 用短超时快速失败，其余沿用 timeout。
        try:
            import httpx
            kwargs["timeout"] = httpx.Timeout(
                self.config.timeout, connect=self.config.connect_timeout)
        except Exception:  # noqa: BLE001  # pragma: no cover
            # httpx 缺席（罕见）时退回 float，行为不比从前差
            kwargs["timeout"] = self.config.timeout
        # 显式钉死 SDK 自带重试：默认 0，让重试只发生在 openthree 这一层。
        kwargs["max_retries"] = max(0, int(self.config.max_retries or 0))
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        self._client = OpenAI(**kwargs)
        return self._client

    def chat(self, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             tool_choice: str = "auto",
             max_tokens: Optional[int] = None,
             temperature: Optional[float] = None) -> Dict[str, Any]:
        """返回 {message, usage, elapsed_ms, raw_request, raw_response, attempts}。

        `attempts` 是**本次调用真正打到线上的次数**（含重试；>1 说明重试过且最终
        成功/放弃）。失败与成功两条路径都会带上它，供日志与进度显示用。

        `max_tokens` / `temperature` 可**按次覆盖**配置里的值 —— 调用方（Agent）
        由此把「模型自管的运行边界」按调用传进来，而不必去改 `self.config`。
        这很关键：Runtime 在 `-i` / `--auto` 下复用同一个 LLM，若靠原地改 config，
        这一轮写进去的边界会污染下一轮的「内核默认」。
        """
        client = self._ensure_client()
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
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        started = time.time()
        attempts = max(0, self.config.retries) + 1
        used = 0
        resp = None
        for attempt in range(attempts):
            used = attempt + 1
            try:
                resp = client.chat.completions.create(**payload)
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

        try:
            data = resp.model_dump()
        except Exception:  # pragma: no cover
            data = {"repr": repr(resp)}

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
_RETRYABLE_STATUS = re.compile(
    r"(?<!\d)(408|409|429|500|502|503|504)(?!\d)")


def _retryable(exc: BaseException) -> bool:
    """网络抖动 / 限流 / 5xx 才重试；参数错误、鉴权失败等立刻返回。"""
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()
    if any(m in text for m in _RETRYABLE_MARKERS):
        return True
    return bool(_RETRYABLE_STATUS.search(text))


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动，避免惊群。"""
    return min(30.0, (2 ** attempt)) + random.uniform(0, 0.5)


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    """日志里不留 api_key；图片 base64 折叠成占位符，避免日志被撑爆。"""
    out = dict(payload)
    out.pop("api_key", None)
    return sanitize_for_log(out)
