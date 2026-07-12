"""Guest bundles and lifecycle (spec §1, §2): stopped → starting → running →
stopping → stopped, plus crashed and unresponsive."""
import asyncio
import io
import json
import os
import sys
import time

from jorm.claims import ClaimError
from jorm.hal import Hal
from jorm.manifest import validate, ManifestError
from jorm.ring import Ring

GUESTS_DIR = 'guests'
_NAME_CHARS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-'


class RefusedError(Exception):
    """Lifecycle or claim refusal — maps to HTTP 409."""


def safe_name(name):
    if (not name or name.startswith('.') or '..' in name
            or any(c not in _NAME_CHARS for c in name)):
        raise RefusedError('unsafe file name %r' % name)
    return name


def write_atomic(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(data)
    os.rename(tmp, path)


def ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        pass


def rmtree(path):
    for name in os.listdir(path):
        sub = path + '/' + name
        if os.stat(sub)[0] & 0x4000:
            rmtree(sub)
        else:
            os.remove(sub)
    os.rmdir(path)


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
        self._child_error = None
        self._crash_count = 0

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

    def summary(self):
        m = self.manifest or {}
        return {
            'id': self.id,
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
        return d

    # -- lifecycle ---------------------------------------------------------

    async def start(self, manual=True):
        if self.state not in ('stopped', 'crashed'):
            raise RefusedError('guest "%s" is %s' % (self.id, self.state))
        if manual:
            self._crash_count = 0
        self.state = 'starting'
        self.console.append('sys', 'starting')
        try:
            self.load_manifest()
            self.sup.claims.grant(self.id, self.manifest.get('caps', {}))
        except OSError:
            self.state = 'stopped'
            raise RefusedError('guest "%s" has no readable manifest.json' % self.id)
        except (ManifestError, ClaimError) as e:
            self.state = 'stopped'
            self.console.append('sys', 'refused: %s' % e)
            raise RefusedError(str(e))

        self.traceback = None
        self._child_error = None
        entry = self.manifest.get('entry', 'main.py')
        ns = {'__name__': '__guest__'}
        try:
            with open(self.dir + '/' + entry) as f:
                src = f.read()
            self.sup.resume(self.id)  # import guard applies to load-time code too
            try:
                exec(compile(src, self.dir + '/' + entry, 'exec'), ns)
            finally:
                self.sup.park(self.id)
            run = ns.get('run')
            if run is None:
                raise ManifestError('%s defines no run(hal) (spec §1)' % entry)
        except Exception as e:
            self.sup.claims.release(self.id)
            self._capture(e)
            self.state = 'crashed'
            self.console.append('sys', 'crashed while loading')
            return self.state

        self.hal = Hal(self.sup, self)
        self.state = 'running'
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
            self.state = 'stopped'
            if (self.manifest.get('restart') == 'always'):
                self._schedule_restart()
        except asyncio.CancelledError:
            if self._child_error is not None:
                self.state = 'crashed'
                self.console.append('sys', 'crashed (child task)')
                self._maybe_restart()
            else:
                self.console.append('sys', 'stopped')
                self.state = 'stopped'
        except Exception as e:
            self._capture(e)
            self.state = 'crashed'
            self._maybe_restart()
        finally:
            for child in self.children:
                child.cancel()
            self.children = []
            self.sup.claims.release(self.id)
            self.status = ''

    async def stop(self, grace_ms=2000):
        if self.state not in ('running', 'unresponsive'):
            raise RefusedError('guest "%s" is %s' % (self.id, self.state))
        self.state = 'stopping'
        self.console.append('sys', 'stopping (grace %d ms)' % grace_ms)
        self.task.cancel()
        deadline = time.ticks_add(time.ticks_ms(), grace_ms)
        while self.state == 'stopping':
            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                self.state = 'unresponsive'
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
    sup.guests[id_] = guest
    return guest
