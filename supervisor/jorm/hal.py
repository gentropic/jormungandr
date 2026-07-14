"""The hal surface (spec §4) — everything a guest can touch, M1 core.

Every awaitable a guest can reach routes through Hal._yield, which parks/resumes
the supervisor's current-guest register — that register is the watchdog's
attribution evidence (spec §1), so no guest await may bypass it.
"""
import asyncio
import json
import os
import socket
import time

import machine
import neopixel

try:
    from jorm.max7219 import Matrix as _Matrix   # top-level, like neopixel: imported in
except ImportError:                              # supervisor context so hal.matrix() does
    _Matrix = None                               # not trip the guest import guard. Absent
                                                 # on the sim (no framebuf); harmless there.

# Device drivers a guest can lease but never import (the guard blocks it): loaded here in
# supervisor context, like neopixel. Guarded so a firmware without them still imports hal —
# hal.dht()/onewire() then raise a clear CapError rather than the supervisor failing to boot.
try:
    import dht as _dht
except ImportError:
    _dht = None
try:
    import onewire as _onewire
    import ds18x20 as _ds18x20
except ImportError:
    _onewire = _ds18x20 = None

from jorm import bus as busmod
from jorm import clock
from jorm import guestcfg
from jorm.claims import display_id   # top-level: display() must not import in guest context
from jorm.fsutil import UnsafePath, ensure_dir, safe_relpath, tree_size
from jorm.panels import validate_panel
from jorm.ring import Tap


class CapError(Exception):
    pass


