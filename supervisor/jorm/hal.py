"""The hal surface (spec §4) — everything a guest can touch, M1 core.

Every awaitable a guest can reach routes through Hal._yield, which parks/resumes
the supervisor's current-guest register — that register is the watchdog's
attribution evidence (spec §1), so no guest await may bypass it.
"""
import asyncio
import os
import time

import machine


class CapError(Exception):
    pass


class Hal:
    def __init__(self, sup, guest):
        self._sup = sup
        self._guest = guest

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
