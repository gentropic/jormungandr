# Fragment-retransmit acceptance (SPEC-three §4) for jorm/espnow.py.
#
# The flaky C3 link can't PROVE the NAK path fires — a healthy link never drops a
# fragment, and a flaky one drops unpredictably. So we stub the radio with an in-memory
# medium that drops exactly the fragments we choose, and check the receiver notices the
# gap, asks for just those fragments, and completes the message. Runs on the unix port
# (no radio); the seal, fragmentation, NAK, reassembly, and replay logic are all real —
# only aioespnow is faked.
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
MAC_A = b'\x02\x00\x00\x00\x00\xaa'
MAC_B = b'\x02\x00\x00\x00\x00\xbb'

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


async def _deliver(text, timeout=3.0):
    """A sends text to B; B receives. A background A.recv() services B's NAKs."""
    a, b = _pair()
    servicer = asyncio.create_task(a.recv())     # only ever sees NAKs -> never returns
    await a.send(MAC_B, text)
    try:
        _mac, got = await asyncio.wait_for(b.recv(), timeout)
    except asyncio.TimeoutError:
        got = None
    servicer.cancel()
    return got


async def main():
    big = 'x' * 1100                              # ~5 fragments once sealed
    ok((len(big.encode()) + 24) // 240 >= 4, 'test payload spans several fragments')

    # 1. clean link: arrives, and nobody had to ask for anything
    got = await _deliver(big)
    ok(got == big, 'multi-fragment message arrives on a clean link')
    ok(MEDIUM.naks == 0, 'clean link sent no NAKs')

    # 2. one fragment eaten once: the receiver asks, the sender resends, it completes
    a, b = _pair()
    MEDIUM.drop_counts = {2: 1}                   # eat fragment #2 exactly once
    servicer = asyncio.create_task(a.recv())
    await a.send(MAC_B, big)
    _mac, got = await asyncio.wait_for(b.recv(), 3.0)
    servicer.cancel()
    ok(got == big, 'a dropped fragment is recovered by NAK')
    ok(MEDIUM.naks >= 1, 'the recovery actually went through a NAK (%d)' % MEDIUM.naks)

    # 3. several fragments eaten once each: still recovers
    a, b = _pair()
    MEDIUM.drop_counts = {0: 1, 1: 1, 3: 1}
    servicer = asyncio.create_task(a.recv())
    await a.send(MAC_B, big)
    _mac, got = await asyncio.wait_for(b.recv(), 3.0)
    servicer.cancel()
    ok(got == big, 'multiple dropped fragments all recovered')

    # 4. a fragment lost for good: retransmit gives up, message does NOT arrive
    a, b = _pair()
    MEDIUM.drop_forever = {2}
    servicer = asyncio.create_task(a.recv())
    await a.send(MAC_B, big)
    try:
        await asyncio.wait_for(b.recv(), 3.0)
        arrived = True
    except asyncio.TimeoutError:
        arrived = False
    servicer.cancel()
    ok(not arrived, 'a permanently-lost fragment is given up on, not retried forever')
    ok(MEDIUM.naks <= 5, 'give-up bounded the NAKs (%d, cap is a few tries)' % MEDIUM.naks)

    # 5. a single-frame message needs no buffer and no NAK
    got = await _deliver('short and sweet')
    ok(got == 'short and sweet', 'single-frame message still works')
    ok(MEDIUM.naks == 0, 'single-frame message sent no NAKs')

    # 6. replay is still rejected after all this fragment machinery. Capture the frames
    # A sent (kept in its _sent buffer for a multi-frag message), then re-inject them
    # verbatim into B — same session, same counter, so B must NOT surface them twice.
    a, b = _pair()
    servicer = asyncio.create_task(a.recv())
    await a.send(MAC_B, big)
    _m, g1 = await asyncio.wait_for(b.recv(), 2.0)
    ok(g1 == big, 'baseline multi-frag message received before replay check')
    captured = None
    for _key, slot in a._sent.items():
        captured = slot['frames']
    for frame in captured:                        # replay every fragment, unchanged
        MEDIUM.inbox[MAC_B].append((MAC_A, frame))
    try:
        await asyncio.wait_for(b.recv(), 1.5)
        replayed = True
    except asyncio.TimeoutError:
        replayed = False
    servicer.cancel()
    ok(not replayed, 'a verbatim replay of a whole message is rejected (counter)')

    print('\nFRAG: %s' % ('ALL PASS' if not fails else '%d FAILED' % fails))
    sys.exit(1 if fails else 0)


asyncio.run(main())
