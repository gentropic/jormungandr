#!/usr/bin/env python3
"""Manage a jormungandr leaf over its sealed-UDP door (jorm/leafapi.py).

A leaf has no HTTP server; this sends a token-sealed datagram to its management port and
prints the sealed reply. The seal here mirrors jorm/seal.py byte-for-byte so the board and
this client speak the same language.

    uv run --with pycryptodome python tools/leafctl.py <host> <op> [--token T] [--n N]

Ops (slice 1): ping | state | log
"""
import argparse
import hashlib
import hmac
import json
import os
import socket
import sys

from Crypto.Cipher import AES   # pycryptodome — raw AES-CBC, matching cryptolib on the board

PORT = 5355


class Sealer:
    """CPython twin of jorm.seal.Sealer: IV ‖ AES-CBC(k_enc) ‖ HMAC-SHA256(k_mac)[:8]."""

    def __init__(self, token):
        if isinstance(token, str):
            token = token.encode()
        self.k_enc = hmac.new(token, b'jorm-espnow-enc/v1', hashlib.sha256).digest()[:16]
        self.k_mac = hmac.new(token, b'jorm-espnow-mac/v1', hashlib.sha256).digest()

    @staticmethod
    def _pad(d):
        n = 16 - (len(d) % 16)
        return d + bytes([n]) * n

    @staticmethod
    def _unpad(d):
        if not d or len(d) % 16:
            return None
        n = d[-1]
        if n < 1 or n > 16 or n > len(d) or d[-n:] != bytes([n]) * n:
            return None
        return d[:-n]

    def seal(self, pt):
        iv = os.urandom(16)
        ct = AES.new(self.k_enc, AES.MODE_CBC, iv).encrypt(self._pad(pt))
        tag = hmac.new(self.k_mac, iv + ct, hashlib.sha256).digest()[:8]
        return iv + ct + tag

    def unseal(self, blob):
        if len(blob) < 16 + 16 + 8:
            return None
        iv, tag, ct = blob[:16], blob[-8:], blob[16:-8]
        if len(ct) % 16:
            return None
        if not hmac.compare_digest(tag, hmac.new(self.k_mac, iv + ct, hashlib.sha256).digest()[:8]):
            return None
        return self._unpad(AES.new(self.k_enc, AES.MODE_CBC, iv).decrypt(ct))


def main():
    ap = argparse.ArgumentParser(description='sealed-UDP leaf management')
    ap.add_argument('host')
    ap.add_argument('op', choices=('ping', 'state', 'log'))
    ap.add_argument('--token', default=os.environ.get('JORM_TOKEN'))
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--n', type=int, default=20, help='log: number of lines')
    ap.add_argument('--timeout', type=float, default=3.0)
    a = ap.parse_args()
    if not a.token:
        sys.exit('need --token or JORM_TOKEN')

    seal = Sealer(a.token)
    req = {'op': a.op}
    if a.op == 'log':
        req['n'] = a.n

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(a.timeout)
    s.sendto(seal.seal(json.dumps(req).encode()), (a.host, a.port))
    try:
        data, _ = s.recvfrom(8192)
    except socket.timeout:
        sys.exit('no reply from %s:%d (unreachable, or wrong token — sealed drops are silent)'
                 % (a.host, a.port))
    pt = seal.unseal(data)
    if pt is None:
        sys.exit('reply did not authenticate (wrong token?)')
    print(json.dumps(json.loads(pt), indent=2))


if __name__ == '__main__':
    main()
