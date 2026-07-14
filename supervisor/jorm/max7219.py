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
from machine import Pin

_DIGIT0 = 0x01
_DECODE = 0x09
_INTENSITY = 0x0A
_SCANLIMIT = 0x0B
_SHUTDOWN = 0x0C
_TEST = 0x0F


class Matrix:
    def __init__(self, spi, cs, n=4):
        self.spi = spi
        self.cs = Pin(cs, Pin.OUT)
        self.cs.value(1)
        self.n = n
        self.width = n * 8
        self.buf = bytearray(8 * n)
        self.fb = framebuf.FrameBuffer(self.buf, self.width, 8, framebuf.MONO_HLSB)

    def _cmd(self, reg, data):
        self.cs.value(0)
        for _ in range(self.n):               # same register to every chip in the chain
            self.spi.write(bytes((reg, data)))
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
        b, n = self.buf, self.n
        for y in range(8):
            self.cs.value(0)
            base = y * n
            for chip in range(n):
                self.spi.write(bytes((_DIGIT0 + y, b[base + chip])))
            self.cs.value(1)
