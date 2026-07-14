"""ESP-NOW transport (SPEC-three) — the bus over the MAC layer, no lwIP.

Carries the same JSON bus messages a WiFi leaf sends over a WebSocket, but as sealed,
fragmented ESP-NOW frames to a peer MAC. A message is:

    seal(rx_nonce ‖ counter ‖ text)  →  fragment into ≤240-byte frames  →  asend each

reassembled by (peer, msg_id) on the way in, unsealed (authenticity checked), then the
nonce and counter checked for replay. Peers are UNENCRYPTED at the ESP-NOW layer —
confidentiality and integrity are the sealer's job (jorm/seal.py), which is how we dodge
the 6-encrypted-peer cap.

**Replay: receiver-issued nonces (§6).** The `rx_nonce` a message carries is not chosen
by the sender — it is a random nonce the RECEIVER issued during a handshake, and the
receiver accepts only its current outstanding nonce (plus a counter that must climb).
That closes cross-session replay: an old session's frames carry a nonce the receiver has
long since retired, so replaying them — even a whole captured session — is rejected. A
sender-chosen session id could not do this, because the receiver had no way to tell a
legitimately-new session from an old one being replayed. The handshake is a three-frame
JOIN/WELCOME/CONFIRM whose freshness comes from each party echoing a nonce it just
generated; the seal (the token-derived group key) authenticates every frame, so a
non-token-holder can neither forge nor complete it.

Because a MAC-layer ACK now means "the radio got it", not "the app accepted it" (a
rebooted peer ACKs a stale-nonce frame and then drops it), liveness is app-level: a
PING/PONG pair, and `last_rx`, exactly like the WiFi leaf's keepalive.

The data frame header is 5 bytes: type, msg_id (2, little-endian), fragment index, count.
Control frames (HELLO/JOIN/WELCOME/CONFIRM/NAK/PING/PONG) are single, unfragmented.
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
T_HELLO = 2             # discovery (gateway -> broadcast)
T_NAK = 3               # "resend these fragments of msg_id" (receiver -> sender)
T_JOIN = 4              # handshake 1 (initiator -> responder): my nonce for YOUR sends
T_WELCOME = 5           # handshake 2 (responder -> initiator): echo + my nonce for yours
T_CONFIRM = 6           # handshake 3 (initiator -> responder): echo, activate
T_PING = 7              # app-level liveness probe
T_PONG = 8              # app-level liveness reply
T_TIME = 9              # gateway -> broadcast: the cluster's Unix time (no NTP on a leaf)

_SENT_TTL_MS = 4000     # how long a sender keeps fragments around to answer a NAK
_NAK_AFTER_MS = 300     # how long a receiver waits for a gap before asking
_NAK_TRIES = 4


class EspNowLink:
    def __init__(self, token):
        self.e = aioespnow.AIOESPNow()
        self.e.active(True)
        self._seal = Sealer(token)
        self._next_id = 0
        self._reasm = {}          # (mac, msg_id) -> {'count','frags','t',...}
        # Replay state is per peer and per direction. As a SENDER I stamp the nonce the
        # peer issued to me (_tx_nonce) and climb a counter (_tx_ctr). As a RECEIVER I
        # issue a nonce (_rx_nonce) and accept only frames carrying it with a counter
        # above the highest I've seen (_rx_ctr). Both reset at each handshake.
        self._tx_nonce = {}       # mac -> 4-byte nonce to stamp on data I SEND to mac
        self._tx_ctr = {}         # mac -> next outbound counter
        self._rx_nonce = {}       # mac -> 4-byte nonce I issued; data FROM mac must carry it
        self._rx_ctr = {}         # mac -> highest counter accepted under _rx_nonce
        self._pending = {}        # mac -> handshake nonce(s) awaiting a CONFIRM/WELCOME
        # Fragment retransmit: a sender keeps a message's frames briefly so it can
        # answer a receiver's NAK ("resend fragment N"); a background loop on the
        # receiver side asks for gaps. Lazily started on first recv().
        self._sent = {}           # (mac, msg_id) -> {'frames': [...], 't'}
        self._nak_task = None
        self.last_rx = time.ticks_ms()   # app-level liveness: last valid frame in
        self.time_cb = None       # set by a leaf: called with the cluster's Unix time

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

    async def send_hello(self, cluster, channel=0):
        """Broadcast a SEALED HELLO naming the cluster AND the gateway's channel (§5).

        Because the sealer's key is the token-derived GROUP key, sealing works over
        broadcast — and that seals the handshake: only a gateway holding the token can
        produce a HELLO a leaf can unseal, so authenticity is free and no separate
        challenge/response is needed. Replay of a HELLO just re-advertises the real
        gateway, so it carries no counter.

        The channel byte is load-bearing: the leaf must NOT trust which channel its own
        scan loop thinks it is on when it hears this — ESP-NOW buffers received frames,
        so a HELLO caught while dwelling on the gateway's channel can surface a hop or
        two later and be misattributed. The gateway states its channel; the leaf obeys."""
        self.add_peer(BROADCAST)
        frame = bytes([T_HELLO]) + self._seal.seal(cluster.encode() + bytes([channel & 0xFF]))
        try:
            await self.e.asend(BROADCAST, frame, False)   # broadcast: no ACK to wait on
        except OSError:
            pass

    async def send_time(self, ts):
        """Broadcast the cluster's Unix time, sealed with the group key so only a
        token-holder can set a leaf's clock. Any leaf on the channel picks it up and
        needs no NTP of its own — an ESP-NOW leaf has no IP to reach one anyway."""
        self.add_peer(BROADCAST)
        frame = bytes([T_TIME]) + self._seal.seal(int(ts).to_bytes(8, 'big'))
        try:
            await self.e.asend(BROADCAST, frame, False)
        except OSError:
            pass

    def _on_time(self, mac, frame):
        pt = self._seal.unseal(frame[1:])
        if pt is None or len(pt) < 8 or self.time_cb is None:
            return
        try:
            self.time_cb(int.from_bytes(pt[:8], 'big'))
        except Exception:
            pass

    async def scan_for_gateway(self, cluster, wlan, channels=None, dwell_ms=1500):
        """Hop channels listening for a HELLO for our cluster; return (mac, channel).

        A pure-ESP-NOW leaf does not know the gateway's channel up front (peers must
        share it), so it scans — set each channel and listen briefly. The channel we
        return is the one the gateway ADVERTISED in the HELLO, not the one our scan loop
        was on: because ESP-NOW queues received frames, a HELLO can be delivered a hop
        after we heard it, so the loop counter is an unreliable witness (this stranded a
        leaf on the wrong channel). A legacy HELLO with no channel byte falls back to the
        loop's channel."""
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
                    pt = self._seal.unseal(frame[1:])
                    if pt is None or pt[:len(want)] != want:
                        continue
                    adv = pt[len(want)] if len(pt) > len(want) else 0
                    return bytes(mac), (adv or ch)     # advertised channel wins
        return None, None

    # -- handshake (SPEC-three §6): receiver-issued nonces ------------------

    async def handshake(self, mac, tries=5, timeout=1.0):
        """Initiator side (a leaf joining its gateway). Establish fresh nonces both ways.

        We pick `na` — the nonce the gateway must stamp on everything it sends US — and
        send it in a JOIN. The gateway echoes `na` (proving liveness) and adds `nb`, the
        nonce WE must stamp on everything we send IT; we confirm `nb`. A replayed old
        WELCOME carries a stale `na` and is ignored, so we only ever accept a WELCOME
        answering this exact JOIN. Retries a few times: on a lossy link a JOIN or WELCOME
        can drop, and a fresh JOIN just gets a fresh WELCOME."""
        key = bytes(mac)
        for _ in range(tries):
            na = os.urandom(4)
            self._pending[key] = na
            self._rx_nonce[key] = na            # ready to accept na-stamped down-data
            self._rx_ctr[key] = 0
            try:
                await self.e.asend(mac, bytes([T_JOIN]) + self._seal.seal(na), True)
            except OSError:
                pass
            end = time.ticks_add(time.ticks_ms(), int(timeout * 1000))
            while time.ticks_diff(end, time.ticks_ms()) > 0:
                try:
                    m, frame = await asyncio.wait_for(self.e.arecv(), timeout)
                except Exception:
                    break
                if bytes(m) != key or not frame or frame[0] != T_WELCOME:
                    continue
                pt = self._seal.unseal(frame[1:])
                if pt is None or len(pt) < 8 or pt[:4] != na:
                    continue                    # forged, or a replayed stale WELCOME
                nb = pt[4:8]
                self._tx_nonce[key] = nb
                self._tx_ctr[key] = 1
                try:
                    await self.e.asend(mac, bytes([T_CONFIRM]) + self._seal.seal(nb), True)
                except OSError:
                    pass
                self._pending.pop(key, None)
                self.last_rx = time.ticks_ms()
                return True
        return False

    async def _on_join(self, mac, frame):
        """Responder side: a peer offered `na`. Issue `nb`, echo, wait for CONFIRM.

        We change no live state here — only stash (na, nb) as pending. A replayed old
        JOIN thus costs one WELCOME and nothing more: it cannot flip our nonces without a
        CONFIRM, which a non-token-holder cannot seal."""
        pt = self._seal.unseal(frame[1:])
        if pt is None or len(pt) < 4:
            return
        na = pt[:4]
        self.add_peer(mac)                      # so we can answer
        nb = os.urandom(4)
        self._pending[bytes(mac)] = (na, nb)
        try:
            await self.e.asend(mac, bytes([T_WELCOME]) + self._seal.seal(na + nb), True)
        except OSError:
            pass

    def _on_confirm(self, mac, frame):
        """Responder side: the CONFIRM echoes the `nb` we issued — now activate both
        directions atomically (stamp `na` on our sends, accept `nb` on theirs)."""
        pt = self._seal.unseal(frame[1:])
        if pt is None or len(pt) < 4:
            return
        key = bytes(mac)
        pend = self._pending.get(key)
        if not isinstance(pend, tuple) or pend[1] != pt[:4]:
            return
        na, nb = pend
        self._tx_nonce[key] = na
        self._tx_ctr[key] = 1
        self._rx_nonce[key] = nb
        self._rx_ctr[key] = 0
        self._pending.pop(key, None)
        self.last_rx = time.ticks_ms()

    # -- liveness (app-level, because a MAC ACK no longer means "accepted") -

    async def send_ping(self, mac):
        """Provoke a PONG. Stamped like data, so a rebooted peer that has forgotten our
        nonce drops it silently — no PONG — and the sender's `last_rx` goes stale."""
        await self._send_stamped(mac, T_PING)

    async def _on_ping(self, mac, frame):
        if self._check_stamped(mac, frame[1:]) is None:
            return                              # stale/forged: no PONG, they re-handshake
        await self._send_stamped(mac, T_PONG)

    def _on_pong(self, mac, frame):
        self._check_stamped(mac, frame[1:])     # updates last_rx iff it validates

    async def _send_stamped(self, mac, kind):
        key = bytes(mac)
        nonce = self._tx_nonce.get(key)
        if nonce is None:
            return
        ctr = self._tx_ctr.get(key, 1)
        self._tx_ctr[key] = ctr + 1
        body = self._seal.seal(nonce + ctr.to_bytes(8, 'big'))
        try:
            await self.e.asend(mac, bytes([kind]) + body, True)
        except OSError:
            pass

    def _check_stamped(self, mac, blob):
        """Validate a single-frame stamped control body against the receiver state;
        returns the trailing payload (may be b'') if fresh, else None. Updates last_rx."""
        pt = self._seal.unseal(blob)
        if pt is None or len(pt) < 12:
            return None
        m = bytes(mac)
        if self._rx_nonce.get(m) != pt[:4]:
            return None
        ctr = int.from_bytes(pt[4:12], 'big')
        if ctr <= self._rx_ctr.get(m, 0):
            return None
        self._rx_ctr[m] = ctr
        self.last_rx = time.ticks_ms()
        return pt[12:]

    # -- send ---------------------------------------------------------------

    async def send(self, mac, text):
        """Seal, fragment, and send. Returns True only if every frame was ACKed; False
        (sending nothing) if we have no nonce for this peer yet — the caller re-joins."""
        key = bytes(mac)
        nonce = self._tx_nonce.get(key)
        if nonce is None:
            return False
        ctr = self._tx_ctr.get(key, 1)
        self._tx_ctr[key] = ctr + 1
        blob = self._seal.seal(nonce + ctr.to_bytes(8, 'big') + text.encode())
        count = (len(blob) + _MAXFRAG - 1) // _MAXFRAG
        mid = self._next_id & 0xFFFF
        self._next_id += 1
        frames = [bytes([T_MSG, mid & 0xFF, (mid >> 8) & 0xFF, i, count])
                  + blob[i * _MAXFRAG:(i + 1) * _MAXFRAG] for i in range(count)]
        # Keep the frames so a NAK can pull single fragments back (only worth it for a
        # multi-frame message; a single frame either ACKs here or is lost outright).
        if count > 1:
            self._sent[(key, mid)] = {'frames': frames, 't': time.ticks_ms()}
            self._reap()
        all_acked = True
        for frame in frames:
            all_acked = await self._send_frame(mac, frame) and all_acked
        return all_acked

    async def _send_frame(self, mac, frame):
        for _ in range(_SEND_TRIES):
            try:
                if await self.e.asend(mac, frame, True):
                    return True
            except OSError:
                pass
            await asyncio.sleep_ms(20)
        return False

    # -- receive ------------------------------------------------------------

    async def recv(self):
        """Await the next complete, authentic, non-replayed DATA message: (mac, text).

        Control frames (handshake, liveness, NAK) are handled inline and never returned."""
        if self._nak_task is None:
            self._nak_task = asyncio.create_task(self._nak_loop())
        while True:
            mac, frame = await self.e.arecv()
            if not frame:
                continue
            t = frame[0]
            if t == T_NAK:
                await self._answer_nak(bytes(mac), frame)
                continue
            if t == T_JOIN:
                await self._on_join(mac, frame)
                continue
            if t == T_CONFIRM:
                self._on_confirm(mac, frame)
                continue
            if t == T_PING:
                await self._on_ping(mac, frame)
                continue
            if t == T_PONG:
                self._on_pong(mac, frame)
                continue
            if t == T_TIME:
                self._on_time(mac, frame)
                continue
            if t == T_WELCOME:
                continue                        # only meaningful inside handshake()
            if len(frame) < _HDR or t != T_MSG:
                continue
            mid = frame[1] | (frame[2] << 8)
            idx, count = frame[3], frame[4]
            if count == 0 or idx >= count:
                continue
            key = (bytes(mac), mid)
            slot = self._reasm.get(key)
            if slot is None or slot['count'] != count:
                # 'nak_t'/'naks' pace the retransmit asks; 't' paces reap (last progress)
                slot = {'count': count, 'frags': {}, 't': time.ticks_ms(),
                        'nak_t': time.ticks_ms(), 'naks': 0}
                self._reasm[key] = slot
            slot['frags'][idx] = frame[_HDR:]
            slot['t'] = time.ticks_ms()
            self._reap()
            if len(slot['frags']) != count:
                continue
            del self._reasm[key]
            blob = b''.join(slot['frags'][i] for i in range(count))
            pt = self._seal.unseal(blob)
            if pt is None or len(pt) < 12:
                continue          # forged, corrupt, or too short — drop silently
            m = bytes(mac)
            nonce, ctr = pt[:4], int.from_bytes(pt[4:12], 'big')
            if self._rx_nonce.get(m) != nonce:
                continue          # stale session (retired nonce) or never established
            if ctr <= self._rx_ctr.get(m, 0):
                continue          # replay/reorder within the session — drop
            self._rx_ctr[m] = ctr
            self.last_rx = time.ticks_ms()
            return m, pt[12:].decode()

    # -- fragment retransmit ------------------------------------------------

    async def _nak_loop(self):
        """Ask a sender to resend the fragments a message is still missing.

        Per-frame ACK-retry (in send) covers a frame the radio never accepted; this
        covers the rest — a frame that ACKed but was dropped, or lost while our radio
        was elsewhere — which the sender otherwise never learns about. When a slot has
        sat with a gap for a beat, NAK exactly the missing indices; a few times, then
        let the reaper drop it (the app-level req/result retries the whole command)."""
        while True:
            await asyncio.sleep_ms(_NAK_AFTER_MS)
            now = time.ticks_ms()
            for (mac, mid), slot in list(self._reasm.items()):
                if time.ticks_diff(now, slot['nak_t']) < _NAK_AFTER_MS:
                    continue
                if slot['naks'] >= _NAK_TRIES:
                    continue
                missing = [i for i in range(slot['count']) if i not in slot['frags']]
                if not missing:
                    continue
                slot['nak_t'] = now
                slot['naks'] += 1
                nak = bytes([T_NAK, mid & 0xFF, (mid >> 8) & 0xFF, len(missing)]) \
                    + bytes(missing)
                try:
                    await self.e.asend(mac, nak, False)
                except OSError:
                    pass

    async def _answer_nak(self, mac, frame):
        """A receiver asked for fragments of a message we sent — resend just those."""
        if len(frame) < 4:
            return
        mid = frame[1] | (frame[2] << 8)
        want = frame[4:4 + frame[3]]
        slot = self._sent.get((mac, mid))
        if slot is None:
            return                 # already reaped, or never ours
        frames = slot['frames']
        for i in want:
            if i < len(frames):
                await self._send_frame(mac, frames[i])

    def _reap(self):
        now = time.ticks_ms()
        for k in list(self._reasm):
            if time.ticks_diff(now, self._reasm[k]['t']) > _REASM_TTL_MS:
                del self._reasm[k]   # a lost fragment must not pin memory
        for k in list(self._sent):
            if time.ticks_diff(now, self._sent[k]['t']) > _SENT_TTL_MS:
                del self._sent[k]    # past answering a NAK; let it go