class Hal:
    def __init__(self, sup, guest):
        self._sup = sup
        self._guest = guest
        self.bus = BusHandle(self, guest.bus_grants())
        self.console = ConsoleHandle(self)
        self.net = NetHandle(self)
        self.storage = StorageHandle(self)
        self.ui = UiHandle(self)
        self.config = ConfigHandle(self)

    def _caps(self):
        return self._guest.manifest.get('caps', {})

    async def _yield(self, aw):
        sup, gid = self._sup, self._guest.id
        sup.park(gid)
        try:
            return await aw
        finally:
            sup.resume(gid)

    def log(self, *args):
        self._guest.console.append('info', ' '.join(str(a) for a in args))

    async def sleep_ms(self, ms):
        await self._yield(asyncio.sleep_ms(ms))

    async def sleep(self, s):
        await self._yield(asyncio.sleep(s))

    def ticks_ms(self):
        return time.ticks_ms()

    def time(self):
        return clock.now()   # Unix seconds, or uptime honestly labelled (spec §4)

    def rand(self, n):
        return os.urandom(n)

    def status(self, text):
        self._guest.status = str(text)[:80]

    def pin(self, n):
        grant = self._sup.claims.pin_grant(self._guest.id, n)
        if grant is None:
            raise CapError('pin %d not granted to guest "%s"' % (n, self._guest.id))
        return PinHandle(n, grant['mode'], grant['pull'])

    def pwm(self, n):
        if not self._sup.claims.pwm_grant(self._guest.id, n):
            raise CapError('pwm on pin %d not granted to guest "%s"' % (n, self._guest.id))
        return PwmHandle(n)

    def adc(self, ch):
        if not self._sup.claims.adc_grant(self._guest.id, ch):
            raise CapError('adc channel %d not granted to guest "%s"' % (ch, self._guest.id))
        return AdcHandle(ch)

    def i2c(self, bus):
        if not any(e['bus'] == bus for e in self._caps().get('i2c', [])):
            raise CapError('i2c bus %d not granted to guest "%s"' % (bus, self._guest.id))
        return I2cHandle(self, bus)

    def spi(self, bus, cs):
        if not self._sup.claims.spi_grant(self._guest.id, bus, cs):
            raise CapError('spi %d/cs %d not granted to guest "%s"' % (bus, cs, self._guest.id))
        return SpiHandle(self, bus, cs)

    def uart(self, id):
        for e in self._caps().get('uart', []):
            if e['id'] == id and self._sup.claims.uart_grant(self._guest.id, e['tx']):
                return UartHandle(id, e['tx'], e['rx'], e.get('baud', 9600))
        raise CapError('uart %d not granted to guest "%s"' % (id, self._guest.id))

    def touch(self, n):
        if not self._sup.claims.touch_grant(self._guest.id, n):
            raise CapError('touch on pin %d not granted to guest "%s"' % (n, self._guest.id))
        return TouchHandle(n)

    def dac(self, n):
        if not self._sup.claims.dac_grant(self._guest.id, n):
            raise CapError('dac on pin %d not granted to guest "%s"' % (n, self._guest.id))
        return DacHandle(n)

    def dht(self, n):
        if not self._sup.claims.dht_grant(self._guest.id, n):
            raise CapError('dht on pin %d not granted to guest "%s"' % (n, self._guest.id))
        if _dht is None:
            raise CapError('dht driver is not on this firmware')
        return DhtHandle(n)

    def onewire(self, n):
        if not self._sup.claims.onewire_grant(self._guest.id, n):
            raise CapError('onewire on pin %d not granted to guest "%s"' % (n, self._guest.id))
        if _onewire is None:
            raise CapError('onewire/ds18x20 driver is not on this firmware')
        return OneWireHandle(n)

    def udp(self, port=0):
        if self._caps().get('udp') != {'client': True}:
            raise CapError('udp not granted to guest "%s"' % self._guest.id)
        return UdpHandle(self, port)

    def rgb(self, n):
        if not self._sup.claims.rgb_grant(self._guest.id, n):
            raise CapError('rgb on pin %d not granted to guest "%s"' % (n, self._guest.id))
        count = 1
        for e in self._caps().get('rgb', []):
            if e['pin'] == n:
                count = e.get('count', 1)
        return RgbHandle(n, count)

    def matrix(self):
        mx = self._caps().get('matrix')
        if not mx or not self._sup.claims.matrix_grant(self._guest.id, mx.get('cs')):
            raise CapError('matrix not granted to guest "%s"' % self._guest.id)
        return MatrixHandle(mx.get('spi', 1), mx['sck'], mx['mosi'], mx['cs'], mx.get('n', 4))

    def display(self, id=None):
        """hal.display — a focus-gated surface on a display the HOST owns (SPEC-two §8,
        revised). The guest draws while it holds the lease; its show() only reaches the
        glass while focused, so the host can reclaim the panel for status when the guest
        is gone. Same surface API as the matrix cap; the wire is the supervisor's."""
        cap = self._caps().get('display')
        if cap is None:
            raise CapError('display not granted to guest "%s"' % self._guest.id)
        did = id or display_id(cap)
        mgr = self._sup.display
        if mgr is None or mgr.get(did) is None:
            raise CapError('no display "%s" on this node' % did)
        if not self._sup.claims.display_grant(self._guest.id, did):
            raise CapError('display "%s" not granted to guest "%s"' % (did, self._guest.id))
        return DisplayHandle(mgr, mgr.get(did), self._guest.id)

    def usb(self):
        """hal.usb — the guest's own granted USB interface, and only its own.

        The descriptor was built at boot and this guest's interface is inert until
        the guest is running (spec §8). What comes back is a thin driver over the
        one interface granted to THIS guest — never the device, never another
        guest's interface. A guest with no usb cap, or one whose interface did not
        make it into the enumerated plan, gets a clear CapError, not a keyboard
        that silently drops every keystroke.
        """
        plan = self._sup.usb_plan
        grants = plan.by_guest(self._guest.id) if plan else []
        if not grants:
            raise CapError('usb not granted to guest "%s" — declare caps.usb, then '
                           'reboot so the device re-enumerates' % self._guest.id)
        return UsbHandle(self, grants)

    def spawn(self, coro):
        task = asyncio.create_task(self._child(coro))
        self._guest.children.append(task)
        return task

    async def _child(self, coro):
        gid = self._guest.id
        try:
            self._sup.resume(gid)
            try:
                await coro
            finally:
                self._sup.park(gid)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # a child crash detonates the whole family (spec §1)
            self._guest.child_crashed(e)

    async def gather(self, *aws):
        return await self._yield(asyncio.gather(*aws))

    def Event(self):
        return HalEvent(self)

    def Queue(self, n=16):
        return HalQueue(self, n)


