# sim stub for MicroPython's `machine` — the unix port has no silicon under it.
# Deployed nodes never see this file; MICROPYPATH puts sim/ first only in sim/run.sh.
# Imports live up here: a lazy import inside a method would run mid-guest-slice
# and get blamed on the guest by the import guard (spec §1).
import math
import sys
import time

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


class PWM:
    def __init__(self, pin, freq=1000, duty=0):
        self._pin = pin
        self._freq = freq
        self._duty = duty
        print('[sim] pwm on %s' % pin)

    def freq(self, hz=None):
        if hz is None:
            return self._freq
        self._freq = hz
        print('[sim] pwm freq -> %d' % hz)

    def duty(self, d=None):
        if d is None:
            return self._duty
        self._duty = d
        print('[sim] pwm duty -> %d' % d)

    def deinit(self):
        pass


class ADC:
    """Synthesizes a slow sine so sensor guests have something true to report."""

    def __init__(self, pin):
        self._pin = pin

    def read_u16(self):
        return int(30000 + 4000 * math.sin(time.ticks_ms() / 20000))


class I2C:
    def __init__(self, id=0, **kw):
        self._id = id

    def scan(self):
        return [0x76]

    def readfrom(self, addr, n):
        return bytes(n)

    def writeto(self, addr, buf):
        print('[sim] i2c%d write 0x%02x: %d bytes' % (self._id, addr, len(buf)))

    def readfrom_mem(self, addr, memaddr, n):
        return bytes(n)

    def writeto_mem(self, addr, memaddr, buf):
        print('[sim] i2c%d mem write 0x%02x/%d: %d bytes' % (self._id, addr, memaddr, len(buf)))


class SPI:
    def __init__(self, id=1, **kw):
        self._id = id

    def write(self, buf):
        print('[sim] spi%d write: %d bytes' % (self._id, len(buf)))

    def write_readinto(self, wbuf, rbuf):
        for i in range(len(rbuf)):
            rbuf[i] = 0


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
