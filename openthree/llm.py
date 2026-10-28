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
            # httpx 缺席（罕见）时退回 float
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
