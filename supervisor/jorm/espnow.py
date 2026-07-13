"""ESP-NOW transport (SPEC-three) — the bus over the MAC layer, no lwIP.

Carries the same JSON bus messages a WiFi leaf sends over a WebSocket, but as sealed,
fragmented ESP-NOW frames to a peer MAC. A message is:

    seal(counter ‖ text)  →  fragment into ≤240-byte frames  →  asend each, ACK-retried

and on the way in, reassembled by (peer, msg_id), unsealed (authenticity checked), and
the per-peer counter checked for replay. Peers are UNENCRYPTED at the ESP-NOW layer —
confidentiality and integrity are the sealer's job (jorm/seal.py), which is how we dodge
the 6-encrypted-peer cap.

The frame header is 5 bytes: type, msg_id (2, little-endian), fragment index, count.
"""
import asyncio
import time

import aioespnow

from jorm.seal import Sealer

BROADCAST = b'\xff\xff\xff\xff\xff\xff'
_MAXFRAG = 240          # payload per frame: ESP-NOW ~250 minus the 5-byte header
_HDR = 5
_REASM_TTL_MS = 4000
_SEND_TRIES = 4

T_MSG = 1               # a sealed, possibly-fragmented bus message
T_HELLO = 2             # discovery (gateway -> broadcast), a later slice


class EspNowLink:
    def __init__(self, token):
        self.e = aioespnow.AIOESPNow()
        self.e.active(True)
        self._seal = Sealer(token)
        self._next_id = 0
        self._reasm = {}          # (mac, msg_id) -> {'count','frags','t'}
        self._tx_ctr = {}         # mac -> next outbound counter
        self._rx_ctr = {}         # mac -> highest inbound counter seen

    def mac(self):
        import network
        return network.WLAN(network.STA_IF).config('mac')

    def add_peer(self, mac):
        try:
            self.e.add_peer(mac)
        except OSError:
            pass                  # already a peer

    # -- send ---------------------------------------------------------------

    async def send(self, mac, text):
        """Seal, fragment, and send. Returns True only if every frame was ACKed."""
        ctr = self._tx_ctr.get(mac, 1)
        self._tx_ctr[mac] = ctr + 1
        blob = self._seal.seal(ctr.to_bytes(8, 'big') + text.encode())
        count = (len(blob) + _MAXFRAG - 1) // _MAXFRAG
        mid = self._next_id & 0xFFFF
        self._next_id += 1
        all_acked = True
        for i in range(count):
            frag = blob[i * _MAXFRAG:(i + 1) * _MAXFRAG]
            frame = bytes([T_MSG, mid & 0xFF, (mid >> 8) & 0xFF, i, count]) + frag
            acked = False
            for _ in range(_SEND_TRIES):
                try:
                    acked = await self.e.asend(mac, frame, True)
                except OSError:
                    acked = False
                if acked:
                    break
                await asyncio.sleep_ms(20)
            all_acked = all_acked and acked
        return all_acked

    # -- receive ------------------------------------------------------------

    async def recv(self):
        """Await the next complete, authentic, non-replayed message: (mac, text)."""
        while True:
            mac, frame = await self.e.arecv()
            if not frame or len(frame) < _HDR or frame[0] != T_MSG:
                continue
            mid = frame[1] | (frame[2] << 8)
            idx, count = frame[3], frame[4]
            if count == 0 or idx >= count:
                continue
            key = (bytes(mac), mid)
            slot = self._reasm.get(key)
            if slot is None or slot['count'] != count:
                slot = {'count': count, 'frags': {}, 't': time.ticks_ms()}
                self._reasm[key] = slot
            slot['frags'][idx] = frame[_HDR:]
            self._reap()
            if len(slot['frags']) != count:
                continue
            del self._reasm[key]
            blob = b''.join(slot['frags'][i] for i in range(count))
            pt = self._seal.unseal(blob)
            if pt is None or len(pt) < 8:
                continue          # forged, corrupt, or too short — drop silently
            ctr = int.from_bytes(pt[:8], 'big')
            if ctr <= self._rx_ctr.get(bytes(mac), 0):
                continue          # replay or reorder — drop
            self._rx_ctr[bytes(mac)] = ctr
            return bytes(mac), pt[8:].decode()

    def _reap(self):
        now = time.ticks_ms()
        for k in list(self._reasm):
            if time.ticks_diff(now, self._reasm[k]['t']) > _REASM_TTL_MS:
                del self._reasm[k]   # a lost fragment must not pin memory
