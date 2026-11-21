"""AES-128-ECB + PKCS#7（纯标准库，只用 `secrets` 之外的两个运算符）。

**为什么自己写**：iLink 的媒体上传要求「先把文件按 AES-128-ECB 加密、再传 CDN」，
而 openthree 的主依赖只有 PyYAML / json_repair —— 频道层要在**任何**装了 openthree
的环境里跑起来，不能为了发一张图要求用户再装 pycryptodome。频道层的 `_http.py`
是同样的理由（urllib 而非 requests）。

**为什么只有加密**：出站要加密（上传），入站要解密（下载）。两条路径都写在这里，
共用同一份 S 盒与密钥扩展 —— 两份实现迟早有一份悄悄错一位，而错一位的密文
在本地看起来完全正常（长度对、不抛异常），只有对面解不开。

**正确性靠向量，不靠眼**：NIST FIPS-197 附录 C.1 的 AES-128 单块向量与
SP 800-38A 的 ECB-AES128 多块向量都在 `tests/test_channels_aes.py` 里，
改这个文件之后先跑它们。
"""
from __future__ import annotations

__all__ = ["encrypt", "decrypt", "pad", "unpad"]

BLOCK_SIZE = 16

# FIPS-197 Figure 7（S 盒）。写成 16 行 × 16 列，与规范排布一致，肉眼比对容易。
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)

# 逆 S 盒：由 S 盒反查得到，不手抄 —— 手抄 256 个字节是这份文件里最可能出错的地方。
_inv = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _inv[_v] = _i
_INV_SBOX = bytes(_inv)
del _i, _v, _inv


def _xtime(a: int) -> int:
    """GF(2^8) 上乘 2（模不可约多项式 0x11B）。"""
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """GF(2^8) 上的一般乘法 —— 只有逆列的 9/11/13/14 用到它。"""
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _expand_key(key: bytes) -> list[int]:
    """AES-128 密钥扩展：16 字节 → 176 字节（11 组轮密钥）。"""
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须是 16 字节，收到 {len(key)}")
    w = list(key)
    rcon = 1
    for i in range(16, 176, 4):
        temp = w[i - 4:i]
        if i % 16 == 0:
            temp = [_SBOX[b] for b in temp[1:] + temp[:1]]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        w.extend(w[i - 16 + j] ^ temp[j] for j in range(4))
    return w


def _build_te_tables() -> tuple:
    """T 表（Te0..Te3）：把「SubBytes + ShiftRows + MixColumns」合成一次查表 + 异或。

    为什么值得：纯 Python 的 AES 每个块要跑 10 轮 × 4 次列表推导，实测 **2.4–7 秒/MiB**，
    20 MiB 的文件要两分多钟 —— 而这段时间是同步占着桥那唯一一条工作线程的。
    合成表之后每轮只剩 16 次查表 + 异或，实测快 5 倍以上，而字节结果不变
    （由 `tests/test_channels_aes.py` 里的 FIPS-197 / SP 800-38A 向量守着）。
    """
    te0, te1, te2, te3 = [], [], [], []
    for x in range(256):
        s = _SBOX[x]
        s2 = _xtime(s)
        s3 = s2 ^ s
        te0.append((s2 << 24) | (s << 16) | (s << 8) | s3)
        te1.append((s3 << 24) | (s2 << 16) | (s << 8) | s)
        te2.append((s << 24) | (s3 << 16) | (s2 << 8) | s)
        te3.append((s << 24) | (s << 16) | (s3 << 8) | s2)
    return tuple(te0), tuple(te1), tuple(te2), tuple(te3)


_TE0, _TE1, _TE2, _TE3 = _build_te_tables()


def _build_mul_tables() -> tuple:
    """InvMixColumns 的四张乘法表（×9 / ×11 / ×13 / ×14）。

    解密没有「逆 T 表」那么好合：Arg 是 `_inv_mix_columns` 每列要做 16 次
    `_mul`，而 `_mul` 自己是一个循环。实测朴素路径 **34.8 秒/MiB**（加密是 0.55），
    一张 10 MiB 的入站图片会把长轮询线程按住六分钟。把四个乘数查成表之后每列只剩
    16 次查表 + 异或，字节结果一字不差（FIPS-197 / SP 800-38A 向量守着）。
    """
    return (tuple(_mul(x, 9) for x in range(256)),
            tuple(_mul(x, 11) for x in range(256)),
            tuple(_mul(x, 13) for x in range(256)),
            tuple(_mul(x, 14) for x in range(256)))


_M9, _M11, _M13, _M14 = _build_mul_tables()


def _round_words(round_keys: list[int]) -> list[int]:
    """把 176 字节的轮密钥压成 44 个 32 位字（大端）—— T 表实现按字异或。"""
    return [(round_keys[i] << 24) | (round_keys[i + 1] << 16)
            | (round_keys[i + 2] << 8) | round_keys[i + 3]
            for i in range(0, 176, 4)]


def _inv_shift_rows(state: list[int]) -> None:
    """第 r 行循环右移 r 位（解密用）。"""
    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            state[r + 4 * c] = row[c]


def _inv_mix_columns(state: list[int]) -> None:
    """逆行混合 —— 四个乘数查表（见 `_build_mul_tables`），不做原地乘法。"""
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3]
        state[i + 1] = _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3]
        state[i + 2] = _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3]
        state[i + 3] = _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]


