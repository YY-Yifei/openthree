"""极简 HTTP/JSON 客户端（只用标准库）。

为什么不用 requests / httpx：频道层要在**任何**装了 openthree 的环境里都能跑，
而 openthree 的主依赖只有 openai / PyYAML / json_repair。urllib 够用 —— 频道
要的无非是「发个 JSON、读回 JSON」，以及「带 45 秒超时的长轮询」。

失败一律抛 `HttpError`（带上状态码与服务端返回的前 300 字），因为渠道 API 的
错误几乎总是藏在响应正文里，光看 400 没法排查。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlencode

USER_AGENT = "openthree-channel/1.0 (+https://github.com/YY-Yifei/openthree)"


class HttpError(RuntimeError):
    """HTTP 调用失败：非 2xx，或响应不是 JSON。"""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def request_json(
    url: str,
    *,
    method: str = "POST",
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
) -> Any:
    """发一个请求并把响应解析成 JSON；2xx 之外抛 `HttpError`。"""
    if params:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    body: Optional[bytes] = None
    hdrs: Dict[str, str] = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:  # 4xx / 5xx
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(f"HTTP {exc.code} {url}: {detail[:300]}",
                        status=exc.code, body=detail) from exc
    except urllib.error.URLError as exc:  # DNS / 连接失败
        raise HttpError(f"连接 {url} 失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpError(f"{url} 超时（{timeout}s）") from exc

    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HttpError(f"{url} 返回的不是 JSON：{text[:300]}",
                        status=status, body=text) from exc


def get_json(url: str, **kwargs: Any) -> Any:
    return request_json(url, method="GET", **kwargs)


def post_json(url: str, payload: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
    return request_json(url, method="POST", payload=payload if payload is not None else {}, **kwargs)


def post_json_status(url: str, payload: Optional[Dict[str, Any]] = None,
                     *, timeout: float = 15.0) -> tuple[int, str]:
    """返回 `(状态码, 正文)` 而不抛非 2xx —— 少数渠道用状态码表示业务结果。

    DingTalk 的 sessionWebhook 就是这种：42001 表示「webhook 过期」，
    这是一个**要处理**的返回值，不是异常。
    """
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return exc.code, ""
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)
    except TimeoutError:
        return 0, "timeout"