class ConsoleHandle:
    """hal.console — the guest's serial line, and it runs both ways.

    A VM's console is not a log: you type into it. A guest that wants to be
    driven by a human asks for input(); one that never asks simply never reads,
    and the UI says so rather than offering a box that does nothing. Registering
    the reader is what makes the difference visible — see Guest.reads_input.
    """

    def __init__(self, hal):
        self._hal = hal

    def write(self, *args):
        self._hal.log(*args)

    def input(self):
        guest = self._hal._guest
        tap = Tap(qlen=16)   # a guest that ignores its console loses its own lines
        guest.stdin.append(tap)
        return _LineIterator(self._hal, tap)


class _LineIterator:
    def __init__(self, hal, tap):
        self._hal = hal
        self._tap = tap

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._hal._yield(self._tap.get())


class BusHandle:
    """hal.bus (spec §4/§5): grant-checked pub/sub. Violations raise — never
    a silent drop."""

    def __init__(self, hal, grants):
        self._hal = hal
        self._grants = grants  # (pub_filters, sub_filters) or None

    def _need(self):
        if self._grants is None:
            raise CapError('guest "%s" has no bus cap' % self._hal._guest.id)
        return self._grants

    def publish(self, topic, msg, retain=False):
        pub, _ = self._need()
        gid = self._hal._guest.id
        if topic.split('/')[0].startswith('$'):
            raise CapError('"$" roots are supervisor-written (spec §5)')
        if not any(busmod.match(f, topic) for f in pub):
            raise CapError('publish to "%s" is outside guest "%s" pub grant' % (topic, gid))
        self._hal._sup.bus.publish(topic, msg, retain=retain, owner=gid)

    def subscribe(self, topic_filter):
        _, sub_grants = self._need()
        if not busmod.valid_filter(topic_filter):
            raise CapError('invalid topic filter %r' % topic_filter)
        if not any(busmod.covered(g, topic_filter) for g in sub_grants):
            raise CapError('subscribe "%s" is outside guest "%s" sub grant'
                           % (topic_filter, self._hal._guest.id))
        sub = self._hal._sup.bus.subscribe([topic_filter], qlen=16,
                                           owner=self._hal._guest.id)
        self._hal._guest.subs.append(sub)
        return SubIterator(self._hal, sub)

    def retained(self, topic):
        _, sub_grants = self._need()
        if not any(busmod.match(g, topic) for g in sub_grants):
            raise CapError('retained("%s") is outside guest "%s" sub grant'
                           % (topic, self._hal._guest.id))
        enc = self._hal._sup.bus.retained.get(topic)
        return json.loads(enc) if enc is not None else None


class SubIterator:
    """async iterator of (topic, msg); each subscriber decodes its own copy —
    no live objects cross guest boundaries."""

    def __init__(self, hal, sub):
        self._hal = hal
        self._sub = sub

    def __aiter__(self):
        return self

    async def __anext__(self):
        # The bus payload now carries an origin node too; a guest still sees just
        # (topic, msg) — a guest subscribing to a bridged topic neither knows nor
        # cares which board it crossed from, which is the point of one tree.
        topic, enc, _origin = await self._hal._yield(self._sub.get())
        return topic, json.loads(enc)


