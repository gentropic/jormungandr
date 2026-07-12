import asyncio

import machine
from microdot import Microdot


def create_app(node):
    app = Microdot()

    @app.before_request
    async def auth(req):
        if req.headers.get('Authorization') != 'Bearer ' + node.token:
            return {'error': 'unauthorized'}, 401

    @app.get('/api/node')
    async def api_node(req):
        return node.info()

    @app.get('/api/node/log')
    async def api_node_log(req):
        try:
            n = int(req.args.get('n', '50'))
        except ValueError:
            return {'error': 'bad n'}, 400
        return {'lines': node.log.tail(n)}

    @app.post('/api/node/reboot')
    async def api_node_reboot(req):
        node.log.append('sys', 'reboot requested over API')

        async def later():
            await asyncio.sleep(0.1)
            machine.reset()

        asyncio.create_task(later())
        return {'rebooting': True}

    return app
