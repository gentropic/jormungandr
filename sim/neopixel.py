# sim stub for MicroPython's `neopixel`. Prints the colour it would have shown,
# so a beacon guest is debuggable with no silicon under it.
_NAMES = {
    (0, 0, 0): 'off',
    (0, 40, 0): 'go', (0, 60, 0): 'go',
    (60, 40, 0): 'caution', (40, 25, 0): 'caution',
    (60, 0, 0): 'fault', (40, 0, 0): 'fault',
}


class NeoPixel:
    def __init__(self, pin, n, **kw):
        self._pin = pin
        self._n = n
        self._buf = [(0, 0, 0)] * n
        self._last = None

    def __len__(self):
        return self._n

    def __setitem__(self, i, rgb):
        self._buf[i] = tuple(rgb)

    def __getitem__(self, i):
        return self._buf[i]

    def fill(self, rgb):
        self._buf = [tuple(rgb)] * self._n

    def write(self):
        if self._buf[0] == self._last:
            return  # only speak when the colour changes; a 1 Hz beacon is not news
        self._last = self._buf[0]
        rgb = self._buf[0]
        print('[sim] rgb -> %s %s' % (rgb, _NAMES.get(rgb, '')))
