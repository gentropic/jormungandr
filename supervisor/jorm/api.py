import asyncio
import binascii
import json
import os
import time

import machine
from microdot import Microdot, Request, Response, send_file
from microdot.websocket import with_websocket

# The UI is one 46 KB file and OTA PUTs it whole; microdot's 16 KB default drops
# the connection mid-body, which looks exactly like a dead node. We have 8 MB of
# PSRAM — a 256 KB ceiling is affordable and still a ceiling.
Request.max_content_length = 256 * 1024
Request.max_body_length = 256 * 1024

from jorm import guests as store
from jorm import fs
from jorm import usb
from jorm import guestcfg
from jorm.bus import BusError, valid_filter
from jorm.fsutil import UnsafePath, safe_name, safe_relpath, write_atomic
from jorm.guests import RefusedError
from jorm.manifest import ManifestError
from jorm.claims import ClaimError
from jorm.panels import PanelError
from jorm.ring import as_json


def _mkdirs(path):
    grown = ''
    for part in path.split('/')[:-1]:
        grown = grown + '/' + part if grown else part
        try:
            os.mkdir(grown)
        except OSError:
            pass


def _walk_staged(root, prefix=''):
    for name in os.listdir(root):
        full = root + '/' + name
        if os.stat(full)[0] & 0x4000:
            for sub in _walk_staged(full, prefix + name + '/'):
                yield sub
        else:
            yield prefix + name


