import time


class Ring:
    """Structured log ring: (ts, level, text) tuples, JSON only at the boundary."""

    def __init__(self, size=200, echo=True):
        self._size = size
        self._echo = echo
        self._lines = []

    def append(self, level, text):
        line = (time.time(), level, str(text))
        self._lines.append(line)
        if len(self._lines) > self._size:
            self._lines.pop(0)
        if self._echo:
            print('[%s] %s' % (level, text))

    def tail(self, n=50):
        return [{'ts': ts, 'level': lv, 'text': tx} for ts, lv, tx in self._lines[-n:]]
