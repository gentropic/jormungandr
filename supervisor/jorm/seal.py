"""App-layer authenticated encryption for ESP-NOW (SPEC-three §6, option B).

ESP-NOW's built-in AES-CCM caps at 6 encrypted peers; this seals the payload in
software instead, over unencrypted peers, with no such cap. It is the standard
Encrypt-then-MAC construction, followed to the letter — rolling your own here is where
people cut themselves, so nothing is improvised:

    seal   = IV ‖ AES-CBC(k_enc, IV, pad(plaintext)) ‖ HMAC-SHA256(k_mac, IV‖ct)[:8]
    unseal = verify the tag FIRST, in constant time; only then decrypt.

Two keys, separately derived from the shared cluster token — one for the cipher, one
for the MAC. Key separation is not optional. The primitives (cryptolib AES-CBC,
hashlib.sha256, os.urandom) are all present on the C3; CTR is not compiled in, so this
is CBC-then-MAC, the equally-standard variant.

Replay is NOT this module's job — it seals and unseals bytes. The transport prepends a
per-peer counter to the plaintext before sealing, so the counter is authenticated, and
checks it after unsealing.
"""
import cryptolib
import hashlib
import os

_BLOCK = 16
_IV = 16
_TAG = 8


def _hmac_sha256(key, msg):
    # No hmac module in MicroPython; this is the textbook construction.
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b'\x00' * (64 - len(key))
    ipad = bytes(b ^ 0x36 for b in key)
    opad = bytes(b ^ 0x5c for b in key)
    inner = hashlib.sha256(ipad + msg).digest()
    return hashlib.sha256(opad + inner).digest()


def _ct_eq(a, b):
    # Constant-time compare: a mismatch must not leak WHERE via timing.
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a, b):
        r |= x ^ y
    return r == 0


def _pad(data):
    n = _BLOCK - (len(data) % _BLOCK)      # PKCS7; n in 1..16, always adds a block
    return data + bytes([n]) * n


def _unpad(data):
    if not data or len(data) % _BLOCK:
        return None
    n = data[-1]
    if n < 1 or n > _BLOCK or n > len(data):
        return None
    if data[-n:] != bytes([n]) * n:        # every pad byte must be n
        return None
    return data[:-n]


class Sealer:
    def __init__(self, token):
        if isinstance(token, str):
            token = token.encode()
        # Labeled-HMAC key derivation: distinct labels give independent keys.
        self._k_enc = _hmac_sha256(token, b'jorm-espnow-enc/v1')[:16]   # AES-128
        self._k_mac = _hmac_sha256(token, b'jorm-espnow-mac/v1')        # 32 bytes

    def seal(self, plaintext):
        iv = os.urandom(_IV)
        ct = cryptolib.aes(self._k_enc, 2, iv).encrypt(_pad(plaintext))
        tag = _hmac_sha256(self._k_mac, iv + ct)[:_TAG]
        return iv + ct + tag

    def unseal(self, blob):
        # Reject anything too short, or a ciphertext that is not whole blocks, before
        # touching the cipher.
        if len(blob) < _IV + _BLOCK + _TAG:
            return None
        iv, tag = blob[:_IV], blob[-_TAG:]
        ct = blob[_IV:-_TAG]
        if len(ct) % _BLOCK:
            return None
        if not _ct_eq(tag, _hmac_sha256(self._k_mac, iv + ct)[:_TAG]):
            return None                    # not authentic — do not decrypt
        return _unpad(cryptolib.aes(self._k_enc, 2, iv).decrypt(ct))
