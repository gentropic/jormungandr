# Fragment-retransmit + replay acceptance (SPEC-three §4/§6) for jorm/espnow.py.
#
# The flaky C3 link can't PROVE the NAK path fires — a healthy link never drops a
# fragment, and a flaky one drops unpredictably. So we stub the radio with an in-memory
# medium that drops exactly the fragments we choose, and check the receiver notices the
# gap, asks for just those fragments, and completes the message. The same harness proves
# the receiver-issued-nonce replay defence: a replay within a session and a whole-session
# replay after a fresh handshake are both rejected. Runs on the unix port — the seal,
# fragmentation, handshake, NAK, reassembly, and replay logic are all real; only
# aioespnow is faked.
import sys
import asyncio

sys.path.insert(0, 'supervisor')

# -- a lossy, in-memory ESP-NOW, injected before espnow.py imports aioespnow ----------


class _Medium:
    def __init__(self):
        self.reset()

    def reset(self):
        self.inbox = {}            # mac(bytes) -> [(src_mac, frame)]
        self.drop_counts = {}      # T_MSG fragment idx -> how many more sends to eat
        self.drop_forever = set()  # idx -> dropped every time (models permanent loss)
        self.naks = 0              # T_NAK frames that crossed the medium
        self.delivered = 0


MEDIUM = _Medium()


class AIOESPNow:
    def __init__(self):
        self.mymac = None

    def active(self, *a):
        return True

    def add_peer(self, mac, channel=0):
        pass

    def mod_peer(self, mac, channel=0):
        pass

    async def asend(self, mac, frame, sync=True):
        # ACK is always True — that is the point: a frame can ACK at the radio and
        # still be dropped, which per-frame retry can't see but the NAK can.
        f = bytes(frame)
        if f and f[0] == 3:                       # T_NAK
            MEDIUM.naks += 1
        drop = False
        if f and f[0] == 1 and len(f) >= 5:       # T_MSG: maybe eat this fragment
            idx = f[3]
            if idx in MEDIUM.drop_forever:
                drop = True
            elif MEDIUM.drop_counts.get(idx, 0) > 0:
                MEDIUM.drop_counts[idx] -= 1
                drop = True
        if not drop:
            src = bytes(self.mymac)
            targets = list(MEDIUM.inbox) if mac == b'\xff' * 6 else [bytes(mac)]
            for t in targets:
                if t != src and t in MEDIUM.inbox:
                    MEDIUM.inbox[t].append((src, f))
                    MEDIUM.delivered += 1
        return True

    async def arecv(self):
        box = MEDIUM.inbox[bytes(self.mymac)]
        while not box:
            await asyncio.sleep_ms(3)
        return box.pop(0)


class _FakeAioespnow:
    AIOESPNow = AIOESPNow


sys.modules['aioespnow'] = _FakeAioespnow()

from jorm.espnow import EspNowLink   # noqa: E402  (after the stub is in place)

TOKEN = 'a-shared-cluster-token'
MAC_A = b'\x02\x00\x00\x00\x00\xaa'   # A = leaf (initiator / data sender)
MAC_B = b'\x02\x00\x00\x00\x00\xbb'   # B = gateway (responder / data receiver)

fails = 0


def ok(c, m):
    global fails
    print(('  ok: ' if c else '  FAIL: ') + m)
    if not c:
        fails += 1


def _pair():
    MEDIUM.reset()
    MEDIUM.inbox[MAC_A] = []
    MEDIUM.inbox[MAC_B] = []
    a = EspNowLink(TOKEN)
    a.e.mymac = MAC_A
    b = EspNowLink(TOKEN)
    b.e.mymac = MAC_B
    a.add_peer(MAC_B)
    b.add_peer(MAC_A)
    return a, b


async def _gateway(b, out):
    """B's role: recv() services handshake/ping/NAK inline and yields data into out."""
    while True:
        _m, text = await b.recv()
        out.append(text)


async def _until(cond, secs):
    for _ in range(int(secs * 50)):
        if cond():
            return True
        await asyncio.sleep_ms(20)
    return cond()


async def _session():
    """Fresh pair, B serving, A handshaked. Returns (a, out, gw_task, a_servicer, hs)."""
    a, b = _pair()
    out = []
    gw = asyncio.create_task(_gateway(b, out))
    hs = await a.handshake(MAC_B)
    serv = asyncio.create_task(a.recv())          # A services PONG/NAK
    return a, out, gw, serv, hs


