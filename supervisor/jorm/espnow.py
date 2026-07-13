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
import os
import time

import aioespnow

from jorm.seal import Sealer

try:
    import network
except ImportError:
    network = None

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
        # A per-boot random session id travels (sealed) with every message, ahead of a
        # per-session counter. Replay protection is "counter must climb WITHIN a
        # session"; a reboot picks a new session, which the receiver adopts — otherwise
        # a rebooted leaf (counter back to 1) is rejected forever by the peer's
        # remembered high-water. Cross-session replay (re-playing a whole old session
        # after a reboot) is a documented residual — a persistent counter closes it.
        self._session = os.urandom(4)
        self._tx_ctr = {}         # mac -> next outbound counter (this session)
        self._rx = {}             # mac -> (peer session, highest counter seen)

    def mac(self):
        return network.WLAN(network.STA_IF).config('mac')

    def add_peer(self, mac, channel=0):
        # channel 0 = the interface's current channel. After a discovery scan the
        # current channel is unreliable for sending, so a discovered peer is pinned to
        # the channel the HELLO came in on.
        try:
            self.e.add_peer(mac, channel=channel)
        except OSError:
            try:
                self.e.mod_peer(mac, channel=channel)   # already a peer: update it
            except OSError:
                pass

    # -- discovery (SPEC-three §5) ------------------------------------------

    async def send_hello(self, cluster):
        """Broadcast a SEALED HELLO. Because the sealer's key is the token-derived
        GROUP key, sealing works over broadcast — and that seals the handshake: only a
        gateway holding the token can produce a HELLO a leaf can unseal, so authenticity
        is free and no separate challenge/response is needed. Replay of a HELLO just
        re-advertises the real gateway, so it carries no counter."""
        self.add_peer(BROADCAST)
        frame = bytes([T_HELLO]) + self._seal.seal(cluster.encode())
        try:
            await self.e.asend(BROADCAST, frame, False)   # broadcast: no ACK to wait on
        except OSError:
            pass

    async def scan_for_gateway(self, cluster, wlan, channels=None, dwell_ms=1500):
        """Hop channels listening for a HELLO for our cluster; return (mac, channel).

        A pure-ESP-NOW leaf does not know the gateway's channel up front (peers must
        share it), so it scans — set each channel, listen briefly, and lock onto the
        first channel that yields a HELLO that unseals to our cluster name."""
        want = cluster.encode()
        for ch in (channels or range(1, 14)):
            try:
                wlan.config(channel=ch)
            except OSError:
                continue
            end = time.ticks_add(time.ticks_ms(), dwell_ms)
            while time.ticks_diff(end, time.ticks_ms()) > 0:
                try:
                    mac, frame = await asyncio.wait_for(self.e.arecv(), 0.3)
                except Exception:
                    continue
                if frame and len(frame) > 1 and frame[0] == T_HELLO:
                    if self._seal.unseal(frame[1:]) == want:
                        return bytes(mac), ch
        return None, None

    # -- send ---------------------------------------------------------------

    async def send(self, mac, text):
        """Seal, fragment, and send. Returns True only if every frame was ACKed."""
        ctr = self._tx_ctr.get(mac, 1)
        self._tx_ctr[mac] = ctr + 1
        blob = self._seal.seal(self._session + ctr.to_bytes(8, 'big') + text.encode())
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
            if pt is None or len(pt) < 12:
                continue          # forged, corrupt, or too short — drop silently
            m = bytes(mac)
            session, ctr = pt[:4], int.from_bytes(pt[4:12], 'big')
            prev = self._rx.get(m)
            if prev is not None and prev[0] == session and ctr <= prev[1]:
                continue          # replay/reorder within a session — drop
            # a new session (peer rebooted) or a higher counter: accept and record
            self._rx[m] = (session, ctr)
            return m, pt[12:].decode()

    def _reap(self):
        now = time.ticks_ms()
        for k in list(self._reasm):
            if time.ticks_diff(now, self._reasm[k]['t']) > _REASM_TTL_MS:
                del self._reasm[k]   # a lost fragment must not pin memory