class PinHandle:
    def __init__(self, n, mode, pull):
        self._n = n
        self._mode = mode
        if mode == 'out':
            self._pin = machine.Pin(n, machine.Pin.OUT)
        else:
            pull_arg = {'up': machine.Pin.PULL_UP, 'down': machine.Pin.PULL_DOWN}.get(pull, -1)
            self._pin = machine.Pin(n, machine.Pin.IN, pull_arg)
        self._v = 0

    def _writable(self):
        if self._mode != 'out':
            raise CapError('pin %d is granted %s — not writable' % (self._n, self._mode))

    def value(self, v=None):
        if v is None:
            return self._pin.value() if self._mode != 'out' else self._v
        self._writable()
        self._v = 1 if v else 0
        self._pin.value(self._v)

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def toggle(self):
        self.value(0 if self._v else 1)


class HalEvent:
    def __init__(self, hal):
        self._hal = hal
        self._ev = asyncio.Event()

    def set(self):
        self._ev.set()

    def clear(self):
        self._ev.clear()

    def is_set(self):
        return self._ev.is_set()

    async def wait(self):
        return await self._hal._yield(self._ev.wait())


class HalQueue:
    """Bounded FIFO — MicroPython's asyncio ships no Queue, so hal brings its own."""

    def __init__(self, hal, n):
        self._hal = hal
        self._n = n
        self._items = []
        self._data = asyncio.Event()
        self._space = asyncio.Event()

    async def put(self, item):
        while len(self._items) >= self._n:
            self._space.clear()
            await self._hal._yield(self._space.wait())
        self._items.append(item)
        self._data.set()

    async def get(self):
        while not self._items:
            self._data.clear()
            await self._hal._yield(self._data.wait())
        item = self._items.pop(0)
        self._space.set()
        return item

    def qsize(self):
        return len(self._items)


class PwmHandle:
    def __init__(self, n):
        self._pwm = machine.PWM(machine.Pin(n))

    def freq(self, hz=None):
        return self._pwm.freq() if hz is None else self._pwm.freq(hz)

    def duty(self, d=None):
        if d is None:
            return self._pwm.duty()
        if not 0 <= d <= 1023:
            raise ValueError('duty is 0..1023')
        self._pwm.duty(d)


class UsbHandle:
    """hal.usb — a guest driving the USB interface it was granted.

    A guest gets keystrokes and clicks, not descriptors. The supervisor built the
    composite device at boot and owns it; this handle reaches exactly the one
    interface granted to this guest. `.keyboard`/`.mouse` are None unless that kind
    was granted, so a guest that asked for a keyboard cannot find a mouse to poke.
    """

    def __init__(self, hal, grants):
        self._hal = hal
        self.keyboard = None
        self.mouse = None
        for g in grants:
            if g.kind == 'hid' and g.spec == 'keyboard':
                self.keyboard = KeyboardGrant(g.itf)
            elif g.kind == 'hid' and g.spec == 'mouse':
                self.mouse = g.itf

    @property
    def ready(self):
        """True once the host has actually configured the interface. A guest can
        poll this instead of typing into a void when nothing is plugged in."""
        k = self.keyboard or self.mouse
        try:
            return bool(k and k.is_open())
        except AttributeError:
            return False


class KeyboardGrant:
    """The keyboard, as a guest holds it. Type a string, or press/release raw
    keycodes (hal.usb via jorm.usb.KeyCode). The supervisor's own release() can
    still lift every key when the guest is stopped, because it holds the same itf."""

    def __init__(self, itf):
        self._itf = itf

    def is_open(self):
        return self._itf.is_open()

    def press(self, *keycodes):
        self._itf.send_keys(keycodes)

    def release(self):
        self._itf.send_keys(())

    def tap(self, *keycodes):
        self._itf.send_keys(keycodes)
        self._itf.send_keys(())


class RgbHandle:
    """Addressable RGB (WS2812 and kin). The supervisor owns the bit-banged
    timing — a guest gets colours, not microseconds."""

    def __init__(self, n, count):
        self._np = neopixel.NeoPixel(machine.Pin(n), count)
        self._count = count

    def __len__(self):
        return self._count

    def set(self, i, rgb):
        self._np[i] = tuple(rgb)

    def fill(self, rgb):
        self._np.fill(tuple(rgb))

    def write(self):
        self._np.write()

    def off(self):
        self._np.fill((0, 0, 0))
        self._np.write()


