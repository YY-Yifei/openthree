"""极简 HTTP/JSON 客户端（只用标准库）。

为什么不用 requests / httpx：频道层要在**任何**装了 openthree 的环境里都能跑，
而 openthree 的主依赖只有 PyYAML / json_repair（LLM 层同样用标准库自己发请求，
见 llm.py 的 post_chat）。urllib 够用 —— 频道
要的无非是「发个 JSON、读回 JSON」，以及「带 45 秒超时的长轮询」。

失败一律抛 `HttpError`（带上状态码与服务端返回的前 300 字），因为渠道 API 的
错误几乎总是藏在响应正文里，光看 400 没法排查。
"""
from __future__ import annotations

import http.client
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
    except (OSError, http.client.HTTPException) as exc:
        # 读正文途中断连 / 截断（`ConnectionResetError`、`IncompleteRead`、
        # `ssl.SSLError`）不上面那三条之列。而本模块的承诺是「失败一律抛
        # `HttpError`」—— 调用方只捕它，漏出去的那一个会让整条消息（连同正文）被丢掉。
        raise HttpError(f"{url} 读取响应失败：{type(exc).__name__}: {exc}") from exc

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


def post_bytes(url: str, data: bytes, *, headers: Optional[Dict[str, str]] = None,
               timeout: float = 60.0) -> tuple[int, Dict[str, str], bytes]:
    """POST 一段原始字节，返回 `(状态码, 响应头, 响应体)` —— **不解析 JSON**。

    微信的媒体上传就是这样：请求体是加密后的二进制，而「文件落在哪」这条信息在
    **响应头**里（`x-encrypted-param`），正文可能是空的。`request_json()` 会把这两样
    都丢掉（它只认正文里的 JSON），所以上传这条路需要一个更低的入口。

    响应头的键**一律小写**：urllib 返回的是大小写不敏感的 `HTTPMessage`，而一旦
    变成普通 dict 就不敏感不起来了 —— 统一小写，调用方 `headers.get("x-...")` 即可。
    非 2xx 抛 `HttpError`（与其它入口同一套错误类型）。
    """
    hdrs: Dict[str, str] = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            got = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(f"HTTP {exc.code} {url}: {detail[:300]}",
                        status=exc.code, body=detail) from exc
    except urllib.error.URLError as exc:
        raise HttpError(f"连接 {url} 失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpError(f"{url} 超时（{timeout}s）") from exc
    except (OSError, http.client.HTTPException) as exc:
        # 同 `request_json()`：正文读到一半断掉也要变成 `HttpError`，否则
        # 「上传失败」会以 `ConnectionResetError` 的身份冒到调用方之外。
        raise HttpError(f"{url} 读写失败：{type(exc).__name__}: {exc}") from exc
    return status, got, body


def get_bytes(url: str, *, timeout: float = 30.0, max_bytes: int = 0,
              headers: Optional[Dict[str, str]] = None) -> bytes:
    """GET 一段原始字节，返回响应体 —— **不解析**，也**不无限读**。

    微信的入站图片就是这样：CDN 上躺着一份 AES 加密的二进制，凭证在 URL 里。
    `request_json()` 走不通（它只认 JSON），`post_bytes()` 是 POST，所以再来一个
    更低的入口。

    `max_bytes` 是**硬闸**（0 = 不限）：响应体来自外部，而「读进内存」这件事必须先
    有上界 —— 分块读到 `max_bytes + 1` 就停，多出来的那 1 字节就是超限的证据。
    超限抛 `HttpError` 而不是返回截断的数据：半张图与一张灰图是同一种坏法。
    """
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if max_bytes and max_bytes > 0:
                # 声明比上限还大就直接放弃，不必先把这几 MB 读进来。
                declared = resp.headers.get("Content-Length")
                try:
                    if declared is not None and int(declared) > max_bytes:
                        raise HttpError(f"{url} 响应体 {declared} 字节超过上限 {max_bytes}")
                except ValueError:
                    pass            # 头里的长度不是数字：交给下面按块判断
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise HttpError(f"{url} 响应体超过上限 {max_bytes} 字节")
                return data
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:      # noqa: BLE001
            pass
        raise HttpError(f"HTTP {exc.code} {url}: {detail[:300]}",
                        status=exc.code, body=detail) from exc
    except urllib.error.URLError as exc:
        raise HttpError(f"连接 {url} 失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpError(f"{url} 超时（{timeout}s）") from exc
    except (OSError, http.client.HTTPException) as exc:
        # 正文读到一半断掉（`ConnectionResetError` / `IncompleteRead`）：CDN 抖一下
        # 不该让整条消息消失 —— 收图那边只捕 `HttpError`。
        raise HttpError(f"{url} 读取响应失败：{type(exc).__name__}: {exc}") from exc
