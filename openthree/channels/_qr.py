"""二维码 PNG → 终端字符画（纯标准库）。

扫码登录必须让人**看见**二维码：终端里贴不了图片，只给一个 png 路径等于让人
先切出去开文件。这里把二维码 PNG 解成模块矩阵，再用半块字符（█ ▀ ▄）直接画在
控制台上，手机对着终端就能扫。

自己解 PNG（zlib + 五种行过滤），只认**非隔行**的 1/2/4/8 位灰度 / RGB / 调色板 /
带 alpha 图 —— 二维码 PNG 就长这样，够了。任何一步解不出来都返回空串 / None，
调用方退化成「给路径」，绝不因为它把一次能成的登录弄挂。

判据一句话：这个模块**只负责看与画**，不碰网络；落盘的字节由调用方写。
"""
from __future__ import annotations

import base64
import shutil
import struct
import sys
import zlib
from typing import Any, Iterator, List, Optional, Sequence, Tuple

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}   # 灰度 / RGB / 调色板 / 灰度+alpha / RGBA
_DEPTHS = (1, 2, 4, 8)
_MIN_MODULES = 21                            # QR 版本 1 是 21×21
_MAX_MODULES = 177                           # QR 版本 40 是 177×177
_LUMA_THRESHOLD = 128
# 半块字符：键 = (上半模块行是否深色, 下半模块行是否深色)
_BLOCKS = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


# --- PNG 解码 ---------------------------------------------------------
def _iter_chunks(data: bytes) -> Iterator[Tuple[bytes, bytes]]:
    pos = len(_SIGNATURE)
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            return
        pos += length + 12          # 长度(4) + 类型(4) + 数据 + CRC(4)
        yield ctype, body
        if ctype == b"IEND":
            return


def _paeth(left: int, up: int, upleft: int) -> int:
    p = left + up - upleft
    pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
    if pa <= pb and pa <= pc:
        return left
    return up if pb <= pc else upleft


def _unfilter(raw: bytes, row_bytes: int, bpp: int,
              height: int) -> Optional[List[bytearray]]:
    """按 PNG 的五种行过滤（None / Sub / Up / Average / Paeth）还原扫描行。"""
    rows: List[bytearray] = []
    prev = bytearray(row_bytes)
    pos = 0
    for _ in range(height):
        if pos >= len(raw):
            return None
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + row_bytes])
        pos += 1 + row_bytes
        if len(line) < row_bytes:
            return None
        if ftype == 0:
            pass
        elif ftype == 1:                       # Sub：加左边
            for i in range(bpp, row_bytes):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:                       # Up：加上边
            for i in range(row_bytes):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                       # Average：加左、上均值
            for i in range(row_bytes):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                       # Paeth
            for i in range(row_bytes):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            return None
        rows.append(line)
        prev = line
    return rows


def _row_values(line: bytearray, width: int, channels: int, depth: int) -> List[int]:
    """把一行字节摊成逐像素通道值（1/2/4 位按每字节高位在前解包）。"""
    if depth == 8:
        return list(line[:width * channels])
    mask = (1 << depth) - 1
    values: List[int] = []
    bit = 0
    for _ in range(width * channels):
        values.append((line[bit >> 3] >> (8 - depth - (bit & 7))) & mask)
        bit += depth
    return values


def _is_dark_rgb(red: int, green: int, blue: int) -> bool:
    return (red * 299 + green * 587 + blue * 114) // 1000 < _LUMA_THRESHOLD


def _dark_rows(rows: List[bytearray], width: int, color_type: int, depth: int,
               palette: Optional[List[Tuple[int, ...]]]) -> List[List[bool]]:
    channels = _CHANNELS[color_type]
    scale = 255 // ((1 << depth) - 1) if depth < 8 else 1
    dark: List[List[bool]] = []
    for line in rows:
        values = _row_values(line, width, channels, depth)
        out: List[bool] = []
        for x in range(width):
            base = x * channels
            if color_type == 0:                # 灰度
                out.append(values[base] * scale < _LUMA_THRESHOLD)
            elif color_type == 4:              # 灰度 + alpha
                out.append(values[base + 1] >= 128
                           and values[base] * scale < _LUMA_THRESHOLD)
            elif color_type == 3:              # 调色板
                index = values[base]
                rgb = palette[index] if palette and index < len(palette) else None
                out.append(bool(rgb) and _is_dark_rgb(*rgb[:3]))
            elif color_type == 2:              # RGB
                out.append(_is_dark_rgb(values[base], values[base + 1], values[base + 2]))
            else:                              # RGBA：透明当白底
                out.append(values[base + 3] >= 128
                           and _is_dark_rgb(values[base], values[base + 1], values[base + 2]))
        dark.append(out)
    return dark


