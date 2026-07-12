import asyncio
import time


class Tap:
    """Bounded drop-oldest stream endpoint (spec §5): a slow consumer loses
    its own messages and nothing else, and the drops are counted."""

    def __init__(self, qlen=16):
        self.qlen = qlen
        self.items = []
        self.drops = 0
        self._ev = asyncio.Event()

    def push(self, item):
        if len(self.items) >= self.qlen:
            self.items.pop(0)
            self.drops += 1
        self.items.append(item)
        self._ev.set()

    async def get(self):
        while not self.items:
            self._ev.clear()
            await self._ev.wait()
        return self.items.pop(0)


class Ring:
    """Structured log ring: (ts, level, text) tuples, JSON only at the boundary."""

    def __init__(self, size=200, echo=True):
        self._size = size
        self._echo = echo
        self._lines = []
        self._taps = []

    def append(self, level, text):
        line = (time.time(), level, str(text))
        self._lines.append(line)
        if len(self._lines) > self._size:
            self._lines.pop(0)
        for tap in self._taps:
            tap.push(line)
        if self._echo:
            print('[%s] %s' % (level, text))

    def tail(self, n=50):
        return [{'ts': ts, 'level': lv, 'text': tx} for ts, lv, tx in self._lines[-n:]]

    def tap(self, qlen=64):
        tap = Tap(qlen)
        self._taps.append(tap)
        return tap

    def untap(self, tap):
        if tap in self._taps:
            self._taps.remove(tap)
