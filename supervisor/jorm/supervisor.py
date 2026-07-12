"""The supervisor: current-guest register, watchdog, import guard, guest set.

Attribution by evidence (spec §1): the register names whoever holds the CPU,
mirrored into RTC memory so a WDT reset can still name its culprit.
"""
import asyncio
import builtins
import os
import time

import machine

from jorm.claims import Claims
from jorm.guests import Guest, GUESTS_DIR, ensure_dir, write_atomic

RTC_TAG = b'jorm!'

# spec §1: the built-in whitelist of pure stdlib a guest may import
IMPORT_WHITELIST = ('json', 'math', 'struct', 'collections', 'binascii', 're')


class Supervisor:
    def __init__(self, node):
        self.node = node
        self.claims = Claims(reserved_pins=node.settings.get('reserved_pins', []))
        self.guests = {}
        self.current = None    # guest id holding the CPU right now
        self.last_seen = None  # last guest to hold it (soft-flagging evidence)
        self._flagged = None
        self._rtc = machine.RTC()

    # -- current-guest register (every hal await passes through here) ------

    def resume(self, gid):
        self.current = gid
        self.last_seen = gid
        self._rtc.memory(RTC_TAG + gid.encode())
        if self._flagged == gid:
            self._flagged = None
            guest = self.guests.get(gid)
            if guest and guest.state == 'unresponsive':
                guest.state = 'running'
                guest.console.append('sys', 'yielding again — back to running')

    def park(self, gid):
        if self.current == gid:
            self.current = None
            self._rtc.memory(b'')

    # -- import guard (spec §1: a guest never imports machine et al.) ------

    def install_import_guard(self):
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
                    guest.state = 'unresponsive'
                    guest.console.append('sys',
                                         'starved the loop for %d ms — flagged unresponsive' % gap)
                    self._flagged = guest.id