class MatrixHandle:
    """An 8-row LED matrix (a MAX7219 chain) as a framebuffer. Like RgbHandle, the
    supervisor owns the wire — the SPI and the chip registers — and a guest gets a
    surface: fill/pixel/text/scroll and show(). The built-in framebuf provides the 8x8
    text font; a guest brings its own if it wants bigger glyphs (the clock does)."""

    def __init__(self, spi_id, sck, mosi, cs, n):
        if _Matrix is None:
            raise CapError('matrix driver unavailable (no framebuf on this port)')
        spi = machine.SPI(spi_id, baudrate=10_000_000, polarity=0, phase=0,
                          sck=machine.Pin(sck), mosi=machine.Pin(mosi))
        self._m = _Matrix(spi, cs, n)
        self._m.init()
        self.width = self._m.width
        self.height = 8

    def fill(self, v=0):
        self._m.fb.fill(1 if v else 0)

    def pixel(self, x, y, v=1):
        self._m.fb.pixel(int(x), int(y), 1 if v else 0)

    def text(self, s, x=0, y=0, v=1):
        self._m.fb.text(str(s), int(x), int(y), 1 if v else 0)

    def hline(self, x, y, w, v=1):
        self._m.fb.hline(int(x), int(y), int(w), 1 if v else 0)

    def rect(self, x, y, w, h, v=1, fill=False):
        self._m.fb.rect(int(x), int(y), int(w), int(h), 1 if v else 0, fill)

    def scroll(self, dx, dy):
        self._m.fb.scroll(int(dx), int(dy))

    def brightness(self, level):
        self._m.brightness(max(0, min(15, int(level))))

    def show(self):
        self._m.show()

    def off(self):
        self._m.fb.fill(0)
        self._m.show()


class DisplayHandle:
    """A guest's surface on a host-owned display. Drawing goes straight to the shared
    framebuffer, but show()/off()/brightness() are gated on holding the focus lease: an
    unfocused guest cannot push to the glass or dim it out from under the host console.
    The host clears the buffer when it reclaims, so a stale guest frame never lingers."""

    def __init__(self, mgr, display, gid):
        self._mgr = mgr
        self._d = display
        self._gid = gid
        self.width = display.width
        self.height = display.height

    def _own(self):
        return self._mgr.owns(self._gid)

    def fill(self, v=0):
        self._d.fill(v)

    def pixel(self, x, y, v=1):
        self._d.pixel(x, y, v)

    def text(self, s, x=0, y=0, v=1):
        self._d.text(s, x, y, v)

    def hline(self, x, y, w, v=1):
        self._d.hline(x, y, w, v)

    def rect(self, x, y, w, h, v=1, fill=False):
        self._d.rect(x, y, w, h, v, fill)

    def scroll(self, dx, dy):
        self._d.scroll(dx, dy)

    def brightness(self, level):
        if self._own():
            self._d.brightness(level)

    def show(self):
        if self._own():
            self._d.show()

    def off(self):
        if self._own():
            self._d.off()


class AdcHandle:
    def __init__(self, ch):
        self._adc = machine.ADC(machine.Pin(ch))

    def read_u16(self):
        return self._adc.read_u16()


class I2cHandle:
    """Address-scoped handle to a supervisor-owned bus. Each call is one
    atomic bus transaction (spec §3); the address is checked per call."""

    def __init__(self, hal, bus):
        self._hal = hal
        self._bus = bus

    def _dev(self, addr):
        if not self._hal._sup.claims.i2c_grant(self._hal._guest.id, self._bus, addr):
            raise CapError('i2c %d/0x%02x not granted to guest "%s"'
                           % (self._bus, addr, self._hal._guest.id))
        return self._hal._sup.i2c(self._bus)

    def read(self, addr, n):
        return self._dev(addr).readfrom(addr, n)

    def write(self, addr, buf):
        self._dev(addr).writeto(addr, buf)

    def mem_read(self, addr, memaddr, n):
        return self._dev(addr).readfrom_mem(addr, memaddr, n)

    def mem_write(self, addr, memaddr, buf):
        self._dev(addr).writeto_mem(addr, memaddr, buf)


