"""`channels/_aes.py` 的正确性测试 —— 靠 NIST 向量，不靠「看起来对」。

为什么要单独一个测试文件：AES 错一位**不会报错**。长度对、不抛异常、加密再解密
甚至可能自洽（如果加密和解密错得一样），只是对面解不开，而那时的现象是「对方看到
一张灰图」，排查会花掉一整天。所以这里钉的是规范附录里的字节值。
"""
from __future__ import annotations

import pytest

from openthree.channels import _aes

# FIPS-197 附录 C.1：AES-128 单块
FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CIPHER = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

# SP 800-38A 附录 F.1.1：ECB-AES128 四个块
SP_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
SP_PLAIN = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"
                         "ae2d8a571e03ac9c9eb76fac45af8e51"
                         "30c81c46a35ce411e5fbc1191a0a52ef"
                         "f69f2445df4f9b17ad2b417be66c3710")
SP_CIPHER = bytes.fromhex("3ad77bb40d7a3660a89ecaf32466ef97"
                          "f5d3d58503b9699de785895a96fdbaaf"
                          "43b1cd7f598ece23881b00e3ed030688"
                          "7b0c785e27e8ad3f8223207104725dd4")


def test_aes128_single_block_matches_fips197():
    rk = _aes._expand_key(FIPS_KEY)
    assert _aes._encrypt_block(rk, FIPS_PLAIN) == FIPS_CIPHER


def test_aes128_decrypt_block_is_the_inverse():
    rk = _aes._expand_key(FIPS_KEY)
    assert _aes._decrypt_block(rk, FIPS_CIPHER) == FIPS_PLAIN


def test_ecb_four_blocks_match_sp800_38a():
    rk = _aes._expand_key(SP_KEY)
    got = b"".join(_aes._encrypt_block(rk, SP_PLAIN[i:i + 16])
                   for i in range(0, len(SP_PLAIN), 16))
    assert got == SP_CIPHER


def test_full_ecb_vector_with_padding():
    """`encrypt()` 带 PKCS#7 补位，所以 64 字节原文会多出一整块 16 字节。"""
    out = _aes.encrypt(SP_PLAIN, SP_KEY)
    assert out[:64] == SP_CIPHER          # 前四块与规范逐字节相同
    assert len(out) == 80


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 255, 1000])
def test_encrypt_decrypt_round_trip(size):
    data = bytes((i * 7) % 256 for i in range(size))
    enc = _aes.encrypt(data, FIPS_KEY)
    assert len(enc) % _aes.BLOCK_SIZE == 0
    assert len(enc) > size                # 永远补位
    assert _aes.decrypt(enc, FIPS_KEY) == data


def test_pad_always_adds_a_full_block_when_already_aligned():
    assert len(_aes.pad(b"x" * 16)) == 32
    assert len(_aes.pad(b"x" * 15)) == 16


def test_unpad_rejects_garbage():
    with pytest.raises(ValueError):
        _aes.unpad(b"")
    with pytest.raises(ValueError):
        _aes.unpad(b"123")               # 不是整块
    with pytest.raises(ValueError):
        _aes.unpad(b"0123456789abcdef")  # 末字节 0x66：补位不合法


def test_decrypt_rejects_partial_blocks():
    with pytest.raises(ValueError):
        _aes.decrypt(b"", FIPS_KEY)
    with pytest.raises(ValueError):
        _aes.decrypt(b"123", FIPS_KEY)


def test_wrong_key_length_is_rejected():
    with pytest.raises(ValueError):
        _aes._expand_key(b"short")
    with pytest.raises(ValueError):
        _aes.encrypt(b"data", b"0123456789abcdefg")   # 17 字节
