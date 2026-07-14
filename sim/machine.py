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


# Reset-cause constants + a stub cause, so node.reset_reason() resolves to a real name off
# hardware (a fresh sim boot reads as a power-on). Values mirror the ESP32 port's ordering.
PWRON_RESET = 1
HARD_RESET = 2
WDT_RESET = 3
DEEPSLEEP_RESET = 4
SOFT_RESET = 5
BROWNOUT_RESET = 6


def reset_cause():
    return PWRON_RESET


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


class TouchPad:
    """A capacitive touch pad — a slow varying reading so a touch guest sees something move."""

    def __init__(self, pin):
        self._pin = pin

    def read(self):
        return int(600 + 200 * math.sin(time.ticks_ms() / 3000))


class DAC:
    def __init__(self, pin):
        self._pin = pin
        self._v = 0

    def write(self, v):
        self._v = v & 0xff
        print('[sim] dac -> %d' % self._v)


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
        # Canned device registers, so a sim sensor guest reads plausible values, not zeros —
        # a BMP280-ish device at 0x76: 0xD0 is the chip id (0x58); other regs ramp slowly.
        if addr == 0x76 and memaddr == 0xD0:
            return bytes([0x58] + [0] * (n - 1)) if n else b''
        return bytes((time.ticks_ms() // 100 + i) & 0xff for i in range(n))

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


class UART:
    """Loopback UART: the sim has no wire, so what a guest writes it can read back — enough
    to prove the cap round-trips (tx -> rx) without silicon. Real behaviour is the board's."""

    def __init__(self, id=1, baudrate=9600, tx=None, rx=None, **kw):
        self._id = id
        self._buf = b''

    def write(self, buf):
        self._buf += bytes(buf)
        return len(buf)

    def read(self, n=None):
        if n is None or n >= len(self._buf):
            r, self._buf = self._buf, b''
        else:
            r, self._buf = self._buf[:n], self._buf[n:]
        return r or None

    def readline(self):
        i = self._buf.find(b'\n')
        if i < 0:
            return self.read()
        r, self._buf = self._buf[:i + 1], self._buf[i + 1:]
        return r

    def any(self):
        return len(self._buf)


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


class USBDevice:
    """The unix port has no TinyUSB, so the sim cannot enumerate — but the point of
    the sim (§11.15) is that the supervisor runs UNMODIFIED, so the class must exist
    for the import, the planner, the endpoint budget and the fit check to all be
    real and tested here. Only the final handoff to the controller is silicon's job:
    the vendored usb core reaches into these built-in descriptor objects, finds the
    sim's stand-ins are hollow, and raises — which usb.apply() catches and records
    as 'this node did not enumerate', exactly as a board with a rejected descriptor
    would. The node comes up reachable regardless; that is the property under test."""

    BUILTIN_NONE = None
    BUILTIN_DEFAULT = None
    BUILTIN_CDC = None

    def __init__(self):
        self._active = False
        self.builtin_driver = None

    def config(self, *args, **kwargs):
        pass

    def active(self, *value):
        if value:
            self._active = bool(value[0])
        return self._active

    def submit_xfer(self, *a, **k):
        return False

    def stall(self, *a, **k):
        pass
