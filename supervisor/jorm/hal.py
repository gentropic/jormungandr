"""The hal surface (spec §4) — everything a guest can touch, M1 core.

Every awaitable a guest can reach routes through Hal._yield, which parks/resumes
the supervisor's current-guest register — that register is the watchdog's
attribution evidence (spec §1), so no guest await may bypass it.
"""
import asyncio
import json
import os
import time

import machine
import neopixel

from jorm import bus as busmod
from jorm import guestcfg
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
        return time.time()

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

    def rgb(self, n):
        if not self._sup.claims.rgb_grant(self._guest.id, n):
            raise CapError('rgb on pin %d not granted to guest "%s"' % (n, self._guest.id))
        count = 1
        for e in self._caps().get('rgb', []):
            if e['pin'] == n:
                count = e.get('count', 1)
        return RgbHandle(n, count)

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
        topic, enc = await self._hal._yield(self._sub.get())
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
