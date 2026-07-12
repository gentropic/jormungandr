import asyncio

import machine
from microdot import Microdot

from jorm import guests as store
from jorm.guests import RefusedError, safe_name, write_atomic
from jorm.manifest import ManifestError
from jorm.claims import ClaimError


def create_app(node, sup):
    app = Microdot()

    @app.before_request
    async def auth(req):
        if req.headers.get('Authorization') != 'Bearer ' + node.token:
            return {'error': 'unauthorized'}, 401

    @app.errorhandler(RefusedError)
    async def refused(req, e):
        return {'error': str(e)}, 409

    @app.errorhandler(ManifestError)
    async def bad_manifest(req, e):
        return {'error': str(e)}, 400

    @app.errorhandler(ClaimError)
    async def claim_conflict(req, e):
        return {'error': str(e)}, 409

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

        async def later():
            await asyncio.sleep(0.1)
            machine.reset()

        asyncio.create_task(later())
        return {'rebooting': True}

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

    # -- claims ----------------------------------------------------------------

    @app.get('/api/claims')
    async def api_claims(req):
        return sup.claims.table()

    def _n(req):
        try:
            return int(req.args.get('n', '50'))
        except ValueError:
            return 50

    return app
