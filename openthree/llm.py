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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


DEFAULT_MODEL = "deepseek-v4.1-flash"


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = DEFAULT_MODEL
    max_tokens: int = 8192
    temperature: float = 0.3
    timeout: float = 600.0
    retries: int = 3
    extra: Dict[str, Any] = field(default_factory=dict)
    source: str = "env"

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


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

    cfg = LLMConfig(
        api_key=str(merged.get("api_key", "") or ""),
        base_url=str(merged.get("base_url", "") or ""),
        model=str(model_override or merged.get("model") or DEFAULT_MODEL),
        max_tokens=int(merged.get("max_tokens", 8192) or 8192),
        temperature=float(merged.get("temperature", 0.3) or 0.3),
        timeout=float(merged.get("timeout", 600) or 600),
        retries=int(merged.get("retries", 3) or 0),
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
        kwargs: Dict[str, Any] = {"api_key": self.config.api_key, "timeout": self.config.timeout}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        self._client = OpenAI(**kwargs)
        return self._client

    def chat(self, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             tool_choice: str = "auto") -> Dict[str, Any]:
        """返回 {message, usage, elapsed_ms, raw_request, raw_response}。"""
        client = self._ensure_client()
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        for k, v in (self.config.extra or {}).items():
            payload.setdefault(k, v)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        started = time.time()
        attempts = max(0, self.config.retries) + 1
        resp = None
        for attempt in range(attempts):
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
        if finish_reason == "length":
            error = (f"模型输出因 max_tokens={self.config.max_tokens} 被截断"
                     f"（finish_reason=length）")

        return {
            "message": message,
            "usage": usage,
            "elapsed_ms": elapsed,
            "raw_request": _redact(payload),
            "raw_response": data,
            "error": error,
            "finish_reason": finish_reason,
        }


_RETRYABLE_MARKERS = (
    "timeout", "timedout", "connection", "connect", "ratelimit", "rate_limit",
    "internalserver", "servererror", "serviceunavailable", "apiconnection",
    "temporarily", "502", "503", "504",
)


def _retryable(exc: BaseException) -> bool:
    """网络抖动 / 限流 / 5xx 才重试；参数错误、鉴权失败等立刻返回。"""
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()
    return any(m in text for m in _RETRYABLE_MARKERS)


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动，避免惊群。"""
    return min(30.0, (2 ** attempt)) + random.uniform(0, 0.5)


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    """日志里不留 api_key。"""
    out = dict(payload)
    out.pop("api_key", None)
    return out
