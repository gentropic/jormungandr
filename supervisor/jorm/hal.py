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

from jorm import bus as busmod


class CapError(Exception):
    pass


class Hal:
    def __init__(self, sup, guest):
        self._sup = sup
        self._guest = guest
        self.bus = BusHandle(self, guest.bus_grants())

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
