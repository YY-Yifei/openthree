"""极简 WebSocket 客户端（只用标准库）—— 只为钉钉 Stream 模式而写。

为什么不引入 `websockets` / `websocket-client`：钉钉这一路只有「连上、收文本帧、
发一个 ack 文本帧、回 ping」四种动作，标准库 socket 足够；多一个依赖就多一处
安装失败的可能，而频道是「把 openthree 接到聊天软件上」的入口，越少依赖越好。

实现范围（够用即止，不追求 RFC 全覆盖）：
  * 握手：`GET` + `Upgrade: websocket` + `Sec-WebSocket-Key`，校验 101
  * 帧：文本 / 二进制 / 续帧 / close / ping / pong；服务端→客户端不掩码、客户端→服务端必掩码
  * 分片：按 opcode 0 续帧累积到 fin
超范围的部分（扩展协商、permessage-deflate、子协议）不实现 —— 钉钉不要求。
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import threading
from typing import Optional
from urllib.parse import urlparse

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class WebSocketError(RuntimeError):
    """握手失败 / 连接被关闭 / 帧不合法。"""


class WebSocket:
    """一个阻塞式 WebSocket 连接：`recv_text()` 循环读消息，`send_text()` 发消息。

    Args:
        url: `ws://` 或 `wss://` 地址（可带 query）。
        connect_timeout: 建连（含 TLS 握手）超时。
        read_timeout: 单次读的超时。钉钉会定期回 pong，所以这个值可以设得较大；
            它只用来兜住「连接已经死了但没收到 FIN」的情况。
    """

    def __init__(self, url: str, *, connect_timeout: float = 15.0,
                 read_timeout: float = 180.0, headers: Optional[dict] = None) -> None:
        self.url = url
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self._extra_headers = dict(headers or {})
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._send_lock = threading.Lock()
        self._closed = False

    # --- 连接 ---------------------------------------------------------
    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"不支持的 WebSocket 地址：{self.url}")
        secure = parsed.scheme == "wss"
        host = parsed.hostname or ""
        if not host:
            raise WebSocketError(f"地址里没有主机名：{self.url}")
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        sock = socket.create_connection((host, port), timeout=self.connect_timeout)
        if secure:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for hkey, hval in self._extra_headers.items():
            lines.append(f"{hkey}: {hval}")
        request = "\r\n".join(lines) + "\r\n\r\n"
        sock.sendall(request.encode("ascii"))

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手期间连接被关闭")
            head += chunk
            if len(head) > 65536:
                raise WebSocketError("握手响应头过大")
        header_blob, _, rest = head.partition(b"\r\n\r\n")
        status_line = header_blob.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status_line:
            raise WebSocketError(f"握手被拒绝：{status_line}")

        self._sock = sock
        self._buf = bytearray(rest)
        self._closed = False
        sock.settimeout(self.read_timeout)

    # --- 关闭 ---------------------------------------------------------
    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._closed = True
        if sock is None:
            return
        try:
            with self._send_lock:
                sock.sendall(_encode_frame(OPCODE_CLOSE, b""))
        except Exception:  # noqa: BLE001 - 关闭路径上的失败无需惊动调用方
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def closed(self) -> bool:
        return self._closed or self._sock is None

    # --- 收发 ---------------------------------------------------------
    def send_text(self, text: str) -> None:
        self._send(OPCODE_TEXT, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        """发一个 ping 帧（保活用）；服务端会回 pong，我们只在 recv 里忽略它。"""
        self._send(OPCODE_PING, payload)

    def _send(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise WebSocketError("连接尚未建立或已关闭")
        with self._send_lock:
            sock.sendall(_encode_frame(opcode, payload, mask=True))

    def recv_text(self) -> Optional[str]:
        """读下一条完整消息（文本按 utf-8 解码）。

        Returns:
            消息文本；连接正常关闭时返回 ``None``。

        Raises:
            WebSocketError: 连接异常断开 / 帧不合法 / 读超时。
        """
        chunks: list[bytes] = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == OPCODE_PING:
                try:
                    self._send(OPCODE_PONG, payload)
                except WebSocketError:
                    return None
                continue
            if opcode == OPCODE_PONG:
                continue
            if opcode == OPCODE_CLOSE:
                try:
                    self._send(OPCODE_CLOSE, b"")
                except WebSocketError:
                    pass
                self.close()
                return None
            if opcode == OPCODE_TEXT or opcode == OPCODE_BINARY:
                chunks = [payload]
            elif opcode == OPCODE_CONT:
                if not chunks:
                    raise WebSocketError("收到无主的续帧")
                chunks.append(payload)
            else:
                raise WebSocketError(f"未知 opcode：{opcode}")
            if fin:
                data = b"".join(chunks)
                return data.decode("utf-8", "replace")

    # --- 帧编解码 -----------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            sock = self._sock
            if sock is None:
                raise WebSocketError("连接已关闭")
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise WebSocketError("读超时（连接可能已失效）") from exc
            except OSError as exc:
                raise WebSocketError(f"连接读失败：{exc}") from exc
            if not chunk:
                self._closed = True
                raise WebSocketError("连接被对端关闭")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._recv_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > 64 * 1024 * 1024:
            raise WebSocketError(f"帧过大：{length} 字节")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload


def _encode_frame(opcode: int, payload: bytes, *, mask: bool = True) -> bytes:
    """把一条消息编成一个 WebSocket 帧（客户端发出的帧必须掩码）。"""
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    header.extend(key)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def decode_frame(data: bytes) -> tuple[bool, int, bytes, int]:
    """从字节里解出一帧 → `(fin, opcode, payload, 消耗的字节数)`。

    与 `_encode_frame` 互为逆运算；单独抽出来是为了让帧格式能被测试直接钉住
    （起一个真 TCP 服务端来测编解码当然也可以，但那测的是两件事）。
    """
    if len(data) < 2:
        raise WebSocketError("帧头不完整")
    b1, b2 = data[0], data[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    pos = 2
    if length == 126:
        if len(data) < pos + 2:
            raise WebSocketError("扩展长度不完整")
        length = struct.unpack("!H", data[pos:pos + 2])[0]
        pos += 2
    elif length == 127:
        if len(data) < pos + 8:
            raise WebSocketError("扩展长度不完整")
        length = struct.unpack("!Q", data[pos:pos + 8])[0]
        pos += 8
    mask = b""
    if masked:
        if len(data) < pos + 4:
            raise WebSocketError("掩码键不完整")
        mask = data[pos:pos + 4]
        pos += 4
    if len(data) < pos + length:
        raise WebSocketError("载荷不完整")
    payload = data[pos:pos + length]
    pos += length
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, pos
