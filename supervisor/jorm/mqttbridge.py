"""Bus <-> MQTT bridge (spec_inbox/mqtt-internal-broker + ROADMAP-flagship-services): a full
node's door to the fleet's MQTT bus. The supervisor owns the broker connection; guests never see
it — a guest publishes on the local bus and it appears in Home Assistant, or a command from HA
appears on the local bus. This is the flagship-side external bridge every "service for leaves"
rides on (a leaf's topic reaches here over the datagram bus-bridge, then out to MQTT).

SECURITY (per the brief): the internal broker is anonymous-open, so INBOUND is untrusted. Inbound
only ever injects non-$ topics under a fixed `cmd/` namespace — never $sys/$ui, never a supervisor
call. A guest acts on it, gated by its caps/claims; the transport is not trusted, the cap model is.
Outbound is namespaced under dev/jorm/<node>/ so nodes don't collide on the flat fleet bus, and
$-rooted local topics are never published out.

umqtt.simple (not robust — robust's reconnect uses time.sleep, which would stall the loop);
reconnection is managed here with await.
"""
import asyncio
import json


async def run_mqtt(node, sup):
    cfg = node.settings.get('mqtt')
    if not cfg or not cfg.get('broker'):
        return
    from umqtt.simple import MQTTClient

    broker, port = cfg['broker'], cfg.get('port', 1883)
    node_ns = cfg.get('ns', 'dev/jorm/%s' % node.hostname)     # OUT prefix (auto-bridges to HA)
    cmd_ns = cfg.get('cmd_ns', 'cmd/jorm/%s' % node.hostname)  # IN prefix (untrusted commands)
    outs = [f for f in (cfg.get('out') or []) if not f.startswith('$')]
    period_ms = max(1000 // max(cfg.get('rate_hz', 4), 1), 100)

    inbound = []
    cli = MQTTClient(node.hostname, broker, port, keepalive=0)
    cli.set_callback(lambda t, p: inbound.append((bytes(t), bytes(p))))
    connected = [False]

    async def ensure():
        while not connected[0]:
            try:
                cli.connect()
                cli.subscribe(cmd_ns + '/#')
                connected[0] = True
                node.log.append('sys', 'mqtt: bridged to %s:%d as %s (<- %s/#)'
                                % (broker, port, node_ns, cmd_ns))
            except OSError as e:
                node.log.append('sys', 'mqtt: connect to %s:%d failed (%s) — retrying'
                                % (broker, port, e))
                await asyncio.sleep(3)

    latest = {}
    if outs:
        sub = sup.bus.subscribe(outs, qlen=64, owner='mqtt')

        async def drain():
            while True:
                topic, enc, origin = await sub.get()
                if origin is not None or topic.startswith('$'):
                    continue
                latest[topic] = enc

        asyncio.create_task(drain())

    await ensure()
    while True:
        await asyncio.sleep_ms(period_ms)
        # inbound: poll the broker, inject each command onto the local bus (scoped)
        try:
            cli.check_msg()
        except OSError:
            connected[0] = False
        while inbound:
            mt, pl = inbound.pop(0)
            mt = mt.decode()
            if not mt.startswith(cmd_ns + '/'):
                continue
            rest = mt[len(cmd_ns) + 1:]
            if not rest or rest.startswith('$'):     # never forge $sys/$ui off the untrusted bus
                continue
            try:
                msg = json.loads(pl) if pl else None
            except (ValueError, TypeError):
                msg = pl.decode('utf-8', 'replace')
            try:
                sup.bus.publish('cmd/' + rest, msg, owner='mqtt')
            except Exception:
                pass
        # outbound: publish the coalesced latest local values out under the node namespace
        if latest and connected[0]:
            batch = latest
            latest = {}
            for topic, enc in batch.items():
                try:
                    cli.publish(node_ns + '/' + topic,
                                enc.encode() if isinstance(enc, str) else enc)
                except OSError:
                    connected[0] = False
                    break
        await ensure()
