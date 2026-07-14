"""Server-side sealed-UDP door client — one node querying another node's leafapi door.

The flagship reaches a leaf's management door (jorm/leafapi.py) the same way tools/leafctl.py
does — a token-sealed datagram to :5355 — but from inside the supervisor's asyncio loop, so the
web API can present and drive leaves it has no HTTP route to. This is the server-side twin of
leafctl: the same seal (jorm.seal), the same nonce handshake for mutating ops.

Non-blocking throughout: the socket never parks the event loop. A held-open transport is exactly
what starved the leaf's render and wedged the flagship (see spec_inbox/ROADMAP-flagship-leaf-
console); this sends a datagram, polls, and yields — nothing is held.
"""
import asyncio
import json
import socket

from jorm.seal import Sealer

PORT = 5355


class DoorError(Exception):
    """A leaf's door did not answer, or answered with the wrong token."""


class LeafClient:
    def __init__(self, token):
        self._seal = Sealer(token)
        self._tx = None      # lazy reusable send socket for fire-and-forget

    def send(self, host, op, port=PORT, **args):
        """Fire-and-forget: seal + sendto, no reply awaited — for the bus-bridge, where a lost
        datagram just means a coalesced value re-sends next tick. Not async: sendto on a UDP
        socket does not block, so this never parks the loop."""
        req = {'op': op}
        req.update(args)
        blob = self._seal.seal(json.dumps(req).encode())
        if self._tx is None:
            self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._tx.sendto(blob, socket.getaddrinfo(host, port)[0][-1])
        except OSError:
            pass

    async def _rpc(self, host, req, port, timeout_ms, retries):
        blob = self._seal.seal(json.dumps(req).encode())
        addr = socket.getaddrinfo(host, port)[0][-1]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)                              # non-blocking: poll, never park the loop
        try:
            per_try = max(timeout_ms // (retries + 1), 100)
            for _ in range(retries + 1):
                try:
                    s.sendto(blob, addr)
                except OSError:
                    pass
                waited = 0
                while waited < per_try:
                    try:
                        data, _addr = s.recvfrom(8192)
                    except OSError:                  # EAGAIN — nothing waiting yet
                        await asyncio.sleep_ms(20)
                        waited += 20
                        continue
                    pt = self._seal.unseal(data)
                    if pt is None:
                        raise DoorError('reply did not authenticate (wrong token?)')
                    return json.loads(pt)
            raise DoorError('no reply from %s:%d (offline, or wrong token — drops are silent)'
                            % (host, port))
        finally:
            s.close()

    async def query(self, host, op, port=PORT, timeout_ms=3000, **args):
        """A read op (ping/state/log) — no side effect, so no nonce."""
        req = {'op': op}
        req.update(args)
        return await self._rpc(host, req, port, timeout_ms, retries=2)

    async def command(self, host, op, port=PORT, timeout_ms=3000, **args):
        """A mutating op (start/stop/restart/install) — fetch a single-use nonce first, so a
        captured datagram can't be replayed, then send the op carrying it."""
        nr = await self._rpc(host, {'op': 'nonce'}, port, timeout_ms, retries=2)
        if not nr.get('nonce'):
            raise DoorError('could not obtain a nonce: %s' % nr)
        req = {'op': op, 'nonce': nr['nonce']}
        req.update(args)
        return await self._rpc(host, req, port, timeout_ms, retries=2)
