import asyncio
import json

import machine
from microdot import Microdot, send_file
from microdot.websocket import with_websocket

from jorm import guests as store
from jorm import guestcfg
from jorm.bus import BusError, valid_filter
from jorm.fsutil import UnsafePath, safe_name, write_atomic
from jorm.guests import RefusedError
from jorm.manifest import ManifestError
from jorm.claims import ClaimError
from jorm.panels import PanelError
from jorm.ring import as_json


def create_app(node, sup):
    app = Microdot()

    @app.before_request
    async def auth(req):
        if req.method == 'GET' and req.path in ('/', '/favicon.ico'):
            return  # the app shell is public; every byte of data behind it is not
        # browsers can't set headers on a WebSocket, so ?token= is accepted too
        token = req.headers.get('Authorization', '')
        token = token[7:] if token.startswith('Bearer ') else req.args.get('token', '')
        if token != node.token:
            return {'error': 'unauthorized'}, 401

    @app.get('/')
    async def index(req):
        try:
            return send_file('ui.html', max_age=0)
        except OSError:
            return {'error': 'ui.html not on this node — deploy it beside main.py'}, 404

    @app.errorhandler(RefusedError)
    async def refused(req, e):
        return {'error': str(e)}, 409

    @app.errorhandler(ManifestError)
    async def bad_manifest(req, e):
        return {'error': str(e)}, 400

    @app.errorhandler(ClaimError)
    async def claim_conflict(req, e):
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

    # -- claims ----------------------------------------------------------------

    @app.get('/api/claims')
    async def api_claims(req):
        return sup.claims.table()

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
        sub = sup.bus.subscribe([], qlen=64, owner='ws')

        async def sender():
            while True:
                topic, enc = await sub.get()
                await ws.send('{"topic": %s, "msg": %s}' % (json.dumps(topic), enc))

        task = asyncio.create_task(sender())
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
            sup.bus.unsubscribe(sub)

    def _n(req):
        try:
            return int(req.args.get('n', '50'))
        except ValueError:
            return 50

    return app
