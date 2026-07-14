"""MAX7219 8x32 matrix as a framebuf (SPEC-two: a leaf's actuator is a panel).

The buffer is a MONO_HLSB framebuf — 32x8, four bytes a row, MSB the leftmost pixel —
which is exactly the byte layout the MAX7219 chain wants a row of, so `show()` ships each
row straight out with no repacking. Wrapping it in `framebuf.FrameBuffer` is the whole
trick: the built-in `text()`/`scroll()`/`fill()` then do the message board's work, and a
custom 7-tall font (the clock) just writes pixels into the same buffer.

The SPI order matches the standalone driver this replaced, proven on the physical panel:
chip 0's byte sent first ends up in the leftmost module.
"""
import framebuf
from machine import Pin, SPI

from jorm.display import Display

_DIGIT0 = 0x01
_DECODE = 0x09
_INTENSITY = 0x0A
_SCANLIMIT = 0x0B
_SHUTDOWN = 0x0C
_TEST = 0x0F


class Matrix(Display):
    """MAX7219 backend for the host display facility. The host builds one via `from_spec`
    (it owns the SPI wire); the `(spi, cs, n)` constructor stays as-is so the existing
    `hal.matrix()` path keeps working through the display-cap transition."""

    height = 8

    @classmethod
    def from_spec(cls, spec):
        spi = SPI(spec.get('spi', 1), baudrate=spec.get('baudrate', 10_000_000),
                  polarity=0, phase=0,
                  sck=Pin(spec['sck']), mosi=Pin(spec['mosi']))
        m = cls(spi, spec['cs'], spec.get('n', 4))
        m.init(spec.get('brightness', 8))
        return m

    def __init__(self, spi, cs, n=4):
        self.spi = spi
        self.cs = Pin(cs, Pin.OUT)
        self.cs.value(1)
        self.n = n
        self.width = n * 8
        self.buf = bytearray(8 * n)
        self.fb = framebuf.FrameBuffer(self.buf, self.width, 8, framebuf.MONO_HLSB)
        self._w = bytearray(2)                # reused per SPI word — show() must not allocate

    def _cmd(self, reg, data):
        w = self._w
        w[0] = reg
        w[1] = data
        self.cs.value(0)
        for _ in range(self.n):               # same register to every chip in the chain
            self.spi.write(w)
        self.cs.value(1)

    def init(self, brightness=8):
        for reg, val in ((_TEST, 0), (_DECODE, 0), (_SCANLIMIT, 7),
                         (_SHUTDOWN, 1), (_INTENSITY, brightness & 0x0F)):
            self._cmd(reg, val)
        self.fb.fill(0)
        self.show()

    def brightness(self, level):
        self._cmd(_INTENSITY, level & 0x0F)

    def show(self):
        b, n, w = self.buf, self.n, self._w
        for y in range(8):
            self.cs.value(0)
            base = y * n
            w[0] = _DIGIT0 + y
            for chip in range(n):
                w[1] = b[base + chip]
                self.spi.write(w)
            self.cs.value(1)