def _encrypt_words(words: list[int], block: bytes) -> bytes:
    """单块加密（T 表版）；`words` 是 `_round_words()` 的结果。"""
    s0 = ((block[0] << 24) | (block[1] << 16) | (block[2] << 8) | block[3]) ^ words[0]
    s1 = ((block[4] << 24) | (block[5] << 16) | (block[6] << 8) | block[7]) ^ words[1]
    s2 = ((block[8] << 24) | (block[9] << 16) | (block[10] << 8) | block[11]) ^ words[2]
    s3 = ((block[12] << 24) | (block[13] << 16) | (block[14] << 8) | block[15]) ^ words[3]
    for rnd in range(1, 10):
        b = 4 * rnd
        t0 = (_TE0[s0 >> 24] ^ _TE1[(s1 >> 16) & 255]
              ^ _TE2[(s2 >> 8) & 255] ^ _TE3[s3 & 255] ^ words[b])
        t1 = (_TE0[s1 >> 24] ^ _TE1[(s2 >> 16) & 255]
              ^ _TE2[(s3 >> 8) & 255] ^ _TE3[s0 & 255] ^ words[b + 1])
        t2 = (_TE0[s2 >> 24] ^ _TE1[(s3 >> 16) & 255]
              ^ _TE2[(s0 >> 8) & 255] ^ _TE3[s1 & 255] ^ words[b + 2])
        t3 = (_TE0[s3 >> 24] ^ _TE1[(s0 >> 16) & 255]
              ^ _TE2[(s1 >> 8) & 255] ^ _TE3[s2 & 255] ^ words[b + 3])
        s0, s1, s2, s3 = t0, t1, t2, t3
    # 末轮没有列混淆：只剩 SubBytes + ShiftRows + AddRoundKey
    o0 = (((_SBOX[s0 >> 24] << 24) | (_SBOX[(s1 >> 16) & 255] << 16)
           | (_SBOX[(s2 >> 8) & 255] << 8) | _SBOX[s3 & 255]) ^ words[40])
    o1 = (((_SBOX[s1 >> 24] << 24) | (_SBOX[(s2 >> 16) & 255] << 16)
           | (_SBOX[(s3 >> 8) & 255] << 8) | _SBOX[s0 & 255]) ^ words[41])
    o2 = (((_SBOX[s2 >> 24] << 24) | (_SBOX[(s3 >> 16) & 255] << 16)
           | (_SBOX[(s0 >> 8) & 255] << 8) | _SBOX[s1 & 255]) ^ words[42])
    o3 = (((_SBOX[s3 >> 24] << 24) | (_SBOX[(s0 >> 16) & 255] << 16)
           | (_SBOX[(s1 >> 8) & 255] << 8) | _SBOX[s2 & 255]) ^ words[43])
    return (o0.to_bytes(4, "big") + o1.to_bytes(4, "big")
            + o2.to_bytes(4, "big") + o3.to_bytes(4, "big"))


def _encrypt_block(round_keys: list[int], block: bytes) -> bytes:
    """单块加密；`round_keys` 是 `_expand_key()` 的 176 字节展开结果。"""
    return _encrypt_words(_round_words(round_keys), block)


def _decrypt_block(round_keys: list[int], block: bytes) -> bytes:
    base = 160
    state = [block[i] ^ round_keys[base + i] for i in range(16)]
    for rnd in range(9, -1, -1):
        _inv_shift_rows(state)
        state = [_INV_SBOX[b] for b in state]
        base = 16 * rnd
        state = [state[i] ^ round_keys[base + i] for i in range(16)]
        if rnd:
            _inv_mix_columns(state)
    return bytes(state)


def pad(data: bytes) -> bytes:
    """PKCS#7 补位：**永远**补（整块时补一整块）—— 这是规范要求，去掉它会少一个分支。"""
    n = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return data + bytes([n]) * n


def unpad(data: bytes) -> bytes:
    """去掉 PKCS#7 补位；补位不合法就抛 `ValueError`（不猜、不忍）。"""
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError(f"解密结果不是整块（{len(data)} 字节）")
    n = data[-1]
    if n < 1 or n > BLOCK_SIZE or data[-n:] != bytes([n]) * n:
        raise ValueError("PKCS#7 补位不合法")
    return data[:-n]


def encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密并补位（iLink 上传要求的就是这个）。

    输出**预分配**：加密前就知道要写多少字节，逐块填进去，不累积 `bytearray`
    （20 MiB 的文件上那样要多一次整份拷贝）。
    """
    words = _round_words(_expand_key(key))
    body = pad(data)
    out = bytearray(len(body))
    view = memoryview(out)
    for off in range(0, len(body), BLOCK_SIZE):
        view[off:off + BLOCK_SIZE] = _encrypt_words(words, body[off:off + BLOCK_SIZE])
    return bytes(out)


def decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密并去掉补位（iLink 下载回来的媒体是这种）。"""
    if len(data) % BLOCK_SIZE:
        raise ValueError(f"密文不是整块（{len(data)} 字节）")
    round_keys = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data), BLOCK_SIZE):
        out += _decrypt_block(round_keys, data[off:off + BLOCK_SIZE])
    return unpad(bytes(out))