def decode_png(data: bytes) -> Optional[Tuple[int, int, List[List[bool]]]]:
    """解一张 PNG，返回 `(宽, 高, 逐像素是否深色)`；不认识的格式返回 None。"""
    if not data or not data.startswith(_SIGNATURE):
        return None
    header: Optional[Tuple[int, ...]] = None
    palette: Optional[List[Tuple[int, ...]]] = None
    idat = bytearray()
    for ctype, body in _iter_chunks(data):
        if ctype == b"IHDR" and len(body) >= 13:
            header = struct.unpack(">IIBBBBB", body[:13])
        elif ctype == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body) - 2, 3)]
        elif ctype == b"IDAT":
            idat += body
    if header is None or not idat:
        return None
    width, height, depth, color_type, _comp, _filt, interlace = header
    if width <= 0 or height <= 0 or interlace != 0:
        return None
    if color_type not in _CHANNELS or depth not in _DEPTHS:
        return None
    channels = _CHANNELS[color_type]
    row_bytes = (width * channels * depth + 7) // 8
    bpp = max(1, channels * depth // 8)
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    rows = _unfilter(raw, row_bytes, bpp, height)
    if rows is None:
        return None
    return width, height, _dark_rows(rows, width, color_type, depth, palette)


# --- 图 → 模块矩阵 ----------------------------------------------------
def _bbox(dark: List[List[bool]]) -> Optional[Tuple[int, int, int, int]]:
    """深色像素的包围盒 `(min_x, min_y, max_x, max_y)`（用来剥掉自带的白边）。"""
    min_x: Optional[int] = None
    min_y: Optional[int] = None
    max_x = max_y = -1
    for y, row in enumerate(dark):
        first = last = -1
        for x, value in enumerate(row):
            if value:
                if first < 0:
                    first = x
                last = x
        if first < 0:
            continue
        if min_y is None:
            min_y = y
        max_y = y
        min_x = first if min_x is None else min(min_x, first)
        max_x = max(max_x, last)
    if min_y is None or min_x is None or max_x < 0:
        return None
    return min_x, min_y, max_x, max_y


def _score(dark: List[List[bool]], box: Tuple[int, int, int, int], modules: int) -> float:
    """给「这个 n 模块解释」打分：抽样若干模块格，看格子内部是否同色。

    模块尺寸猜对时每个格子内部是纯色；猜错（差半格）时格子会横跨深浅交界，
    抽样点就自相矛盾。抽样点落在格子的 1/4、1/2、3/4 处，避开边缘的抗锯齿像素。
    """
    min_x, min_y, max_x, max_y = box
    sx = (max_x - min_x + 1) / modules
    sy = (max_y - min_y + 1) / modules
    if sx < 1 or sy < 1:
        return 0.0
    stride = max(1, modules // 24)              # 最多看约 24×24 个格子
    offsets = (0.25, 0.5, 0.75)
    good = total = 0
    for row in range(0, modules, stride):
        for col in range(0, modules, stride):
            y0 = min_y + int(row * sy)
            x0 = min_x + int(col * sx)
            seen = set()
            for dy in offsets:
                py = min(y0 + int(dy * sy), max_y)
                for dx in offsets:
                    seen.add(dark[py][min(x0 + int(dx * sx), max_x)])
            total += 1
            good += len(seen) == 1
    return good / total if total else 0.0


def _has_finder(grid: List[List[bool]], row0: int, col0: int) -> bool:
    """一个 7×7 定位图形：外圈深色方框 + 中间 3×3 深色实心。"""
    for dr in range(7):
        for dc in range(7):
            ring = dr in (0, 6) or dc in (0, 6)
            core = 2 <= dr <= 4 and 2 <= dc <= 4
            if grid[row0 + dr][col0 + dc] != (ring or core):
                return False
    return True


def _module_grid(dark: List[List[bool]],
                 box: Tuple[int, int, int, int]) -> Optional[List[List[bool]]]:
    """在包围盒里找出模块尺寸，采样成 n×n 矩阵；找不到可信解释就返回 None。"""
    min_x, min_y, max_x, max_y = box
    width = max_x - min_x + 1
    height = max_y - min_y + 1
    if width < _MIN_MODULES or height < _MIN_MODULES:
        return None

    # 左上定位图形的顶边正好是 7 个模块宽 —— 先按它估一版，猜错再退化成全扫。
    run = 0
    top = dark[min_y]
    x = min_x
    while x <= max_x and top[x]:
        run += 1
        x += 1
    candidates: List[int] = []
    if run >= 7:
        guess = int(round(width / (run / 7)))
        candidates.extend(guess + delta for delta in (-8, -4, 0, 4, 8))
    candidates.extend(range(_MIN_MODULES, _MAX_MODULES + 1, 4))
    best_score = 0.0
    best_modules = 0
    tried = set()
    for modules in candidates:
        if modules in tried or modules % 2 == 0:
            continue
        tried.add(modules)
        if modules < _MIN_MODULES or modules > _MAX_MODULES:
            continue
        score = _score(dark, box, modules)
        if score > best_score:
            best_score, best_modules = score, modules
    if not best_modules or best_score < 0.9:
        return None

    sx = width / best_modules
    sy = height / best_modules
    grid: List[List[bool]] = []
    for row in range(best_modules):
        y = min(min_y + int((row + 0.5) * sy), max_y)
        grid.append([dark[y][min(min_x + int((col + 0.5) * sx), max_x)]
                     for col in range(best_modules)])
    # 三个定位图形是二维码的铁证：对不上就是尺寸猜错了，宁可不画。
    if not (_has_finder(grid, 0, 0)
            and _has_finder(grid, 0, best_modules - 7)
            and _has_finder(grid, best_modules - 7, 0)):
        return None
    return grid


def png_to_matrix(data: bytes) -> Optional[List[List[bool]]]:
    """PNG 字节 → `n×n` 模块矩阵（`True` = 深色）；认不出来返回 None。"""
    decoded = decode_png(data)
    if decoded is None:
        return None
    _width, _height, dark = decoded
    box = _bbox(dark)
    if box is None:
        return None
    return _module_grid(dark, box)


# --- 矩阵 → 字符画 ----------------------------------------------------
def console_style(stream: Optional[Any] = None) -> str:
    """当前输出流能画到什么档：`half`（半块字符）/ `full`（只有全块）/ `ascii`。

    Windows 的 GBK 控制台有 `█` 却没有 `▀`、`▄` —— 硬画半块会 UnicodeEncodeError，
    把一次能成的登录弄挂。所以按「编码得出什么」逐档退让，最差也画得出 `##`。
    """
    stream = stream if stream is not None else sys.stdout
    encoding = getattr(stream, "encoding", "") or "utf-8"
    for style, probe in (("half", "█▀▄"), ("full", "█")):
        try:
            probe.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            continue
        return style
    return "ascii"


def console_width(default: int = 80) -> int:
    """当前终端有几列。取不到（重定向到文件 / 无 tty）就按 `default` 算。"""
    try:
        return int(shutil.get_terminal_size((default, 24)).columns)
    except Exception:  # noqa: BLE001 - 宽度只影响排版，猜不出来不该坏事
        return default


def render_matrix(matrix: Sequence[Sequence[bool]], quiet: int = 2,
                  style: str = "auto", max_width: Optional[int] = None,
                  min_quiet: int = 0) -> str:
    """把模块矩阵画成字符画，模块左右各留 `quiet` 个空白的静默区。

    * `half`：一行画两个模块行（`█` / `▀` / `▄`），一个模块占一个字符宽、半个
      字符高 —— 正好抵消终端字符「高约是宽两倍」的长宽比，画出来是正方形。
    * `full` / `ascii`：一行画一个模块行，一个模块占两个字符宽（`██` / `##`），
      长宽比同样成立，代价是行数翻倍、字符集更保守。

    `style="auto"` 按输出流编码自动选档（见 `console_style`）。
    `max_width` 给了就**先把静默区收窄到装得下**；收到底还装不下则返回空串，
    让调用方退化成「给图片路径」—— 折行的二维码是扫不出来的，宁可不画。
    """
    if not matrix:
        return ""
    style = console_style() if style == "auto" else style
    modules = len(matrix)
    quiet = max(0, int(quiet))
    per_module = 1 if style == "half" else 2
    if max_width:
        floor = max(0, min(int(min_quiet), quiet))
        while quiet > floor and (modules + 2 * quiet) * per_module > max_width:
            quiet -= 1
        if (modules + 2 * quiet) * per_module > max_width:
            return ""
    width = modules + 2 * quiet
    padded: List[List[bool]] = [[False] * width for _ in range(quiet)]
    for row in matrix:
        padded.append([False] * quiet + [bool(v) for v in row] + [False] * quiet)
    padded.extend([[False] * width for _ in range(quiet)])

    if style != "half":
        cell = "██" if style == "full" else "##"
        return "\n".join(
            "".join(cell if value else "  " for value in row) for row in padded)

    if len(padded) % 2:
        padded.append([False] * width)
    lines: List[str] = []
    for index in range(0, len(padded), 2):
        upper, lower = padded[index], padded[index + 1]
        lines.append("".join(_BLOCKS[(upper[c], lower[c])] for c in range(width)))
    return "\n".join(lines)


def render_png(data: bytes, quiet: int = 2, style: str = "auto") -> str:
    """PNG 字节 → 字符画；解不出来返回空串（调用方据此退化成「给路径」）。"""
    matrix = png_to_matrix(data)
    if matrix is None:
        return ""
    return render_matrix(matrix, quiet=quiet, style=style)


def decode_base64(content: str) -> Optional[bytes]:
    """宽松地解 base64（容忍空白、`data:` 前缀、缺 padding）；失败返回 None。"""
    text = (content or "").strip()
    if not text:
        return None
    if text.lower().startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = "".join(text.split())
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text)
    except Exception:  # noqa: BLE001 - 坏 base64 只是画不出来，不是错误
        return None


def render_qr_base64(content: str, quiet: int = 2, style: str = "auto") -> str:
    """服务端给的 base64 PNG → 字符画；解不出来返回空串。"""
    raw = decode_base64(content)
    return render_png(raw, quiet=quiet, style=style) if raw else ""


# --- 内容 → 矩阵 / 矩阵 → PNG ----------------------------------------
def content_to_matrix(content: str) -> Tuple[Optional[List[List[bool]]], bytes]:
    """服务端给的 `qrcode_img_content` → `(矩阵, 原始 PNG 字节)`。

    实测两种形态都会出现：**base64 的 PNG 图片**，或**一段 URL 文本**
    （微信的扫码确认页，形如 `https://liteapp.weixin.qq.com/q/…`）。
    所以先按 PNG 解；解不出来就把内容当文本自己编码 —— 两种形态都能画到终端上。
    第二项只在内容确实是 PNG 时才非空，调用方据此决定落盘是存原图还是存自己生成的图。
    """
    if not content:
        return None, b""
    raw = decode_base64(content)
    if raw:
        matrix = png_to_matrix(raw)
        if matrix is not None:
            return matrix, raw
    from . import _qrencode

    return _qrencode.encode_matrix(content.strip()), b""


def _chunk(tag: bytes, body: bytes) -> bytes:
    """一个 PNG 数据块：长度 + 类型 + 内容 + CRC32。"""
    return (struct.pack(">I", len(body)) + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))


