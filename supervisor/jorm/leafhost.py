"""Smart leaf: a leaf that HOSTS guests (SPEC-two §7), managed over the bus.

Keep the guest machinery (hal, guests, bus, claims), drop the HTTP server. Guests run
locally; the node has no API for a flagship to call, so management rides the one thing
it does have — the bus. Over a single outbound WebSocket the leaf:

  - pushes its guests' bus traffic up to the flagship (so a guest here is heard there);
  - forwards its guests' STATE up, namespaced as leaf/<name>/guest/<id>, so the
    flagship can list them without an /api/guests to call;
  - subscribes to cmd/leaf/<name>/# and executes start/stop/restart/rm/install against
    its own Supervisor — a flagship manages a mini node's guests with no server on it.

The lwIP cost is still one client connection; the server, and its listen socket, stay
gone. What a smart leaf costs is trust, not memory (mpy soft isolation on one core).
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


async def _push(ws, sub, up_prefix):
    """Forward local bus traffic to the flagship. Guest states are rewritten under
    leaf/<name>/guest/<id> (retained) so the flagship can see them; other $-roots are
    node-private and stay home; imports (origin set) are never re-forwarded."""
    while True:
        topic, enc, origin = await sub.get()
        if origin is not None:
            continue
        if topic.startswith('$sys/guest/'):
            gid = topic.split('/')[2]
            await ws.send('{"op": "pub", "retain": true, "topic": %s, "msg": %s}'
                          % (json.dumps(up_prefix + 'guest/' + gid), enc))
        elif topic.startswith('$'):
            continue
        else:
            await ws.send('{"op": "pub", "topic": %s, "msg": %s}' % (json.dumps(topic), enc))


async def _exec(node, sup, store, ws, verb, msg, up_prefix):
    """Run one management command against the local Supervisor and report the result."""
    gid = msg.get('guest')
    req = msg.get('req')
    ok, err = True, None
    try:
        if verb == 'start':
            await sup.guests[gid].start()
        elif verb == 'stop':
            await sup.guests[gid].stop()
        elif verb == 'restart':
            g = sup.guests[gid]
            if g.state in ('running', 'unresponsive'):
                await g.stop()
            await g.start()
        elif verb == 'rm':
            g = sup.guests[gid]
            if g.state in ('running', 'unresponsive'):
                await g.stop()
            store.rmtree(g.dir)
            del sup.guests[gid]
            sup.sys_publish('$sys/guest/%s/state' % gid, None, retain=True)  # clear it
        elif verb == 'install':
            # A bundle fits in one 4 KB bus message for a small guest (the common leaf
            # case); a larger one is a later, chunked slice.
            g = store.create(sup, msg.get('manifest'), msg.get('files'))
            # publish its state so it appears in the roster at once, not only after
            # its first start (store.create does not transition state)
            sup.sys_publish('$sys/guest/%s/state' % g.id, {'state': g.state}, retain=True)
        else:
            ok, err = False, 'unknown verb %r' % verb
    except KeyError:
        ok, err = False, 'no guest %r on this leaf' % gid
    except Exception as e:
        ok, err = False, str(e)
    node.log.append('sys', 'leaf-host: %s %s -> %s'
                    % (verb, gid or '', 'ok' if ok else err))
    await ws.send(json.dumps({
        'op': 'pub', 'topic': up_prefix + 'result',
        'msg': {'req': req, 'verb': verb, 'guest': gid, 'ok': ok, 'error': err}}))


async def _uplink(node, sup):
    from jorm import guests as store
    flagship = node.settings.get('flagship')
    if not flagship:
        node.log.append('error', 'leaf-host: no "flagship" — guests run but nothing hears them')
        while True:
            await asyncio.sleep(30)
    host, port = _host_port(flagship)
    myname = node.hostname
    cmd_prefix = 'cmd/leaf/%s/' % myname
    up_prefix = 'leaf/%s/' % myname

    while True:
        ws = None
        sub = None
        pusher = None
        ka_task = None
        try:
            ws = await wsclient.connect(host, port, '/api/bus', node.token)
            node.log.append('sys', 'leaf-host: uplink to %s' % flagship)
            ka_task = asyncio.create_task(wsclient.keepalive(ws))
            await ws.send(json.dumps({'op': 'sub', 'filters': [cmd_prefix + '#']}))
            await ws.send(json.dumps({
                'op': 'pub', 'retain': True, 'topic': '$sys/leaf/' + myname,
                'msg': {'name': myname, 'board': node.board_name(), 'hosts_guests': True}}))
            # '#' deliberately does not match $-roots (the $SYS convention), so ask
            # for $sys/guest/# explicitly — that is how the leaf's guest STATES reach
            # _push to be forwarded up as leaf/<name>/guest/<id>.
            sub = sup.bus.subscribe(['#', '$sys/guest/#'], qlen=64, owner='uplink')
            pusher = asyncio.create_task(_push(ws, sub, up_prefix))
            # Publish every installed guest's current state, so the flagship sees the
            # whole roster — not only guests that happen to have changed state since
            # boot. You cannot start a guest you cannot see.
            for g in sup.guests.values():
                sup.sys_publish('$sys/guest/%s/state' % g.id, {'state': g.state}, retain=True)
            while True:
                frame = json.loads(await ws.recv())
                topic = frame.get('topic', '')
                if topic.startswith(cmd_prefix):
                    await _exec(node, sup, store, ws, topic[len(cmd_prefix):],
                                frame.get('msg') or {}, up_prefix)
        except (OSError, EOFError, ValueError) as e:
            node.log.append('sys', 'leaf-host: uplink down (%s) — retrying' % e)
        finally:
            if pusher is not None:
                pusher.cancel()
            if ka_task is not None:
                ka_task.cancel()
            if sub is not None:
                sup.bus.unsubscribe(sub)
            if ws is not None:
                await ws.close()
        await asyncio.sleep(3)


def _mac_bytes(s):
    try:
        return bytes(int(p, 16) for p in s.split(':'))
    except (ValueError, AttributeError):
        return None


class _EspNowUplink:
    """Present the espnow link to _push/_exec with the same send/recv a WS gives them,
    so the guest-management logic is reused unchanged. There is only one gateway, so
    recv drops the sender MAC."""

    def __init__(self, link, gw):
        self.link = link
        self.gw = gw

    async def send(self, text):
        return await self.link.send(self.gw, text)

    async def recv(self):
        _mac, text = await self.link.recv()
        return text

    async def close(self):
        pass


async def _espnow_uplink(node, sup):
    from jorm import guests as store
    from jorm.espnow import EspNowLink
    link = EspNowLink(node.token)
    gw = _mac_bytes(node.settings.get('gateway_mac', ''))
    if gw is None:
        # No seeded gateway — scan channels for a HELLO for our cluster (§5). This can
        # take a few seconds; retry forever, since a leaf with no gateway has nothing
        # else to do.
        cluster = node.settings.get('cluster') or 'Cluster'
        while gw is None:
            node.log.append('sys', 'leaf-host: scanning for an espnow gateway (%s)' % cluster)
            gw, ch = await link.scan_for_gateway(cluster, node.wlan)
            if gw is None:
                await asyncio.sleep(2)
        node.log.append('sys', 'leaf-host: found gateway %s on ch %d'
                        % (':'.join('%02x' % b for b in gw), ch))
        # Pin the channel and let the radio settle before we start sending — the scan
        # left it hopping.
        try:
            node.wlan.config(channel=ch)
        except OSError:
            pass
        await asyncio.sleep_ms(300)
        link.add_peer(gw, ch)
    else:
        link.add_peer(gw)
    ul = _EspNowUplink(link, gw)
    myname = node.hostname
    cmd_prefix = 'cmd/leaf/%s/' % myname
    up_prefix = 'leaf/%s/' % myname
    node.log.append('sys', 'leaf-host: espnow uplink established')

    # ESP-NOW is connectionless — there is no socket to drop, so no reconnect loop and
    # no keepalive; a send just fails (ACK False) while the gateway is away and starts
    # working again when it returns.
    announce = json.dumps({
        'op': 'pub', 'retain': True, 'topic': '$sys/leaf/' + myname,
        'msg': {'name': myname, 'board': node.board_name(),
                'hosts_guests': True, 'transport': 'espnow'}})
    await ul.send(json.dumps({'op': 'sub', 'filters': [cmd_prefix + '#']}))
    await ul.send(announce)
    sub = sup.bus.subscribe(['#', '$sys/guest/#'], qlen=64, owner='uplink')
    for g in sup.guests.values():
        sup.sys_publish('$sys/guest/%s/state' % g.id, {'state': g.state}, retain=True)
    asyncio.create_task(_push(ul, sub, up_prefix))

    async def _reannounce():
        # Re-send identity + roster on an interval, so a gateway that reboots recovers
        # who this leaf is (its retained table was cleared) without the leaf rebooting.
        while True:
            await asyncio.sleep(20)
            await ul.send(announce)
            for g in sup.guests.values():
                sup.sys_publish('$sys/guest/%s/state' % g.id, {'state': g.state}, retain=True)
    asyncio.create_task(_reannounce())
    while True:
        try:
            text = await ul.recv()
            frame = json.loads(text)
        except (ValueError, OSError):
            continue
        topic = frame.get('topic', '')
        if topic.startswith(cmd_prefix):
            await _exec(node, sup, store, ul, topic[len(cmd_prefix):],
                        frame.get('msg') or {}, up_prefix)


def run_leaf_host(node):
    from jorm.supervisor import Supervisor
    sup = Supervisor(node)
    sup.blame_check()          # honor a watchdog reset: name and bench the culprit
    sup.scan()
    sup.install_import_guard()
    espnow = node.settings.get('transport') == 'espnow'

    async def _amain():
        if not espnow:
            from jorm.netwatch import wifi_watch
            asyncio.create_task(wifi_watch(node))   # re-associate a dropped link
        asyncio.create_task(sup.heartbeat())        # WDT + runaway detection
        asyncio.create_task(sup.telemetry())        # heap/temp on the local bus
        asyncio.create_task(_espnow_uplink(node, sup) if espnow else _uplink(node, sup))
        await sup.autostart()
        node.log.append('sys', 'leaf-host: %d guest(s) installed, hosting locally'
                        % len(sup.guests))
        while True:
            await asyncio.sleep(3600)

    asyncio.run(_amain())
