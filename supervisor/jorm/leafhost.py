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
import time

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


async def _commands(node, sup, store, ul, cmd_prefix, up_prefix):
    """Receive and run management commands until cancelled."""
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


async def _find_gateway(node, link):
    """Return (mac, channel): the seeded gateway, or one found by scanning (§5)."""
    gw = _mac_bytes(node.settings.get('gateway_mac', ''))
    if gw is not None:
        return gw, node.settings.get('gateway_channel')
    cluster = node.settings.get('cluster') or 'Cluster'
    while True:
        node.log.append('sys', 'leaf-host: scanning for an espnow gateway (%s)' % cluster)
        gw, ch = await link.scan_for_gateway(cluster, node.wlan)
        if gw is not None:
            return gw, ch
        await asyncio.sleep(2)


async def _espnow_uplink(node, sup):
    from jorm import guests as store
    from jorm.espnow import EspNowLink
    link = EspNowLink(node.token)
    myname = node.hostname
    cmd_prefix = 'cmd/leaf/%s/' % myname
    up_prefix = 'leaf/%s/' % myname
    announce = json.dumps({
        'op': 'pub', 'retain': True, 'topic': '$sys/leaf/' + myname,
        'msg': {'name': myname, 'board': node.board_name(),
                'hosts_guests': True, 'transport': 'espnow'}})

    def _roster():
        for g in sup.guests.values():
            sup.sys_publish('$sys/guest/%s/state' % g.id, {'state': g.state}, retain=True)

    # Discover → serve → re-discover. ESP-NOW has no socket to drop, but a gateway can
    # reboot and come back on a different channel, or its WiFi channel can move, and a
    # leaf that pinned one channel at boot would then be stranded silently. So we mirror
    # the WiFi leaf's keepalive: an active heartbeat probe (the announce, which also
    # refreshes our identity on the gateway), and when it stops being ACKed for long
    # enough, tear down and scan afresh. The probe doubling as the announce is why a
    # rebooted gateway relearns this leaf without anyone rebooting.
    while True:
        gw, ch = await _find_gateway(node, link)
        node.log.append('sys', 'leaf-host: gateway %s%s'
                        % (':'.join('%02x' % b for b in gw),
                           '' if ch is None else ' on ch %d' % ch))
        if ch is not None:
            try:
                node.wlan.config(channel=ch)      # pin it; the scan left the radio hopping
            except OSError:
                pass
            await asyncio.sleep_ms(300)
        link.add_peer(gw, ch or 0)
        # Establish fresh nonces both ways before any data (§6). If it fails — a lost
        # JOIN/WELCOME on a bad link, or the gateway already gone — scan afresh.
        if not await link.handshake(gw):
            node.log.append('sys', 'leaf-host: handshake with gateway failed — re-discovering')
            continue
        ul = _EspNowUplink(link, gw)
        await ul.send(json.dumps({'op': 'sub', 'filters': [cmd_prefix + '#']}))
        await ul.send(announce)
        sub = sup.bus.subscribe(['#', '$sys/guest/#'], qlen=64, owner='uplink')
        _roster()
        pusher = asyncio.create_task(_push(ul, sub, up_prefix))
        cmdr = asyncio.create_task(_commands(node, sup, store, ul, cmd_prefix, up_prefix))
        node.log.append('sys', 'leaf-host: espnow uplink established')
        # Liveness is app-level, not MAC-ACK: with receiver-issued nonces a rebooted
        # gateway ACKs a stale-nonce frame at the radio and then drops it, so an ACK no
        # longer proves acceptance. Probe with PING and watch last_rx (a validated PONG
        # or any down-data refreshes it); ~16 s without one means re-handshake.
        tick = 0
        try:
            while True:
                await asyncio.sleep(5)
                await link.send_ping(gw)
                tick += 1
                if tick % 4 == 0:                  # ~every 20 s, refresh identity + roster
                    await ul.send(announce)
                    _roster()
                if time.ticks_diff(time.ticks_ms(), link.last_rx) > 16000:
                    node.log.append('sys', 'leaf-host: gateway quiet ~16s (no pong) — re-discovering')
                    break
        finally:
            pusher.cancel()
            cmdr.cancel()                          # must stop reading before a re-scan,
            await asyncio.sleep_ms(50)             # so two arecv()s don't race the radio
            sup.bus.unsubscribe(sub)


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