def matrix_to_png(matrix: Sequence[Sequence[bool]], scale: int = 8,
                  quiet: int = 4) -> bytes:
    """模块矩阵 → 8 位灰度 PNG 字节（每模块 `scale` 像素，四周留 `quiet` 模块白边）。

    服务端只给 URL 时，落盘的那张 PNG 由这里现场生成 —— 于是「终端里能扫」与
    「事后拿文件重扫 / 转发」用的是同一张码，不会是空壳文件。
    """
    if not matrix:
        return b""
    modules = len(matrix)
    scale = max(1, int(scale))
    quiet = max(0, int(quiet))
    side = (modules + 2 * quiet) * scale
    raw = bytearray()
    for y in range(side):
        raw.append(0)                       # 每行的过滤字节：0 = 不过滤
        row = y // scale - quiet
        for x in range(side):
            col = x // scale - quiet
            dark = 0 <= row < modules and 0 <= col < modules and matrix[row][col]
            raw.append(0 if dark else 255)
    header = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)   # 8 位灰度
    return (_SIGNATURE + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


def encode_text_matrix(text: str, ec: str = "m") -> Optional[List[List[bool]]]:
    """一段文本 → 模块矩阵（`ec` 是纠错级 L/M/Q/H，默认 M）。"""
    from . import _qrencode

    return _qrencode.encode_matrix(text, ec=ec)


def render_text(text: str, quiet: int = 2, style: str = "auto",
                ec: str = "m", max_width: Optional[int] = None,
                min_quiet: int = 0) -> str:
    """一段文本 → 控制台字符画（自己编码，不依赖服务端给的图片）。"""
    matrix = encode_text_matrix(text, ec=ec)
    if not matrix:
        return ""
    return render_matrix(matrix, quiet=quiet, style=style,
                         max_width=max_width, min_quiet=min_quiet)


if __name__ == "__main__":  # pragma: no cover - 手工调试：python -m openthree.channels._qr 图.png|文本
    from pathlib import Path

    if len(sys.argv) < 2:
        print("用法：python -m openthree.channels._qr <二维码.png | 一段文本>")
    else:
        target = Path(sys.argv[1])
        if target.exists():
            print(render_png(target.read_bytes()) or "（这张图认不出来）")
        else:
            print(render_text(sys.argv[1]) or "（这段文本编不出来）")
