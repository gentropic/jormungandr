#!/usr/bin/env python3
"""Manage a jormungandr leaf over its sealed-UDP door (jorm/leafapi.py).

A leaf has no HTTP server; this sends a token-sealed datagram to its management port and
prints the sealed reply. The seal here mirrors jorm/seal.py byte-for-byte so the board and
this client speak the same language.

    uv run --with pycryptodome python tools/leafctl.py <host> <op> [--token T] [--n N]

Ops (slice 1): ping | state | log
"""
import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import socket
import sys

from Crypto.Cipher import AES   # pycryptodome — raw AES-CBC, matching cryptolib on the board

PORT = 5355
CHUNK = 512                     # bytes per put — leaves margin under a 1472 B WiFi MTU


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
    ap.add_argument('op', choices=('ping', 'state', 'log', 'start', 'stop', 'restart',
                                   'install', 'reboot'))
    ap.add_argument('guest', nargs='?',
                    help='guest id (start/stop/restart), or local bundle dir (install)')
    ap.add_argument('--token', default=os.environ.get('JORM_TOKEN'))
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--n', type=int, default=20, help='log: number of lines')
    ap.add_argument('--timeout', type=float, default=3.0)
    a = ap.parse_args()
    if not a.token:
        sys.exit('need --token or JORM_TOKEN')

    seal = Sealer(a.token)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(a.timeout)

    def rpc(payload, soft=False):
        s.sendto(seal.seal(json.dumps(payload).encode()), (a.host, a.port))
        try:
            data, _ = s.recvfrom(8192)
        except socket.timeout:
            if soft:
                return None
            sys.exit('no reply from %s:%d (unreachable, or wrong token — sealed drops are silent)'
                     % (a.host, a.port))
        pt = seal.unseal(data)
        if pt is None:
            if soft:
                return None
            sys.exit('reply did not authenticate (wrong token?)')
        return json.loads(pt)

    def nonce():
        # single-use, server-issued challenge so a captured mutating datagram can't be replayed
        nr = rpc({'op': 'nonce'})
        if not nr.get('nonce'):
            sys.exit('could not obtain a nonce: %s' % nr)
        return nr['nonce']

    def upload(local, remote):
        with open(local, 'rb') as f:
            data = f.read()
        crc = binascii.crc32(data) & 0xffffffff
        total, off = len(data), 0
        while True:
            chunk = data[off:off + CHUNK]
            put = {'op': 'put', 'path': remote, 'off': off, 'total': total,
                   'data': base64.b64encode(chunk).decode()}
            if off + len(chunk) >= total:
                put['crc'] = crc
            r = None
            for _ in range(6):                       # retransmit on loss/reorder
                r = rpc(put, soft=True)
                if r is not None:
                    break
            if r is None:
                sys.exit('upload of %s stalled at offset %d (no ack)' % (remote, off))
            if not r.get('ok'):
                sys.exit('put %s: %s' % (remote, r.get('err')))
            off = r['next']
            if r.get('done') or off >= total:
                break

    if a.op == 'install':
        bundle = a.guest
        if not bundle or not os.path.isdir(bundle):
            sys.exit('install needs a local bundle directory (with manifest.json)')
        m = json.load(open(os.path.join(bundle, 'manifest.json')))
        gid = m['id']
        names = ['manifest.json', m.get('entry', 'main.py')]
        for name in sorted(os.listdir(bundle)):      # any extra bundle files, not hidden
            p = os.path.join(bundle, name)
            if name not in names and os.path.isfile(p) and not name.startswith('.'):
                names.append(name)
        for name in names:
            remote = 'guests/%s/%s' % (gid, name)
            upload(os.path.join(bundle, name), remote)
            print('  uploaded %s' % remote, file=sys.stderr)
        print(json.dumps(rpc({'op': 'install', 'id': gid, 'nonce': nonce()}), indent=2))
        return

    req = {'op': a.op}
    if a.op == 'log':
        req['n'] = a.n
    if a.op in ('start', 'stop', 'restart'):
        if not a.guest:
            sys.exit('%s needs a guest id' % a.op)
        req['guest'] = a.guest
        req['nonce'] = nonce()
    if a.op == 'reboot':
        req['nonce'] = nonce()
    print(json.dumps(rpc(req), indent=2))


if __name__ == '__main__':
    main()
