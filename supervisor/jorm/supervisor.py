"""The supervisor: current-guest register, watchdog, import guard, guest set.

Attribution by evidence (spec §1): the register names whoever holds the CPU,
mirrored into RTC memory so a WDT reset can still name its culprit.
"""
import asyncio
import builtins
import gc
import os
import time

import machine

from jorm import clock
from jorm.bus import Bus, BusError
from jorm.claims import Claims
from jorm.fsutil import ensure_dir, write_atomic
from jorm.guests import Guest, GUESTS_DIR

RTC_TAG = b'jorm!'

# spec §1: the built-in whitelist of pure stdlib a guest may import
IMPORT_WHITELIST = ('json', 'math', 'struct', 'collections', 'binascii', 're')


class Supervisor:
    def __init__(self, node):
        self.node = node
        self.claims = Claims(reserved_pins=node.settings.get('reserved_pins', []))
        self.bus = Bus()
        self.guests = {}
        self.current = None    # guest id holding the CPU right now
        self.last_seen = None  # last guest to hold it (soft-flagging evidence)
        self._flagged = None
        self._rtc = machine.RTC()
        self._i2c = {}
        self._spi = {}

    def i2c(self, bus):
        """The supervisor owns the bus object; guests get address-scoped handles."""
        if bus not in self._i2c:
            self._i2c[bus] = machine.I2C(bus)
        return self._i2c[bus]

    def spi(self, bus):
        if bus not in self._spi:
            self._spi[bus] = machine.SPI(bus)
        return self._spi[bus]

    # -- current-guest register (every hal await passes through here) ------

    def resume(self, gid):
        self.current = gid
        self.last_seen = gid
        self._rtc.memory(RTC_TAG + gid.encode())
        if self._flagged == gid:
            self._flagged = None
            guest = self.guests.get(gid)
            if guest and guest.state == 'unresponsive':
                guest.set_state('running')
                guest.console.append('sys', 'yielding again — back to running')

    def park(self, gid):
        if self.current == gid:
            self.current = None
            self._rtc.memory(b'')

    # -- the supervisor is a first-class publisher on its own bus (spec §5) --

    def sys_publish(self, topic, msg, retain=False):
        try:
            self.bus.publish(topic, msg, retain=retain, owner='$sys')
        except BusError as e:
            self.node.log.append('error', 'sys publish %s: %s' % (topic, e))

    async def ntp(self):
        """Sync at boot, then daily (spec §4). A node whose clock never sets says
        so in /api/node rather than quietly serving 1970 to every console pane."""
        tries = 0
        while True:
            if clock.sync(self.node.log):
                await asyncio.sleep(86400)   # daily, per spec §4
                tries = 0
            else:
                tries += 1
                await asyncio.sleep(min(300, 5 * 2 ** tries))  # 10s, 20s, 40s … 5 min

    async def telemetry(self):
        n = 0
        while True:
            self.sys_publish('$sys/clock/tick',
                             {'ts': clock.now(), 'n': n, 'synced': clock.status()['synced']})
            if n % 5 == 0:
                self.sys_publish('$sys/heap',
                                 {'free': gc.mem_free(), 'alloc': gc.mem_alloc()},
                                 retain=True)
            n += 1
            await asyncio.sleep(1)

    # -- import guard (spec §1: a guest never imports machine et al.) ------

    def install_import_guard(self):
        # asyncio lazy-loads submodules on first attribute touch; force them all
        # now, while the register is clear — otherwise the first guest to make
        # the supervisor touch e.g. asyncio.Event gets blamed for an internal
        # import ("event" is not importable in a guest) it never wrote.
        for name in ('wait_for', 'wait_for_ms', 'gather', 'Event',
                     'ThreadSafeFlag', 'Lock', 'StreamReader', 'StreamWriter',
                     'open_connection', 'start_server', 'current_task',
                     'new_event_loop'):
            getattr(asyncio, name, None)

        orig = builtins.__import__

        def guarded(name, *args):
            if self.current is None:
                return orig(name, *args)
            if name.split('.')[0] in IMPORT_WHITELIST:
                return orig(name, *args)
            raise ImportError('"%s" is not importable in a guest — everything arrives through hal (spec §1)'
                              % name)

        builtins.__import__ = guarded

    # -- boot ---------------------------------------------------------------

    def blame_check(self):
        stamp = self._rtc.memory()
        if not (stamp and stamp[:len(RTC_TAG)] == RTC_TAG):
            return
        gid = stamp[len(RTC_TAG):].decode()
        self._rtc.memory(b'')
        self.node.log.append('sys',
                             'watchdog reset: guest "%s" held the CPU — autostart disabled' % gid)
        try:
            write_atomic(GUESTS_DIR + '/' + gid + '/.suspected',
                         'suspected in watchdog reset — autostart disabled')
        except OSError:
            pass

    def scan(self):
        ensure_dir(GUESTS_DIR)
        for id_ in os.listdir(GUESTS_DIR):
            if not os.stat(GUESTS_DIR + '/' + id_)[0] & 0x4000:
                continue
            guest = Guest(self, id_)
            try:
                guest.load_manifest()
            except Exception as e:
                guest.console.append('error', 'manifest: %s' % e)
                self.node.log.append('error', 'guest %s: bad manifest: %s' % (id_, e))
            self.guests[id_] = guest
        self.node.log.append('sys', '%d guest(s) installed' % len(self.guests))

    async def autostart(self):
        for guest in self.guests.values():
            if not (guest.manifest and guest.manifest.get('autostart')):
                continue
            if guest.suspected():
                self.node.log.append('sys', 'guest %s: autostart skipped (suspected)' % guest.id)
                continue
            try:
                await guest.start(manual=False)
            except Exception as e:
                self.node.log.append('error', 'autostart %s: %s' % (guest.id, e))

    # -- heartbeat + hardware WDT (spec §1) ----------------------------------

    async def heartbeat(self):
        wdt = None
        if self.node.settings.get('wdt', True):
            try:
                wdt = machine.WDT(timeout=8000)
            except (AttributeError, OSError, ValueError):
                pass
        last = time.ticks_ms()
        while True:
            await asyncio.sleep_ms(100)
            if wdt:
                wdt.feed()
            now = time.ticks_ms()
            gap = time.ticks_diff(now, last)
            last = now
            if gap > 350 and self.last_seen:  # 100 ms period + 250 ms starvation budget
                guest = self.guests.get(self.last_seen)
                if guest and guest.state == 'running':
                    guest.set_state('unresponsive')
                    guest.console.append('sys',
                                         'starved the loop for %d ms — flagged unresponsive' % gap)
                    self._flagged = guest.id