def _read_marker(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def create_app(node, sup):
    app = Microdot()

    @app.before_request
    async def auth(req):
        # The app shell is public; every byte of data behind it is not. /web/* is
        # shell too — the terminal emulator and the shell that drives it are code,
        # not secrets, and a browser cannot put a bearer header on a dynamic
        # import() any more than it can on a WebSocket. The token still guards
        # every /api/ call the shell then makes, which is where the node lives.
        if req.method == 'GET' and (req.path in ('/', '/favicon.ico')
                                    or req.path.startswith('/web/')):
            return
        # The one endpoint a token cannot guard, because redeeming is how you get
        # one. What protects it is that a ticket is a 128-bit nonce that a client
        # must already have been given, and that it dies on first use.
        if req.method == 'POST' and req.path == '/api/auth/redeem':
            return
        # browsers can't set headers on a WebSocket, so ?token= is accepted too
        token = req.headers.get('Authorization', '')
        token = token[7:] if token.startswith('Bearer ') else req.args.get('token', '')
        if token != node.token:
            return {'error': 'unauthorized'}, 401

    # ── tickets: how `jorm open` hands a credential to a browser ───────────
    #
    # Not by putting the bearer token in the URL. A fragment is never sent to the
    # server — but it is written to browser history, to autocomplete, and to
    # whatever a person gets when they hit "copy link". So what goes there has to
    # be worthless a minute later. A ticket is 128 bits of urandom, good for one
    # redemption and sixty seconds. The token itself never leaves a header.
    tickets = {}

    @app.post('/api/auth/ticket')
    async def auth_ticket(req):
        now = time.ticks_ms()
        for t in [t for t, exp in tickets.items() if time.ticks_diff(exp, now) < 0]:
            del tickets[t]        # an unredeemed ticket is a loose credential
        t = binascii.hexlify(os.urandom(16)).decode()
        tickets[t] = time.ticks_add(now, 60000)
        return {'ticket': t, 'expires_in': 60}

    @app.post('/api/auth/redeem')
    async def auth_redeem(req):
        t = (req.json or {}).get('ticket', '')
        exp = tickets.pop(t, None)   # pop first: one use, in time or not
        if exp is None or time.ticks_diff(exp, time.ticks_ms()) < 0:
            return {'error': 'no such ticket — expired, spent, or never issued'}, 401
        return {'token': node.token}

    @app.get('/')
    async def index(req):
        # microdot streams a file in 1 KB chunks, so a big UI costs no RAM — only
        # wire time. Serve it pre-gzipped and even that mostly goes away. The node
        # has no compressor; the tool that pushes it does, and ships both.
        if 'gzip' in req.headers.get('Accept-Encoding', ''):
            try:
                return send_file('ui.html.gz', compressed=True,
                                 content_type='text/html', max_age=0)
            except OSError:
                pass
        try:
            return send_file('ui.html', max_age=0)
        except OSError:
            return {'error': 'ui.html not on this node — deploy it beside main.py'}, 404

    @app.get('/web/<path:path>')
    async def web(req, path):
        # The shell surface: a terminal emulator and the shell that drives it.
        # Lazily fetched by the UI on first open, so a phone loading the dashboard
        # never pays for a terminal it did not ask for. Cached hard — it only
        # changes when the supervisor does, and the supervisor reboots when it does.
        path = safe_relpath(path)
        ctype = ('text/javascript' if path.endswith('.js')
                 else 'text/css' if path.endswith('.css')
                 else 'application/octet-stream')
        if 'gzip' in req.headers.get('Accept-Encoding', ''):
            try:
                return send_file('web/' + path + '.gz', compressed=True,
                                 content_type=ctype, max_age=86400)
            except OSError:
                pass
        try:
            return send_file('web/' + path, content_type=ctype, max_age=86400)
        except OSError:
            return {'error': 'no such asset'}, 404

    @app.errorhandler(RefusedError)
    async def refused(req, e):
        return {'error': str(e)}, 409

    @app.errorhandler(ManifestError)
    async def bad_manifest(req, e):
        return {'error': str(e)}, 400

    @app.errorhandler(ClaimError)
    async def claim_conflict(req, e):
        return {'error': str(e)}, 409

    @app.errorhandler(usb.UsbError)
    async def usb_conflict(req, e):
        # Like a claim conflict: the install asks for a finite resource (endpoints)
        # that is spoken for. 409, with the per-interface breakdown from the planner.
        return {'error': str(e)}, 409

    @app.errorhandler(BusError)
    async def bus_error(req, e):
        return {'error': str(e)}, 400

    @app.errorhandler(PanelError)
    async def panel_error(req, e):
        return {'error': str(e)}, 400

    @app.errorhandler(UnsafePath)
    async def unsafe_path(req, e):
        return {'error': str(e)}, 400

    def guest_or_404(id_):
        guest = sup.guests.get(id_)
        if guest is None:
            raise RefusedError('no such guest "%s"' % id_)
        return guest

    # -- node ----------------------------------------------------------------

    @app.get('/api/node')
    async def api_node(req):
        return node.info()

    @app.get('/api/node/log')
    async def api_node_log(req):
        return {'lines': node.log.tail(_n(req))}

    @app.post('/api/node/reboot')
    async def api_node_reboot(req):
        node.log.append('sys', 'reboot requested over API')
        _reboot_soon()
        return {'rebooting': True}

    @app.put('/api/node/cluster')
    async def api_node_cluster(req):
        body = req.json
        name = (body or {}).get('name', '')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32:
            raise RefusedError('a cluster name is 1–32 characters')
        name = name.strip()
        # settings.json also holds the wifi psk and the bearer token. Read, edit
        # one key, write atomically — never regenerate a secrets file from a
        # partial idea of what was in it.
        node.settings['cluster'] = name
        write_atomic('settings.json', json.dumps(node.settings))
        node.log.append('sys', 'cluster renamed to "%s"' % name)
        return {'cluster': name}

    # -- supervisor OTA (spec §11): stage, update, trial, confirm/rollback ----

    OTA_ROOTS = ('main.py', 'boot.py', 'ui.html', 'jorm/', 'lib/', 'web/')

    @app.get('/api/node/update')
    async def api_update_status(req):
        try:
            staged = sorted(_walk_staged(store.OTA_STAGED))
        except OSError:
            staged = []
        return {
            'staged': staged,
            'trial': _read_marker('.trial'),
            'rolled_back': _read_marker('.rolled-back'),
            'version': node.info()['version'],
        }

    @app.put('/api/node/files/<path:path>')
    async def api_update_stage(req, path):
        path = safe_relpath(path)
        if not any(path == r or path.startswith(r) for r in OTA_ROOTS):
            raise RefusedError('OTA may only replace %s' % ', '.join(OTA_ROOTS))
        dest = store.OTA_STAGED + '/' + path
        _mkdirs(dest)
        write_atomic(dest, req.body)   # bytes, always — the UI ships gzipped
        return {'staged': path, 'bytes': len(req.body)}

    @app.post('/api/node/update')
    async def api_update_apply(req):
        staged = sorted(_walk_staged(store.OTA_STAGED))
        if not staged:
            raise RefusedError('nothing staged — PUT /api/node/files/<path> first')
        write_atomic('.update', ','.join(staged))
        node.log.append('sys', 'update: %d file(s) staged — rebooting to apply' % len(staged))
        _reboot_soon()
        return {'applying': staged, 'rebooting': True,
                'note': 'the next boot is a trial; it self-reverts unless the node comes back'}

    @app.delete('/api/node/update')
    async def api_update_discard(req):
        try:
            store.rmtree(store.OTA_STAGED)
        except OSError:
            pass
        return {'discarded': True}

    @app.post('/api/node/maintenance')
    async def api_node_maintenance(req):
        # The hardware WDT cannot be disarmed once armed, so a running node
        # cannot be held in the REPL long enough to deploy to it — the watchdog
        # reboots it out from under you, correctly. This is the way in: the node
        # reboots into a state where it starts nothing and arms nothing, and
        # waits at the REPL for a deploy over USB. Normal boot resumes on the
        # next reset; the flag is consumed, never sticky.
        write_atomic('.maintenance', 'requested over the API')
        node.log.append('sys', 'maintenance requested — rebooting to the REPL')
        _reboot_soon()
        return {'rebooting': True, 'into': 'maintenance'}

    def _reboot_soon():
        async def later():
            await asyncio.sleep(0.1)
            machine.reset()

        asyncio.create_task(later())

    # -- guests ----------------------------------------------------------------

    @app.get('/api/guests')
    async def api_guests(req):
        return [g.summary() for _, g in sorted(sup.guests.items())]

    @app.post('/api/guests')
    async def api_guests_create(req):
        body = req.json
        if not isinstance(body, dict):
            return {'error': 'expected JSON {manifest, files}'}, 400
        guest = store.create(sup, body.get('manifest'), body.get('files'))
        return guest.summary(), 201

    @app.get('/api/guests/<id_>')
    async def api_guest(req, id_):
        return guest_or_404(id_).detail()

    @app.delete('/api/guests/<id_>')
    async def api_guest_delete(req, id_):
        guest = guest_or_404(id_)
        if guest.state not in ('stopped', 'crashed'):
            raise RefusedError('guest "%s" is %s — stop it first' % (id_, guest.state))
        store.rmtree(guest.dir)
        del sup.guests[id_]
        sup.sys_publish('$sys/guest/%s/state' % id_, None, retain=True)  # clear the slot
        return {'removed': id_}

    @app.post('/api/guests/<id_>/start')
    async def api_guest_start(req, id_):
        return {'state': await guest_or_404(id_).start()}

    @app.post('/api/guests/<id_>/stop')
    async def api_guest_stop(req, id_):
        grace = int(req.args.get('grace_ms', '2000'))
        return {'state': await guest_or_404(id_).stop(grace)}

    @app.post('/api/guests/<id_>/restart')
    async def api_guest_restart(req, id_):
        guest = guest_or_404(id_)
        if guest.state in ('running', 'unresponsive'):
            await guest.stop()
        return {'state': await guest.start()}

    @app.post('/api/guests/<id_>/console')
    async def api_guest_console_in(req, id_):
        guest = guest_or_404(id_)
        if guest.state != 'running':
            raise RefusedError('guest "%s" is %s — nothing is listening' % (id_, guest.state))
        body = req.json
        if not isinstance(body, dict) or not isinstance(body.get('line'), str):
            return {'error': 'expected {"line": "..."}'}, 400
        guest.send_input(body['line'])
        return {'sent': body['line']}

    @app.get('/api/guests/<id_>/config')
    async def api_guest_config_get(req, id_):
        return guestcfg.view(guest_or_404(id_))

    @app.put('/api/guests/<id_>/config')
    async def api_guest_config_put(req, id_):
        return guestcfg.write(guest_or_404(id_), req.json)

    @app.get('/api/guests/<id_>/console')
    async def api_guest_console(req, id_):
        return {'lines': guest_or_404(id_).console.tail(_n(req))}

    @app.get('/api/guests/<id_>/files/<name>')
    async def api_guest_file_get(req, id_, name):
        guest = guest_or_404(id_)
        try:
            with open(guest.dir + '/' + safe_name(name)) as f:
                return f.read()
        except OSError:
            return {'error': 'no such file'}, 404

    @app.put('/api/guests/<id_>/files/<name>')
    async def api_guest_file_put(req, id_, name):
        guest = guest_or_404(id_)
        if guest.state not in ('stopped', 'crashed'):
            raise RefusedError('guest "%s" is %s — stop it before editing the bundle'
                               % (id_, guest.state))
        write_atomic(guest.dir + '/' + safe_name(name), req.body.decode())
        return {'written': name}

    @app.route('/api/guests/<id_>/console/stream')
    @with_websocket
    async def api_guest_console_ws(req, ws, id_):
        guest = guest_or_404(id_)
        tap = guest.console.tap(qlen=64)

        async def sender():
            while True:
                await ws.send(json.dumps(as_json(await tap.get())))

        for line in guest.console.tail(20):
            await ws.send(json.dumps(line))
        task = asyncio.create_task(sender())
        try:
            while True:
                await ws.receive()  # nothing to hear; a close raises us out
        finally:
            task.cancel()
            guest.console.untap(tap)

    # -- the shared library store (spec §1/§6) --------------------------------

    def importers(name):
        """Which installed guests import this library? A static scan of bundle
        sources at install time — cheap, and its one blind spot is stated: there
        is no dynamic import in the guest SDK, so there is nothing for it to miss.
        """
        users = []
        for gid, guest in sup.guests.items():
            try:
                names = os.listdir(guest.dir)
            except OSError:
                continue
            for fn in names:
                if not fn.endswith('.py'):
                    continue
                try:
                    with open(guest.dir + '/' + fn) as f:
                        src_ = f.read()
                except OSError:
                    continue
                for line in src_.split('\n'):
                    line = line.strip()
                    if ((line.startswith('import ') and name in line.split()) or
                            (line.startswith('from ') and line.split()[1].split('.')[0] == name)):
                        users.append(gid)
                        break
                if gid in users:
                    break
        return users

    @app.get('/api/lib')
    async def api_lib(req):
        out = []
        try:
            names = sorted(os.listdir(store.LIB_DIR))
        except OSError:
            names = []
        for fn in names:
            if fn.endswith('.py') and fn[:-3] not in RESERVED_LIBS:
                mod = fn[:-3]
                out.append({'name': mod,
                            'bytes': os.stat(store.LIB_DIR + '/' + fn)[6],
                            'imported_by': importers(mod)})
        return out

    # The supervisor's own vendored deps share lib/ with the guest library store
    # (spec §1 names it /lib, and MicroPython already has it on sys.path). They
    # are packages — lib/microdot/ — so a lib/<name>.py cannot collide by
    # accident, but it could on purpose. Say no.
    RESERVED_LIBS = ('microdot',)

    @app.put('/api/lib/<name>')
    async def api_lib_put(req, name):
        mod = safe_name(name if name.endswith('.py') else name + '.py')[:-3]
        if mod in RESERVED_LIBS:
            raise RefusedError('"%s" is the supervisor\'s own — not yours to replace' % mod)
        running = [g for g in importers(mod) if sup.guests[g].state == 'running']
        if running and req.args.get('force') is None:
            raise RefusedError(
                'library "%s" is imported by running guest(s) %s — they keep their '
                'loaded copy until restart regardless (Python caches modules); '
                '?force if you know this' % (mod, ', '.join(running)))
        write_atomic(store.LIB_DIR + '/' + mod + '.py', req.body.decode())
        return {'installed': mod, 'imported_by': importers(mod)}

    @app.delete('/api/lib/<name>')
    async def api_lib_delete(req, name):
        mod = safe_name(name if name.endswith('.py') else name + '.py')[:-3]
        users = importers(mod)
        if users and req.args.get('force') is None:
            raise RefusedError('library "%s" is imported by %s — refused'
                               % (mod, ', '.join(users)))
        try:
            os.remove(store.LIB_DIR + '/' + mod + '.py')
        except OSError:
            return {'error': 'no such library'}, 404
        return {'removed': mod}

    # -- the filesystem (spec §6): what a shell mounts -------------------------

    @app.errorhandler(fs.FsError)
    async def fs_error(req, e):
        return {'error': str(e)}, 403

    @app.get('/api/fs')
    @app.get('/api/fs/<path:path>')
    async def api_fs_get(req, path=''):
        path = fs.norm(path)
        if fs.is_dir(path):
            return {'path': '/' + path, 'dir': True, 'entries': fs.listdir(path)}
        return Response(body=fs.read(path),
                        headers={'Content-Type': 'application/octet-stream'})

    @app.put('/api/fs/<path:path>')
    async def api_fs_put(req, path):
        path = fs.norm(path)
        fs.write(path, req.body)
        return {'written': '/' + path, 'bytes': len(req.body)}

    @app.delete('/api/fs/<path:path>')
    async def api_fs_delete(req, path):
        path = fs.norm(path)
        fs.remove(path)
        return {'removed': '/' + path}

    @app.post('/api/fs/<path:path>')
    async def api_fs_post(req, path):
        path = fs.norm(path)
        op = req.args.get('op')
        if op == 'mkdir':
            fs.mkdir(path)
            return {'created': '/' + path}
        if op == 'rename':
            dst = fs.norm((req.json or {}).get('to', ''))
            fs.rename(path, dst)
            return {'renamed': '/' + path, 'to': '/' + dst}
        if op == 'stat':
            return fs.stat(path)
        return {'error': 'op is mkdir | rename | stat'}, 400

    # -- claims ----------------------------------------------------------------

    @app.get('/api/claims')
    async def api_claims(req):
        return sup.claims.table()

    # -- usb (spec §8: virtual hardware, fixed at boot) ----------------------

    @app.get('/api/usb')
    async def api_usb(req):
        # None until enumerate_usb() has run; a node with no usb guests reports an
        # empty applied plan rather than null, so the UI can say "nothing on the
        # bus" instead of guessing.
        if sup.usb_plan is None:
            return {'interfaces': [], 'endpoints_used': 0,
                    'endpoints_total': usb.EP_BUDGET, 'applied': False,
                    'pending': False, 'error': None}
        p = sup.usb_plan.info()
        # Does the plan the host is looking at still match what is installed? If a
        # usb guest was added or removed since boot, the answer is "reboot to
        # re-enumerate" — surfaced here so the UI can say it in amber.
        try:
            want = usb.plan(sup.guests.values())
            p['pending'] = ([g.info() for g in want.grants]
                            != [i for i in p['interfaces']])
        except usb.UsbError:
            p['pending'] = True
        return p

    @app.post('/api/usb/replan')
    async def api_usb_replan(req):
        # Re-enumeration means the host sees the whole device drop and come back,
        # which cannot happen without a reboot on this silicon. So this does not
        # quietly rebuild the descriptor — it reboots, and the new descriptor is
        # built at the next boot from whatever is installed then (§8).
        node.log.append('sys', 'usb replan requested — rebooting to re-enumerate')
        _reboot_soon()
        return {'rebooting': True, 'reason': 'usb re-enumeration needs a power cycle'}

    # -- bus (spec §5: port mirroring is the debugging feature) ---------------

    @app.post('/api/bus/publish')
    async def api_bus_publish(req):
        body = req.json
        if not isinstance(body, dict) or 'topic' not in body:
            return {'error': 'expected {topic, msg, retain?}'}, 400
        delivered = sup.bus.publish(body['topic'], body.get('msg'),
                                    retain=bool(body.get('retain')), owner='api')
        return {'published': body['topic'], 'delivered': delivered}

    @app.get('/api/bus/retained')
    async def api_bus_retained(req):
        return sup.bus.retained_table()

    @app.get('/api/bus/subs')
    async def api_bus_subs(req):
        return [dict(s.info(), owner=s.owner) for s in sup.bus.subs]

    @app.route('/api/bus')
    @with_websocket
    async def api_bus_ws(req, ws):
        # A browser on a LAN can keep up with a few hundred messages a second;
        # 64 was a guest's budget, not a monitor's.
        sub = sup.bus.subscribe([], qlen=256, owner='ws')

        async def sender():
            while True:
                topic, enc = await sub.get()
                await ws.send('{"topic": %s, "msg": %s}' % (json.dumps(topic), enc))

        async def drops():
            # A monitor that quietly drops is a monitor that lies about what the
            # bus carried. The node never starves for a slow browser (spec §5) —
            # but the browser has to be told that it is the one falling behind.
            last = 0
            while True:
                await asyncio.sleep(2)
                if sub.drops != last:
                    last = sub.drops
                    await ws.send('{"drops": %d}' % last)

        task = asyncio.create_task(sender())
        dtask = asyncio.create_task(drops())
        try:
            while True:
                try:
                    frame = json.loads(await ws.receive())
                except ValueError:
                    await ws.send('{"error": "bad json"}')
                    continue
                op = frame.get('op') if isinstance(frame, dict) else None
                if op == 'sub':
                    filters = [f for f in frame.get('filters', []) if valid_filter(f)]
                    sub.filters = filters
                    sup.bus.deliver_retained(sub, filters)
                elif op == 'pub':
                    try:
                        sup.bus.publish(frame.get('topic'), frame.get('msg'),
                                        retain=bool(frame.get('retain')), owner='ws')
                    except BusError as e:
                        await ws.send(json.dumps({'error': str(e)}))
                else:
                    await ws.send('{"error": "unknown op"}')
        finally:
            task.cancel()
            dtask.cancel()
            sup.bus.unsubscribe(sub)

    def _n(req):
        try:
            return int(req.args.get('n', '50'))
        except ValueError:
            return 50

    return app
