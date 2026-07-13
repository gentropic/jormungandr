"""Smart leaf: a leaf that HOSTS guests (SPEC-two spectrum, the mpy middle tier).

The experiment behind the design: keep the guest machinery (hal, guests, bus, claims),
drop the HTTP server. Guests run locally; their bus traffic is pushed up to a flagship
over one outbound connection. No server means lwIP breathes — the thing that blocked a
C3 was the listen socket, not the guests. The trade is mpy's soft isolation on a single
core: a runaway guest can wedge the node (no preemption), and there is no memory wall.

Everything here is the full node MINUS create_app/start_server, PLUS the push uplink.
"""
import asyncio
import json

from jorm import wsclient


def _host_port(url):
    rest = url.split('://', 1)[-1].rstrip('/')
    if ':' in rest:
        h, p = rest.rsplit(':', 1)
        return h, int(p)
    return rest, 80


async def _push_uplink(node, sup):
    """Forward this leaf's own bus traffic up to a flagship.

    The dumb leaf pushed a fixed set of sensor topics; a smart leaf pushes whatever
    its guests publish. Subscribe to the local bus and republish each message on the
    flagship — skipping $-roots (node-private telemetry) and anything imported
    (origin set), which is the same split-horizon the bridge uses so a two-way link
    cannot loop.
    """
    flagship = node.settings.get('flagship')
    if not flagship:
        node.log.append('error', 'leaf-host: no "flagship" — guests run but nothing hears them')
        return
    host, port = _host_port(flagship)
    while True:
        ws = None
        sub = None
        try:
            ws = await wsclient.connect(host, port, '/api/bus', node.token)
            node.log.append('sys', 'leaf-host: uplink to %s' % flagship)
            sub = sup.bus.subscribe(['#'], qlen=64, owner='uplink')
            while True:
                topic, enc, origin = await sub.get()
                if topic.startswith('$') or origin is not None:
                    continue
                await ws.send('{"op": "pub", "topic": %s, "msg": %s}'
                              % (json.dumps(topic), enc))
        except (OSError, EOFError, ValueError) as e:
            node.log.append('sys', 'leaf-host: uplink down (%s) — retrying' % e)
        finally:
            if sub is not None:
                sup.bus.unsubscribe(sub)
            if ws is not None:
                await ws.close()
        await asyncio.sleep(3)


def run_leaf_host(node):
    # Import the guest machinery — hal, guests, bus, claims — but never the API. This
    # is the ~63 KB of the supervisor without the ~10 KB (and the whole lwIP appetite)
    # of the server.
    from jorm.supervisor import Supervisor
    sup = Supervisor(node)
    sup.blame_check()          # honor a watchdog reset: name and bench the culprit
    sup.scan()
    sup.install_import_guard()

    async def _amain():
        asyncio.create_task(sup.heartbeat())    # WDT + runaway detection (the point)
        asyncio.create_task(sup.telemetry())    # heap/temp on the local bus
        asyncio.create_task(_push_uplink(node, sup))
        await sup.autostart()                   # start the guests marked autostart
        node.log.append('sys', 'leaf-host: %d guest(s) installed, hosting locally'
                        % len(sup.guests))
        while True:
            await asyncio.sleep(3600)

    asyncio.run(_amain())