def _frames(a):
    for _k, slot in a._sent.items():              # frames of the last multi-frag message
        return slot['frames']
    return []


async def main():
    big = 'x' * 1100                              # ~5 fragments once sealed
    ok((len(big.encode()) + 24) // 240 >= 4, 'test payload spans several fragments')

    # 0. the handshake itself
    a, out, gw, serv, hs = await _session()
    ok(hs, 'JOIN/WELCOME/CONFIRM handshake completes')
    gw.cancel(); serv.cancel()

    # 1. clean link: arrives, and nobody had to ask for anything
    a, out, gw, serv, _ = await _session()
    await a.send(MAC_B, big)
    ok(await _until(lambda: big in out, 2.0), 'multi-fragment message arrives on a clean link')
    ok(MEDIUM.naks == 0, 'clean link sent no NAKs')
    gw.cancel(); serv.cancel()

    # 2. one fragment eaten once: the receiver asks, the sender resends, it completes
    a, out, gw, serv, _ = await _session()
    MEDIUM.drop_counts = {2: 1}
    await a.send(MAC_B, big)
    ok(await _until(lambda: big in out, 3.0), 'a dropped fragment is recovered by NAK')
    ok(MEDIUM.naks >= 1, 'the recovery actually went through a NAK (%d)' % MEDIUM.naks)
    gw.cancel(); serv.cancel()

    # 3. several fragments eaten once each: still recovers
    a, out, gw, serv, _ = await _session()
    MEDIUM.drop_counts = {0: 1, 1: 1, 3: 1}
    await a.send(MAC_B, big)
    ok(await _until(lambda: big in out, 3.0), 'multiple dropped fragments all recovered')
    gw.cancel(); serv.cancel()

    # 4. a fragment lost for good: retransmit gives up, message does NOT arrive
    a, out, gw, serv, _ = await _session()
    MEDIUM.drop_forever = {2}
    await a.send(MAC_B, big)
    ok(not await _until(lambda: big in out, 3.0),
       'a permanently-lost fragment is given up on, not retried forever')
    ok(MEDIUM.naks <= 5, 'give-up bounded the NAKs (%d, cap is a few tries)' % MEDIUM.naks)
    gw.cancel(); serv.cancel()

    # 5. a single-frame message needs no buffer and no NAK
    a, out, gw, serv, _ = await _session()
    await a.send(MAC_B, 'short and sweet')
    ok(await _until(lambda: 'short and sweet' in out, 2.0), 'single-frame message still works')
    ok(MEDIUM.naks == 0, 'single-frame message sent no NAKs')
    gw.cancel(); serv.cancel()

    # 6. replay WITHIN a session: same nonce, same counter — rejected
    a, out, gw, serv, _ = await _session()
    await a.send(MAC_B, big)
    await _until(lambda: big in out, 2.0)
    captured = list(_frames(a))
    before = len(out)
    for f in captured:
        MEDIUM.inbox[MAC_B].append((MAC_A, f))
    await asyncio.sleep_ms(400)
    ok(len(out) == before, 'an in-session replay is rejected (counter)')
    gw.cancel(); serv.cancel()

    # 7. replay ACROSS sessions: capture a session's frames, re-handshake (fresh nonce),
    # then replay the old frames — the retired nonce means they are rejected. This is the
    # hole a sender-chosen session id could not close.
    a, b = _pair()
    out = []
    gw = asyncio.create_task(_gateway(b, out))
    await a.handshake(MAC_B)
    serv = asyncio.create_task(a.recv())
    await a.send(MAC_B, big)
    await _until(lambda: big in out, 2.0)
    captured = list(_frames(a))
    serv.cancel()                                 # free A's inbox before re-handshaking
    await asyncio.sleep_ms(40)
    hs2 = await a.handshake(MAC_B)                 # B issues a brand-new nonce
    serv = asyncio.create_task(a.recv())
    ok(hs2, 're-handshake issues a fresh nonce')
    before = len(out)
    for f in captured:                            # replay the whole old session verbatim
        MEDIUM.inbox[MAC_B].append((MAC_A, f))
    await asyncio.sleep_ms(400)
    ok(len(out) == before, 'a whole-session replay is rejected after re-handshake')
    gw.cancel(); serv.cancel()

    print('\nFRAG+REPLAY: %s' % ('ALL PASS' if not fails else '%d FAILED' % fails))
    sys.exit(1 if fails else 0)


asyncio.run(main())