class SpiHandle:
    def __init__(self, hal, bus, cs):
        self._spi = hal._sup.spi(bus)
        self._cs = machine.Pin(cs, machine.Pin.OUT)
        self._cs.value(1)

    def xfer(self, buf):
        rbuf = bytearray(len(buf))
        self._cs.value(0)
        try:
            self._spi.write_readinto(buf, rbuf)
        finally:
            self._cs.value(1)
        return bytes(rbuf)

    def write(self, buf):
        self._cs.value(0)
        try:
            self._spi.write(buf)
        finally:
            self._cs.value(1)


class UartHandle:
    """A supervisor-owned UART, granted by declaring its id + tx/rx pins. A guest talks a
    serial peripheral (a GPS, another MCU) without importing machine — the pins are reserved
    like any other, so two guests can't fight over the same wire."""

    def __init__(self, id, tx, rx, baud):
        self._u = machine.UART(id, baudrate=baud, tx=machine.Pin(tx), rx=machine.Pin(rx))

    def write(self, buf):
        return self._u.write(buf)

    def read(self, n=None):
        return self._u.read(n)

    def readline(self):
        return self._u.readline()

    def any(self):
        return self._u.any()


class TouchHandle:
    def __init__(self, n):
        self._t = machine.TouchPad(machine.Pin(n))

    def read(self):
        return self._t.read()


class DacHandle:
    def __init__(self, n):
        self._d = machine.DAC(machine.Pin(n))

    def write(self, value):
        self._d.write(value & 0xff)      # 8-bit, 0..255


class DhtHandle:
    """A DHT11/DHT22 temp+humidity sensor on one pin. measure(), then read the last sample —
    the sensor is slow (~2 s between reads), so a guest measures on its own cadence."""

    def __init__(self, n):
        self._s = _dht.DHT22(machine.Pin(n))

    def measure(self):
        self._s.measure()

    def temperature(self):
        return self._s.temperature()

    def humidity(self):
        return self._s.humidity()


class OneWireHandle:
    """DS18B20-style temperature sensors on a one-wire bus (one pin, many devices): scan() for
    device ROMs, convert() to trigger a reading, then read_temp(rom) once it has settled."""

    def __init__(self, n):
        self._ds = _ds18x20.DS18X20(_onewire.OneWire(machine.Pin(n)))

    def scan(self):
        return self._ds.scan()

    def convert(self):
        self._ds.convert_temp()

    def read_temp(self, rom):
        return self._ds.read_temp(rom)


class UdpHandle:
    """Client UDP datagrams (spec §3): the guest sends and receives, but the supervisor owns
    the socket — a guest cannot import socket (the guard blocks it). recv is non-blocking and
    yields through the hal, so the watchdog still sees who holds the CPU."""

    def __init__(self, hal, port):
        self._hal = hal
        self._s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._s.setblocking(False)
        if port:
            self._s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._s.bind(socket.getaddrinfo('0.0.0.0', port)[0][-1])

    def sendto(self, data, host, port):
        return self._s.sendto(data, socket.getaddrinfo(host, port)[0][-1])

    async def recv(self, timeout_ms=1000, bufsize=512):
        waited = 0
        while waited < timeout_ms:
            try:
                return self._s.recvfrom(bufsize)     # (data, addr)
            except OSError:                          # EAGAIN — nothing waiting yet
                await self._hal.sleep_ms(20)
                waited += 20
        return None

    def close(self):
        self._s.close()


class NetResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    @property
    def text(self):
        return self.body.decode()

    def json(self):
        return json.loads(self.body)


