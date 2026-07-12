# sim stub for MicroPython's `machine` — the unix port has no silicon under it.
# Deployed nodes never see this file; MICROPYPATH puts sim/ first only in sim/run.sh.
import sys

_RTC_MEM = bytearray()


def reset():
    print('[sim] machine.reset() — exiting')
    sys.exit(0)


def unique_id():
    return b'\xb8\xf8\x62\xf7\xc5\x10'


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 2
    PULL_DOWN = 3
    IRQ_RISING = 4
    IRQ_FALLING = 8

    def __init__(self, n, mode=-1, pull=-1):
        self._n = n
        self._v = 0

    def value(self, v=None):
        if v is None:
            return self._v
        self._v = 1 if v else 0
        print('[sim] pin %d -> %d' % (self._n, self._v))

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def toggle(self):
        self.value(not self._v)

    def irq(self, handler=None, trigger=0):
        pass


class RTC:
    def memory(self, buf=None):
        global _RTC_MEM
        if buf is None:
            return bytes(_RTC_MEM)
        _RTC_MEM = bytearray(buf)


class WDT:
    def __init__(self, id=0, timeout=5000):
        print('[sim] WDT armed, timeout %d ms (inert in sim)' % timeout)

    def feed(self):
        pass
