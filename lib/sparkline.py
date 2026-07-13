"""A shared library, installable on a live node with `jorm lib --install`.

Stateless at module level, per the §1 convention: classes and functions only.
Python caches a module once and hands every importer the same object, so a
mutable module-level global here would be a cross-guest leak. Instances live in
the guest that made them.
"""

BLOCKS = ' ▁▂▃▄▅▆▇█'


class Trend:
    """A tiny rolling series with a text sparkline — the poor node's chart."""

    def __init__(self, size=16):
        self.size = size
        self.values = []

    def push(self, v):
        self.values.append(v)
        if len(self.values) > self.size:
            self.values.pop(0)
        return self

    def spark(self):
        if not self.values:
            return ''
        lo, hi = min(self.values), max(self.values)
        span = (hi - lo) or 1
        return ''.join(BLOCKS[int((v - lo) / span * (len(BLOCKS) - 1))]
                       for v in self.values)

    def mean(self):
        return sum(self.values) / len(self.values) if self.values else 0

    def last(self):
        return self.values[-1] if self.values else None