class NetHandle:
    """Tiny async HTTP client (spec §4). Client-only in zero; the supervisor
    owns the listener. HTTPS lands with the flagship TLS budget, not here."""

    def __init__(self, hal):
        self._hal = hal

    def _need(self):
        if not self._hal._caps().get('net', {}).get('client'):
            raise CapError('guest "%s" has no net cap' % self._hal._guest.id)

    async def socket(self, host, port):
        self._need()
        return await self._hal._yield(asyncio.open_connection(host, port))

    async def get(self, url):
        return await self._request('GET', url, None)

    async def post(self, url, data):
        return await self._request('POST', url, data)

    async def _request(self, method, url, data):
        self._need()
        if not url.startswith('http://'):
            raise CapError('http:// only — TLS costs ~35 KB of heap and is a flagship cap (spec §3)')
        rest = url[7:]
        hostport, _, path = rest.partition('/')
        host, _, port = hostport.partition(':')
        return await self._hal._yield(self._do(method, host, int(port or 80), '/' + path, data))

    async def _do(self, method, host, port, path, data):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            body = b''
            if data is not None:
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
            writer.write(('%s %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n'
                          'Content-Length: %d\r\n\r\n'
                          % (method, path, host, len(body))).encode())
            if body:
                writer.write(body)
            await writer.drain()
            raw = await reader.read(-1)
        finally:
            writer.close()
            await writer.wait_closed()
        head, _, payload = raw.partition(b'\r\n\r\n')
        status = int(head.split(b' ', 2)[1])
        return NetResponse(status, payload)


class QuotaFile:
    """Advisory quota, checked on write — seek-past-quota is covered because
    every write path goes through here (spec §3)."""

    def __init__(self, f, budget, guest_id):
        self._f = f
        self._budget = budget
        self._gid = guest_id

    def write(self, data):
        self._budget -= len(data)
        if self._budget < 0:
            raise OSError('storage quota exceeded for guest "%s" (advisory, spec §3)' % self._gid)
        return self._f.write(data)

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


class StorageHandle:
    def __init__(self, hal):
        self._hal = hal

    def open(self, path, mode='r'):
        caps = self._hal._caps()
        if 'storage' not in caps:
            raise CapError('guest "%s" has no storage cap' % self._hal._guest.id)
        try:
            path = safe_relpath(path)
        except UnsafePath:
            raise CapError('path %r escapes the storage jail (spec §3)' % path)
        root = self._hal._guest.dir + '/data'
        ensure_dir(root)
        full = root + '/' + path
        if 'w' in mode or 'a' in mode or '+' in mode:
            used = tree_size(root)
            if 'w' in mode:
                try:
                    used -= os.stat(full)[6]  # truncating: the old bytes come back
                except OSError:
                    pass
            budget = caps['storage']['quota_kb'] * 1024 - used
            return QuotaFile(open(full, mode), budget, self._hal._guest.id)
        return open(full, mode)


class UiHandle:
    def __init__(self, hal):
        self._hal = hal

    def _need(self):
        if self._hal._caps().get('ui') is not True:
            raise CapError('guest "%s" has no ui cap' % self._hal._guest.id)

    async def panel(self, widgets):
        self._need()
        grants = self._hal._guest.bus_grants()
        validate_panel(widgets, grants[1] if grants else [])
        self._hal._sup.sys_publish('$ui/%s/panel' % self._hal._guest.id,
                                   {'v': 0, 'widgets': widgets}, retain=True)

    async def config(self, fields):
        self._need()
        guestcfg.declare(self._hal._sup, self._hal._guest, fields)


class ConfigHandle:
    def __init__(self, hal):
        self._hal = hal

    def get(self, key, default=None):
        return self._hal._guest.cfg_values.get(key, default)

    def all(self):
        return dict(self._hal._guest.cfg_values)

    def watch(self):
        tap = Tap(qlen=8)
        self._hal._guest.cfg_watchers.append(tap)
        return _WatchIterator(self._hal, tap)


class _WatchIterator:
    def __init__(self, hal, tap):
        self._hal = hal
        self._tap = tap

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._hal._yield(self._tap.get())
