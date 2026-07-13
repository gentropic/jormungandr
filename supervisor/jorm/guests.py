"""Guest bundles and lifecycle (spec §1, §2): stopped → starting → running →
stopping → stopped, plus crashed and unresponsive."""
import asyncio
import gc
import io
import json
import os
import sys
import time

from jorm import guestcfg
from jorm.claims import ClaimError
from jorm.fsutil import UnsafePath, ensure_dir, rmtree, safe_name, write_atomic
from jorm.hal import Hal
from jorm.manifest import validate, ManifestError
from jorm.ring import Ring

GUESTS_DIR = 'guests'
LIB_DIR = 'lib'
OTA_STAGED = 'staged'
MEM_RESERVE = 32 * 1024  # what the supervisor keeps for itself before any grant
FIRST_NUM = 100          # guests are numbered from 100, Proxmox-style


class RefusedError(Exception):
    """Lifecycle or claim refusal — maps to HTTP 409."""


class Guest:
    def __init__(self, sup, id_):
        self.sup = sup
        self.id = id_
        self.state = 'stopped'
        self.status = ''
        self.manifest = None
        self.console = Ring(200, echo=False)
        self.task = None
        self.children = []
        self.traceback = None
        self.hal = None
        self.subs = []
        self.cfg_values = {}
        self.cfg_schema = None
        self.cfg_pending = set()
        self.cfg_watchers = []
        self.num = None          # VMID-ish: assigned at install, stable for life
        self.mem = []            # sampled heap attributed to this guest (spec §2)
        self._child_error = None
        self._crash_count = 0

    def set_state(self, state):
        self.state = state
        self.sup.sys_publish('$sys/guest/%s/state' % self.id, {'state': state},
                             retain=True)

    def bus_grants(self):
        caps = (self.manifest or {}).get('caps', {})
        if 'bus' not in caps:
            return None
        bus = caps['bus'] or {}
        pub = bus['pub'] if 'pub' in bus else [self.id + '/#']
        sub = bus['sub'] if 'sub' in bus else []
        return (pub, sub)

    @property
    def dir(self):
        return GUESTS_DIR + '/' + self.id

    def load_manifest(self):
        with open(self.dir + '/manifest.json') as f:
            self.manifest = validate(json.load(f))

    def suspected(self):
        try:
            with open(self.dir + '/.suspected') as f:
                return f.read()
        except OSError:
            return None

    def load_num(self):
        try:
            with open(self.dir + '/.num') as f:
                self.num = int(f.read())
        except (OSError, ValueError):
            self.num = None
        return self.num

    def summary(self):
        m = self.manifest or {}
        return {
            'id': self.id,
            'num': self.num,
            'name': m.get('name', self.id),
            'state': self.state,
            'status': self.status,
            'autostart': bool(m.get('autostart')),
            'restart': m.get('restart', 'never'),
            'caps': sorted((m.get('caps') or {}).keys()),
            'suspected': self.suspected(),
        }

    def detail(self):
        d = self.summary()
        d['manifest'] = self.manifest
        d['claims'] = self.sup.claims.for_guest(self.id)
        d['traceback'] = self.traceback
        d['bus'] = {'subs': [s.info() for s in self.subs],
                    'published': self.sup.bus.pub_counts.get(self.id, 0)}
        # Sampled, not measured — MicroPython has no per-task heap (spec §10).
        # The supervisor attributes each heartbeat's allocation delta to whichever
        # guest held the CPU. Honest label in the UI: "sampled", never "used".
        d['mem'] = {'sampled_kb': [round(k, 1) for k in self.mem],
                    'declared_kb': (self.manifest or {}).get('caps', {}).get('mem_kb')}
        return d

    # -- lifecycle ---------------------------------------------------------

    async def start(self, manual=True):
        if self.state not in ('stopped', 'crashed'):
            raise RefusedError('guest "%s" is %s' % (self.id, self.state))
        if manual:
            self._crash_count = 0
        self.set_state('starting')
        self.console.append('sys', 'starting')
        try:
            self.load_manifest()
            self.sup.claims.grant(self.id, self.manifest.get('caps', {}))
        except OSError:
            self.set_state('stopped')
            raise RefusedError('guest "%s" has no readable manifest.json' % self.id)
        except (ManifestError, ClaimError) as e:
            self.set_state('stopped')
            self.console.append('sys', 'refused: %s' % e)
            raise RefusedError(str(e))

        need = self.manifest.get('caps', {}).get('mem_kb')
        if need:
            gc.collect()
            if gc.mem_free() < need * 1024 + MEM_RESERVE:
                self.sup.claims.release(self.id)
                self.set_state('stopped')
                msg = ('declared mem_kb %d + reserve exceeds free heap (%d KB free)'
                       % (need, gc.mem_free() // 1024))
                self.console.append('sys', 'refused: ' + msg)
                raise RefusedError(msg)

        guestcfg.load(self)
        self.traceback = None
        self._child_error = None
        entry = self.manifest.get('entry', 'main.py')
        ns = {'__name__': '__guest__'}
        try:
            with open(self.dir + '/' + entry) as f:
                src = f.read()
            # Bundle first, then /lib (spec §1). Only for the load: imports belong
            # at module level, and a lazy import inside a running guest would race
            # every other guest's path — we have been bitten by exactly that.
            saved = sys.path[:]
            sys.path[:] = [self.dir, LIB_DIR] + saved
            self.sup.resume(self.id)  # the guard applies to load-time code too
            try:
                exec(compile(src, self.dir + '/' + entry, 'exec'), ns)
            finally:
                self.sup.park(self.id)
                sys.path[:] = saved
            run = ns.get('run')
            if run is None:
                raise ManifestError('%s defines no run(hal) (spec §1)' % entry)
        except Exception as e:
            self.sup.claims.release(self.id)
            self._capture(e)
            self.set_state('crashed')
            self.console.append('sys', 'crashed while loading')
            return self.state

        self.hal = Hal(self.sup, self)
        self.cfg_pending = set()  # restart applies pending values (spec §7)
        self.set_state('running')
        self.console.append('sys', 'running')
        self.task = asyncio.create_task(self._runner(run))
        return self.state

    async def _runner(self, run):
        try:
            self.sup.resume(self.id)
            try:
                await run(self.hal)
            finally:
                self.sup.park(self.id)
            self.console.append('sys', 'clean exit')
            self.set_state('stopped')
            if (self.manifest.get('restart') == 'always'):
                self._schedule_restart()
        except asyncio.CancelledError:
            if self._child_error is not None:
                self.set_state('crashed')
                self.console.append('sys', 'crashed (child task)')
                self._maybe_restart()
            else:
                self.console.append('sys', 'stopped')
                self.set_state('stopped')
        except Exception as e:
            self._capture(e)
            self.set_state('crashed')
            self._maybe_restart()
        finally:
            for child in self.children:
                child.cancel()
            self.children = []
            for sub in self.subs:
                self.sup.bus.unsubscribe(sub)
            self.subs = []
            self.cfg_watchers = []
            self.sup.claims.release(self.id)
            self.status = ''

    async def stop(self, grace_ms=2000):
        if self.state not in ('running', 'unresponsive'):
            raise RefusedError('guest "%s" is %s' % (self.id, self.state))
        self.set_state('stopping')
        self.console.append('sys', 'stopping (grace %d ms)' % grace_ms)
        self.task.cancel()
        deadline = time.ticks_add(time.ticks_ms(), grace_ms)
        while self.state == 'stopping':
            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                self.set_state('unresponsive')
                self.console.append('sys', 'still alive after grace — unresponsive (cannot preempt)')
                break
            await asyncio.sleep_ms(50)
        return self.state

    def child_crashed(self, e):
        self._capture(e)
        self._child_error = e
        if self.task:
            self.task.cancel()

    def _capture(self, e):
        buf = io.StringIO()
        sys.print_exception(e, buf)
        self.traceback = buf.getvalue()
        self.console.append('error', self.traceback)

    def _maybe_restart(self):
        if self.manifest.get('restart', 'never') in ('on-crash', 'always'):
            self._schedule_restart()

    def _schedule_restart(self):
        if self._crash_count >= 5:
            self.console.append('sys', 'restart limit reached (5) — staying down')
            return
        delay = 1 << self._crash_count  # 1, 2, 4, 8, 16 s
        self._crash_count += 1
        self.console.append('sys', 'restart in %d s (try %d/5)' % (delay, self._crash_count))
        asyncio.create_task(self._delayed_restart(delay))

    async def _delayed_restart(self, delay):
        await asyncio.sleep(delay)
        if self.state in ('crashed', 'stopped'):
            try:
                await self.start(manual=False)
            except RefusedError as e:
                self.console.append('sys', 'restart refused: %s' % e)


def create(sup, manifest, files):
    m = validate(manifest)
    id_ = m['id']
    if id_ in sup.guests:
        raise RefusedError('guest "%s" already exists' % id_)
    if not isinstance(files, dict) or not files:
        raise ManifestError('bundle has no files')
    entry = m.get('entry', 'main.py')
    if entry not in files:
        raise ManifestError('bundle has no entry file %r' % entry)
    for name in files:
        safe_name(name)

    ensure_dir(GUESTS_DIR)
    path = GUESTS_DIR + '/' + id_
    try:
        os.mkdir(path)
    except OSError:
        raise RefusedError('guest "%s" already exists on flash' % id_)
    write_atomic(path + '/manifest.json', json.dumps(m))
    for name, content in files.items():
        write_atomic(path + '/' + name, content)

    guest = Guest(sup, id_)
    guest.manifest = m
    nums = [g.num for g in sup.guests.values() if g.num]
    guest.num = max(nums) + 1 if nums else FIRST_NUM
    write_atomic(path + '/.num', str(guest.num))
    sup.guests[id_] = guest
    return guest
