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
from jorm.bridge import BridgeManager
from jorm.cluster import Discovery
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

# Serving self-probe cadence: probe every HEALTH_PROBE_S; after HEALTH_FAIL_MAX consecutive
# failures (~60 s of a wedged server) the heartbeat stops feeding the WDT and the node
# reboots. Conservative on purpose — a false reboot-loop on a headless node is worse than a
# wedge you can physically reset, so it takes sustained failure, not one slow probe.
HEALTH_PROBE_S = 10
HEALTH_FAIL_MAX = 6

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
        # Serving health (full nodes): a loopback self-probe watches that the HTTP server
        # actually answers, so the heartbeat feeds the WDT only while the node is really
        # SERVING — not merely while the event loop cycles. c510 once fed the watchdog for
        # ages while its server was wedged; loop-liveness isn't service-liveness.
        self._health_fails = 0     # consecutive failed serving probes (0 = healthy)
        self._health_watch = False # true once serving_watch runs; else the gate is inert
        # Datagram bus-bridge, flagship side: leaves that asked (over the door `bsub` op) to
        # receive certain bus topics. ip -> {'topics', 'port', 'last_ms'}; reaped on TTL.
        self.leaf_bridges = {}
        self._rtc = machine.RTC()
        self._i2c = {}
        self._spi = {}
        # The USB device, built once at boot from installed guests (§8). None until
        # enumerate_usb() runs; None also on a node with no usb guests, which is why
        # hal.usb() checks it rather than assuming it exists.
        self.usb_plan = None
        # Cluster discovery: this node's view of its peers (one §1). Always present,
        # even for a cluster of one — a cluster of one is how a cluster of eight begins.
        self.cluster = Discovery(node)
        # Bus bridging (one §4): pull a declared slice of each peer's bus into ours.
        self.bridge = BridgeManager(self)
        # Host-owned displays (two §8, revised): the supervisor owns the panel and runs a
        # status console on it, leasing focus to a guest. Built early in boot (main.py) so
        # it can narrate wifi/ntp before the supervisor exists; reused here. Absent on a
        # node with no panel.
        self.display = getattr(node, 'display', None)
        if self.display is None:
            specs = node.settings.get('displays')
            if specs:
                try:
                    from jorm.display import DisplayManager
                    self.display = DisplayManager(specs)
                except Exception as e:
                    node.log.append('sys', 'display: init failed — %s' % e)
        if self.display is not None:
            self.display.on_note = self._note_pub

    def _note_pub(self, text):
        self.sys_publish('$sys/display',
                         {'display': self.display.primary_id, 'owner': 'host', 'status': text},
                         retain=True)

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
            if self.display is not None and not clock.status()['synced']:
                self.display.note('ntp')     # narrate the sync attempt while unsynced
            if clock.sync(self.node.log, self.node.settings.get('ntp_host')):
                if self.display is not None:
                    self.display.note('up')  # clock set — leave 'ntp' behind
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
            # Feed the watchdog unless the serving-probe says the server has been wedged for
            # HEALTH_FAIL_MAX probes running — then withhold the feed so the WDT reboots us.
            # The gate is inert on a leaf (no serving_watch), which feeds on loop-liveness.
            if wdt and not (self._health_watch and self._health_fails >= HEALTH_FAIL_MAX):
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

    async def serving_watch(self):
        """Prove the HTTP server actually ANSWERS, not just that the loop cycles. A loopback
        GET / (public, no auth) every HEALTH_PROBE_S; HEALTH_FAIL_MAX misses in a row means the
        server is wedged and the heartbeat should let the watchdog reboot us. Full nodes only —
        a leaf has no server, so it never starts this and keeps the plain loop-liveness feed."""
        if not self.node.settings.get('wdt_health', True):
            return
        self._health_watch = True
        probe_s = self.node.settings.get('wdt_probe_s', HEALTH_PROBE_S)
        port = self.node.settings.get('wdt_probe_port', self.node.port)  # defaults to the API port
        while True:
            await asyncio.sleep(probe_s)
            ok = False
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
                try:
                    w.write(b'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n')
                    await asyncio.wait_for(w.drain(), 5)
                    line = await asyncio.wait_for(r.readline(), 5)
                    ok = bool(line) and line.startswith(b'HTTP/')
                finally:
                    # Closing must never flip a good probe to failed — wait_closed is absent on
                    # some MicroPython builds, so guard it in its own swallowing try.
                    try:
                        w.close()
                        wc = getattr(w, 'wait_closed', None)
                        if wc:
                            await wc()
                    except Exception:
                        pass
            except Exception:
                ok = False
            if ok:
                if self._health_fails:
                    self.node.log.append('sys', 'serving-probe recovered after %d miss(es)'
                                         % self._health_fails)
                self._health_fails = 0
            else:
                self._health_fails += 1
                if self._health_fails == HEALTH_FAIL_MAX:
                    self.node.log.append(
                        'error', 'http server unresponsive for %d probes (~%d s) — withholding '
                                 'the watchdog feed; the node will reboot'
                                 % (HEALTH_FAIL_MAX, HEALTH_FAIL_MAX * probe_s))
