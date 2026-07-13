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
from jorm import usb
from jorm.bus import Bus, BusError
from jorm.claims import Claims
from jorm.fsutil import ensure_dir, write_atomic
from jorm.guests import Guest, GUESTS_DIR, LIB_DIR


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False

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
        # The USB device, built once at boot from installed guests (§8). None until
        # enumerate_usb() runs; None also on a node with no usb guests, which is why
        # hal.usb() checks it rather than assuming it exists.
        self.usb_plan = None

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

    def mcu_temp(self):
        try:
            import esp32
            return esp32.mcu_temperature()
        except (ImportError, AttributeError, OSError):
            return None

    async def telemetry(self):
        n = 0
        while True:
            self.sys_publish('$sys/clock/tick',
                             {'ts': clock.now(), 'n': n, 'synced': clock.status()['synced']})
            if n % 5 == 0:
                self.sys_publish('$sys/heap',
                                 {'free': gc.mem_free(), 'alloc': gc.mem_alloc()},
                                 retain=True)
                temp = self.mcu_temp()
                if temp is not None:
                    # Celsius. There is no other unit.
                    self.sys_publish('$sys/temp', {'c': temp}, retain=True)
            n += 1
            await asyncio.sleep(1)

    # -- import guard (spec §1: a guest never imports machine et al.) ------

    def guest_path(self, name):
        """Three-tier resolution (spec §1): the bundle dir first, then the node's
        shared library store, then the stdlib whitelist. Returns a path or None."""
        gid = self.current
        if gid:
            p = '%s/%s/%s.py' % (GUESTS_DIR, gid, name)
            if _exists(p):
                return p
        p = '%s/%s.py' % (LIB_DIR, name)
        return p if _exists(p) else None

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
            root = name.split('.')[0]
            if root in IMPORT_WHITELIST:
                return orig(name, *args)
            if self.guest_path(root):
                # A bundle module or an installed /lib driver. sys.path is set to
                # [bundle, /lib] for the duration of the guest's load (guests.py),
                # so the ordinary import machinery finds the right file.
                return orig(name, *args)
            raise ImportError('"%s" is not importable in a guest — everything arrives '
                              'through hal, or ships in the bundle, or is installed '
                              'in /lib (spec §1)' % name)

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
        ensure_dir(LIB_DIR)
        for id_ in os.listdir(GUESTS_DIR):
            if not os.stat(GUESTS_DIR + '/' + id_)[0] & 0x4000:
                continue
            guest = Guest(self, id_)
            guest.load_num()
            try:
                guest.load_manifest()
            except Exception as e:
                guest.console.append('error', 'manifest: %s' % e)
                self.node.log.append('error', 'guest %s: bad manifest: %s' % (id_, e))
            self.guests[id_] = guest
        self.node.log.append('sys', '%d guest(s) installed' % len(self.guests))

    def enumerate_usb(self):
        """Build the USB device once, at boot, from every installed guest (§8).

        After scan(), before autostart(): the descriptor is fixed from what is
        INSTALLED, and only then do guests begin to run. A guest that crashes or is
        stopped later does not change the device — its interface just goes inert.
        """
        try:
            self.usb_plan = usb.plan(self.guests.values())
        except usb.UsbError as e:
            # A plan that does not fit is a boot-time fact, not a crash. Record it,
            # enumerate nothing, and let the node come up reachable over WiFi so a
            # person can remove the guest that overflowed the bus.
            self.usb_plan = usb.Plan()
            self.usb_plan.error = str(e)
            self.node.log.append('error', 'usb: %s' % e)
            return
        if self.usb_plan.grants:
            usb.apply(self.usb_plan, self.node.log)

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
        # The starvation budget the node LEARNS, rather than one we assume.
        #
        # MicroPython's GC is mark-sweep over the whole heap, and this board has
        # 8 MB of PSRAM: a pass over a full heap stops the world for ~400 ms —
        # far past any budget you would pick by hand. Measuring it at boot is no
        # use either; the heap is empty then and it collects in 12 ms.
        #
        # So detect collections as they happen (alloc drops when the sweep frees)
        # and let the budget grow to fit what this node's collector actually
        # costs. A GC pause is the supervisor stopping the world, not a guest
        # refusing to yield — and a budget tighter than the collector convicts an
        # innocent guest once a second, which is exactly what it was doing.
        budget = 250
        stalls = 0
        last = time.ticks_ms()
        last_alloc = gc.mem_alloc()
        sample_n = 0
        while True:
            await asyncio.sleep_ms(100)
            if wdt:
                wdt.feed()
            now = time.ticks_ms()
            gap = time.ticks_diff(now, last)
            last = now

            # Sampled memory attribution (spec §2/§10): MicroPython has no
            # per-task heap, so we attribute each interval's allocation delta to
            # whoever held the CPU. It is a sample, not a measurement, and the UI
            # says so — but it is enough to see which guest is growing.
            alloc = gc.mem_alloc()
            delta = alloc - last_alloc
            last_alloc = alloc
            holder = self.guests.get(self.last_seen)
            if holder and delta > 0:
                holder.mem_acc = getattr(holder, 'mem_acc', 0) + delta

            # A sweep freed memory during this interval: the world stopped, and it
            # was the collector that stopped it. Learn what that costs here.
            collected = delta < -8192
            sample_n += 1
            if sample_n % 20 == 0:   # every ~2 s, push a point to each series
                for guest in self.guests.values():
                    kb = getattr(guest, 'mem_acc', 0) / 1024.0
                    guest.mem.append(kb)
                    if len(guest.mem) > 30:
                        guest.mem.pop(0)
            if gap > 100 + budget and collected:
                grown = min(2000, gap + 150)
                if grown > budget:
                    budget = grown
                    self.node.log.append(
                        'sys', 'gc stopped the world for %d ms — starvation budget '
                               'is now %d ms (this heap is 8 MB; the collector walks '
                               'all of it)' % (gap, budget))
            elif gap > 100 + budget:
                # Attribution is by evidence of who HOLDS the CPU — self.current,
                # the register, which is set only between a guest's resume and its
                # park. Not last_seen: a guest that already yielded is innocent,
                # and blaming it is the exact mistake the register was built to
                # end. An empty register means the supervisor stalled itself —
                # say so, rather than framing whoever ran most recently.
                guest = self.guests.get(self.current) if self.current else None
                if guest and guest.state == 'running':
                    guest.set_state('unresponsive')
                    guest.console.append('sys',
                                         'held the CPU for %d ms — flagged unresponsive' % gap)
                    self._flagged = guest.id
                else:
                    stalls += 1
                    if stalls % 10 == 1:   # do not narrate every one
                        self.node.log.append(
                            'sys', 'supervisor stalled %d ms (no guest held the CPU) '
                                   '— gc, or the node is simply busy' % gap)
