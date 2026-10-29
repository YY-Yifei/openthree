"""纯标准库的二维码编码器：文本 / URL → n×n 模块矩阵。

只做二维码规范里「生成」的那一半，且只做 byte 模式（UTF-8 字节流）——
扫码登录给的是一串 URL，byte 模式是唯一需要的也是唯一稳妥的：
不猜字符集、不做汉字模式的 Shift-JIS 换算，同一段文本在任何扫码器上都一致。

支持的版本 1–15（21×21 … 77×77 模块），纠错级 L / M / Q / H。
版本是**自适应的**：从 1 往上试，第一个装得下就停 —— 二维码越长越密，
能小就小，手机上更好扫。超过版本 15 的容量返回 None（调用方退化成「打印文本」）。

为什么自己写：iLink 的 `qrcode_img_content` 给的是一段 URL 而不是图片，
项目又坚持零第三方依赖，所以「URL → 可扫的码」这一步必须自己完成。

规范里的几条硬约定（改这个文件时不要动的）：
  * 码字（codeword）按块交织，块长只有「n」与「n+1」两种，短块在前；
  * 8 种掩码逐个试，按四条罚分规则取最小分；
  * 第 6 行 / 第 6 列是时序图形，任何「预留区域」都不得把它们覆盖；
  * 三个定位图形必须对得上 —— 它们错了，码就是废的。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# 版本上限：留到 15（77×77）足够装下任何登录 URL（M 级 ≈ 400 字节），
# 再往上的表只会增加抄错的机会，而登录场景永远用不到。
MAX_VERSION = 15
MIN_VERSION = 1

# 纠错级 → 格式信息里的 2 位编号（L=01 / M=00 / Q=11 / H=10）
_LEVEL_BITS = {"l": 1, "m": 0, "q": 3, "h": 2}

# (每块纠错码字数, 块数, 数据码字总数)；块长差恒为 1，短块在前。
_DATA: Dict[int, Dict[str, Tuple[int, int, int]]] = {
    1: {"l": (7, 1, 19), "m": (10, 1, 16), "q": (13, 1, 13), "h": (17, 1, 9)},
    2: {"l": (10, 1, 34), "m": (16, 1, 28), "q": (22, 1, 22), "h": (28, 1, 16)},
    3: {"l": (15, 1, 55), "m": (26, 1, 44), "q": (18, 2, 34), "h": (22, 2, 26)},
    4: {"l": (20, 1, 80), "m": (18, 2, 64), "q": (26, 2, 48), "h": (16, 4, 36)},
    5: {"l": (26, 1, 108), "m": (24, 2, 86), "q": (18, 4, 62), "h": (22, 4, 46)},
    6: {"l": (18, 2, 136), "m": (16, 4, 108), "q": (24, 4, 76), "h": (28, 4, 60)},
    7: {"l": (20, 2, 156), "m": (18, 4, 124), "q": (18, 6, 88), "h": (26, 5, 66)},
    8: {"l": (24, 2, 194), "m": (22, 4, 154), "q": (22, 6, 110), "h": (26, 6, 86)},
    9: {"l": (30, 2, 232), "m": (22, 5, 182), "q": (20, 8, 132), "h": (24, 8, 100)},
    10: {"l": (18, 4, 274), "m": (26, 5, 216), "q": (24, 8, 154), "h": (28, 8, 122)},
    11: {"l": (20, 4, 324), "m": (30, 5, 254), "q": (28, 8, 180), "h": (24, 11, 140)},
    12: {"l": (24, 4, 370), "m": (22, 8, 290), "q": (26, 10, 206), "h": (28, 11, 158)},
    13: {"l": (26, 4, 428), "m": (22, 9, 334), "q": (24, 12, 244), "h": (22, 16, 180)},
    14: {"l": (30, 4, 461), "m": (24, 9, 365), "q": (20, 16, 261), "h": (24, 16, 197)},
    15: {"l": (22, 6, 523), "m": (24, 10, 415), "q": (30, 12, 295), "h": (24, 18, 223)},
}

# 校正图形中心坐标（版本 1 没有校正图形）
_ALIGN: Dict[int, Tuple[int, ...]] = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
    7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
    11: (6, 30, 54), 12: (6, 32, 58), 13: (6, 34, 62), 14: (6, 26, 46, 66),
    15: (6, 26, 48, 70),
}

# 末尾补零的余数位（规范规定，不是笔误）
_REMAINDER: Dict[int, int] = {
    1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0,
    10: 0, 11: 0, 12: 0, 13: 0, 14: 3, 15: 3,
}

_PAD_BYTES = (0xEC, 0x11)


# --- GF(256) -----------------------------------------------------------
_EXP: List[int] = [0] * 512
_LOG: List[int] = [0] * 256


def _init_gf() -> None:
    """建指数 / 对数表（本原多项式 0x11D，二维码规范选定的那个）。"""
    value = 1
    for index in range(255):
        _EXP[index] = value
        _LOG[value] = index
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for index in range(255, 512):
        _EXP[index] = _EXP[index - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> List[int]:
    """生成多项式 `(x-α⁰)(x-α¹)…`，系数从最高次到常数项。"""
    poly = [1]
    for index in range(degree):
        factor = _EXP[index]
        out = [0] * (len(poly) + 1)
        for position, coefficient in enumerate(poly):
            out[position] ^= coefficient
            out[position + 1] ^= _gf_mul(coefficient, factor)
        poly = out
    return poly


def _rs_remainder(block: List[int], degree: int) -> List[int]:
    """一个数据块的 Reed-Solomon 纠错码字（多项式除法的余式）。"""
    generator = _rs_generator(degree)
    remainder = list(block) + [0] * degree
    for index in range(len(block)):
        factor = remainder[index]
        if factor:
            for offset, coefficient in enumerate(generator):
                remainder[index + offset] ^= _gf_mul(coefficient, factor)
    return remainder[len(block):]


# --- 数据编码 ----------------------------------------------------------
def _encode_codewords(text: str, version: int, level: str) -> Optional[List[int]]:
    """byte 模式：模式指示符 + 字符数 + UTF-8 字节，补终止符与填充字节。"""
    _ec, _blocks, total_data = _DATA[version][level]
    capacity = total_data * 8
    payload = text.encode("utf-8")
    count_bits = 8 if version <= 9 else 16
    needed = 4 + count_bits + 8 * len(payload)
    if needed > capacity:
        return None

    bits: List[int] = [0, 1, 0, 0]                       # 0100 = byte 模式
    bits.extend((len(payload) >> shift) & 1 for shift in range(count_bits - 1, -1, -1))
    for byte in payload:
        bits.extend((byte >> shift) & 1 for shift in range(7, -1, -1))
    bits.extend([0] * min(4, capacity - len(bits)))       # 终止符
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(bit) for bit in bits[start:start + 8]), 2)
                 for start in range(0, len(bits), 8)]
    index = 0
    while len(codewords) < total_data:
        codewords.append(_PAD_BYTES[index % 2])
        index += 1
    return codewords


def _interleave(codewords: List[int], ec_per_block: int, blocks: int) -> List[int]:
    """按块算纠错、再交织：数据码字逐列取，纠错码字跟在后面。"""
    total = len(codewords)
    short = total // blocks
    long_count = total % blocks
    lengths = [short] * (blocks - long_count) + [short + 1] * long_count

    data_blocks: List[List[int]] = []
    ec_blocks: List[List[int]] = []
    position = 0
    for length in lengths:
        block = codewords[position:position + length]
        position += length
        data_blocks.append(block)
        ec_blocks.append(_rs_remainder(block, ec_per_block))

    out: List[int] = []
    for index in range(max(lengths)):
        for block in data_blocks:
            if index < len(block):
                out.append(block[index])
    for index in range(ec_per_block):
        for block in ec_blocks:
            out.append(block[index])
    return out


# --- 功能图形 ----------------------------------------------------------
def _make_function_modules(version: int) -> Tuple[List[List[bool]], List[List[bool]]]:
    """画定位 / 时序 / 校正图形与深色模块，并标出「已占用」的位置。"""
    size = 17 + 4 * version
    modules = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(row: int, col: int, dark: bool) -> None:
        if 0 <= row < size and 0 <= col < size:
            modules[row][col] = dark
            fixed[row][col] = True

    def finder(row0: int, col0: int) -> None:
        # 7×7 定位图形 + 一圈浅色分隔符
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                ring = dr in (0, 6) or dc in (0, 6)
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                put(row0 + dr, col0 + dc, inside and (ring or core))

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for index in range(8, size - 8):                      # 时序图形
        dark = index % 2 == 0
        put(6, index, dark)
        put(index, 6, dark)

    centers = _ALIGN[version]
    for row in centers:                                   # 校正图形
        for col in centers:
            if (row, col) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(row + dr, col + dc, max(abs(dr), abs(dc)) != 1)

    put(size - 8, 8, True)                                # 固定的深色模块

    for index in range(9):                                # 格式信息区（左上）
        if index == 6:                                    # 第 6 行 / 第 6 列是时序图形
            continue
        put(8, index, False)
        put(index, 8, False)
    for index in range(8):                                # 格式信息区（右上 / 左下）
        put(8, size - 1 - index, False)
        put(size - 1 - index, 8, False)

    if version >= 7:                                      # 版本信息区
        for index in range(18):
            a = size - 11 + index % 3
            b = index // 3
            put(b, a, False)
            put(a, b, False)
    return modules, fixed


def _place_codewords(modules: List[List[bool]], fixed: List[List[bool]],
                     codewords: List[int], remainder: int) -> None:
    """从右下角起按「两列一组、上下交替」的蛇形把比特铺进去。"""
    size = len(modules)
    bits: List[int] = []
    for codeword in codewords:
        bits.extend((codeword >> shift) & 1 for shift in range(7, -1, -1))
    bits.extend([0] * remainder)

    index = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:                      # 竖直时序图形那一列不参与数据
            col -= 1
        for step in range(size):
            row = size - 1 - step if upward else step
            for current in (col, col - 1):
                if not fixed[row][current]:
                    modules[row][current] = bool(bits[index]) if index < len(bits) else False
                    index += 1
        col -= 2
        upward = not upward


# --- 掩码 / 格式信息 ---------------------------------------------------
def _mask_hit(mask: int, col: int, row: int) -> bool:
    """第 `mask` 号掩码在该位置是否翻转。"""
    if mask == 0:
        return (col + row) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (col + row) % 3 == 0
    if mask == 4:
        return (col // 3 + row // 2) % 2 == 0
    if mask == 5:
        return col * row % 2 + col * row % 3 == 0
    if mask == 6:
        return (col * row % 2 + col * row % 3) % 2 == 0
    return ((col + row) % 2 + col * row % 3) % 2 == 0


def _apply_mask(modules: List[List[bool]], fixed: List[List[bool]], mask: int) -> None:
    size = len(modules)
    for row in range(size):
        for col in range(size):
            if not fixed[row][col] and _mask_hit(mask, col, row):
                modules[row][col] = not modules[row][col]


def _bch_remainder(value: int, generator: int, degree: int) -> int:
    """BCH 余式：`value` 左移 `degree` 位后模 `generator`。"""
    remainder = value
    for _ in range(degree):
        remainder = (remainder << 1) ^ ((remainder >> (degree - 1)) * generator)
    return remainder & ((1 << degree) - 1)


def _draw_format(modules: List[List[bool]], level: str, mask: int) -> None:
    """15 位格式信息（纠错级 + 掩码 + BCH 校验）写两份，另加固定深色模块。"""
    size = len(modules)
    data = (_LEVEL_BITS[level] << 3) | mask
    bits = ((data << 10) | _bch_remainder(data, 0x537, 10)) ^ 0x5412

    def bit(index: int) -> bool:
        return bool((bits >> index) & 1)

    for index in range(6):
        modules[index][8] = bit(index)
    modules[7][8] = bit(6)
    modules[8][8] = bit(7)
    modules[8][7] = bit(8)
    for index in range(9, 15):
        modules[8][14 - index] = bit(index)

    for index in range(8):
        modules[8][size - 1 - index] = bit(index)
    for index in range(8, 15):
        modules[size - 15 + index][8] = bit(index)
    modules[size - 8][8] = True


def _draw_version(modules: List[List[bool]], version: int) -> None:
    """18 位版本信息（版本 7 起才有）。"""
    if version < 7:
        return
    size = len(modules)
    bits = (version << 12) | _bch_remainder(version, 0x1F25, 12)
    for index in range(18):
        value = bool((bits >> index) & 1)
        a = size - 11 + index % 3
        b = index // 3
        modules[b][a] = value
        modules[a][b] = value


# --- 罚分（选掩码用）---------------------------------------------------
def _penalty(modules: List[List[bool]]) -> int:
    """规范的四条罚分规则；分越低越好扫。"""
    size = len(modules)
    score = 0

    def run_score(line: List[bool]) -> int:
        total = 0
        run = 1
        for index in range(1, len(line)):
            if line[index] == line[index - 1]:
                run += 1
            else:
                if run >= 5:
                    total += 3 + (run - 5)
                run = 1
        if run >= 5:
            total += 3 + (run - 5)
        return total

    def finder_like(line: List[bool]) -> int:
        pattern = [True, False, True, True, True, False, True,
                   False, False, False, False]
        reverse = pattern[::-1]
        hits = 0
        for start in range(len(line) - 10):
            window = line[start:start + 11]
            if window == pattern or window == reverse:
                hits += 1
        return hits * 40

    for row in modules:
        score += run_score(row) + finder_like(row)
    for col in range(size):
        column = [modules[row][col] for row in range(size)]
        score += run_score(column) + finder_like(column)

    for row in range(size - 1):
        for col in range(size - 1):
            value = modules[row][col]
            if (modules[row][col + 1] == value
                    and modules[row + 1][col] == value
                    and modules[row + 1][col + 1] == value):
                score += 3

    dark = sum(1 for row in modules for value in row if value)
    percent = dark * 100 / (size * size)
    score += int(abs(percent - 50) / 5) * 10
    return score


# --- 对外 --------------------------------------------------------------
def _draw(version: int, codewords: List[int], level: str) -> List[List[bool]]:
    """铺码字、逐个试掩码、取罚分最低的那一张。"""
    base, fixed = _make_function_modules(version)
    _place_codewords(base, fixed, codewords, _REMAINDER[version])

    best: Optional[Tuple[int, List[List[bool]]]] = None
    for mask in range(8):
        trial = [row[:] for row in base]
        _apply_mask(trial, fixed, mask)
        _draw_format(trial, level, mask)
        _draw_version(trial, version)
        score = _penalty(trial)
        if best is None or score < best[0]:
            best = (score, trial)
    return best[1] if best else base


def encode_matrix(text: str, ec: str = "m", version: Optional[int] = None
                  ) -> Optional[List[List[bool]]]:
    """把文本编成 `n×n` 模块矩阵（`True` = 深色）；装不下就返回 None。

    `version=None` 时自适应选最小的版本；显式给版本便于测试与调试。
    """
    level = (ec or "m").lower()[:1]
    if level not in _LEVEL_BITS or not text:
        return None
    candidates = [version] if version else range(MIN_VERSION, MAX_VERSION + 1)
    for candidate in candidates:
        if candidate not in _DATA:
            continue
        codewords = _encode_codewords(text, candidate, level)
        if codewords is None:
            continue
        ec_per_block, blocks, _total = _DATA[candidate][level]
        return _draw(candidate, _interleave(codewords, ec_per_block, blocks), level)
    return None


def version_of(matrix: List[List[bool]]) -> int:
    """矩阵边长 → 版本号（反推，给测试与日志用）。"""
    return (len(matrix) - 17) // 4


def capacity(version: int, ec: str = "m") -> int:
    """该版本 / 纠错级下 byte 模式最多能装多少字节。"""
    level = (ec or "m").lower()[:1]
    _ec, _blocks, total_data = _DATA[version][level]
    count_bits = 8 if version <= 9 else 16
    return (total_data * 8 - 4 - count_bits) // 8
