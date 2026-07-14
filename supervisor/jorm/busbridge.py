"""Datagram bus-bridge (spec_inbox/ROADMAP-flagship-services): a leaf's bus and the flagship's
bus join over fire-and-forget sealed datagrams — the datagram replacement for the held-open
WebSocket uplink, for a leaf that can't afford a live connection.

Both directions are just the door `pub` op: publishing to a node's door IS publishing to its bus.

  - Outbound (leaf -> flagship): the leaf forwards its declared `up` topics to the flagship's
    door. No flagship code needed.
  - Inbound (flagship -> leaf): the leaf registers its `down` topics with the flagship (the
    `bsub` op, kept alive), and the flagship's push-server forwards matching bus messages to the
    leaf's door.

Coalesced keep-latest per topic at a bounded rate, both ways — the backpressure that makes
datagram forwarding safe where the unbounded uplink was not.
"""
import asyncio
import json
import time

from jorm.bus import match
from jorm.leafclient import LeafClient

DOOR_PORT = 5355
BSUB_EVERY_S = 8          # leaf re-registers its down-subscriptions (a keepalive)
BRIDGE_TTL_MS = 20000     # flagship drops a leaf whose bsub it hasn't heard this long


async def run_bridge(node, sup):
    """Leaf side: forward declared `up` topics to the flagship (coalesced), and — if `down`
    topics are declared — keep a `bsub` registration alive so the flagship pushes them to our
    door. (Inbound needs this node's own door running, i.e. mgmt on.)"""
    br = node.settings.get('bridge') or {}
    host = br.get('flagship')
    if not host:
        node.log.append('sys', 'bus-bridge: no flagship configured — not bridging')
        return
    port = br.get('port', DOOR_PORT)                 # the FLAGSHIP's door port
    client = LeafClient(node.token)

    downs = [t for t in (br.get('down') or []) if not t.startswith('$')]
    if downs:
        my_door = node.settings.get('mgmt_port', DOOR_PORT)

        async def keepalive():
            while True:
                client.send(host, 'bsub', port=port, ip=node.ip, door=my_door, topics=downs)
                await asyncio.sleep(BSUB_EVERY_S)

        asyncio.create_task(keepalive())
        node.log.append('sys', 'bus-bridge: registering down-topics %s with %s' % (','.join(downs), host))

    ups = br.get('up') or []
    if not ups:
        while True:
            await asyncio.sleep(3600)                # down-only bridge: nothing to forward up
    rate_hz = br.get('rate_hz', 2)
    retain = br.get('retain', True)
    period_ms = max(1000 // max(rate_hz, 1), 100)
    sub = sup.bus.subscribe(ups, qlen=64, owner='bridge')
    latest = {}

    async def drain():
        while True:
            topic, enc, origin = await sub.get()
            if origin is not None:                   # never re-forward what we imported
                continue
            latest[topic] = enc

    asyncio.create_task(drain())
    node.log.append('sys', 'bus-bridge: forwarding %s to %s:%d at %d Hz'
                    % (','.join(ups), host, port, rate_hz))
    while True:
        await asyncio.sleep_ms(period_ms)
        if not latest:
            continue
        batch = latest
        latest = {}
        for topic, enc in batch.items():
            try:
                msg = json.loads(enc)
            except (ValueError, TypeError):
                continue
            client.send(host, 'pub', port=port, topic=topic, msg=msg, retain=retain)


async def run_bridge_server(node, sup):
    """Flagship side: push registered leaves the bus topics they asked for. Subscribe the bus
    ('#' excludes $sys), fan each message to any leaf whose down-topics match, coalesce
    keep-latest per (leaf, topic), and flush to each leaf's door `pub` at a bounded rate."""
    client = LeafClient(node.token)
    sub = sup.bus.subscribe(['#'], qlen=128, owner='bridge-down')
    latest = {}                                      # (ip, topic) -> encoded msg

    async def drain():
        while True:
            topic, enc, origin = await sub.get()
            if origin is not None:
                continue
            for ip, reg in sup.leaf_bridges.items():
                if any(match(f, topic) for f in reg['topics']):
                    latest[(ip, topic)] = enc

    asyncio.create_task(drain())
    node.log.append('sys', 'bus-bridge: flagship push-server up')
    while True:
        await asyncio.sleep_ms(200)                  # ~5 Hz flush
        now = time.ticks_ms()
        for ip in list(sup.leaf_bridges):            # reap leaves gone quiet
            if time.ticks_diff(now, sup.leaf_bridges[ip]['last_ms']) > BRIDGE_TTL_MS:
                del sup.leaf_bridges[ip]
        if not latest:
            continue
        batch = latest
        latest = {}
        for (ip, topic), enc in batch.items():
            reg = sup.leaf_bridges.get(ip)
            if reg is None:                          # leaf reaped since we queued it
                continue
            try:
                msg = json.loads(enc)
            except (ValueError, TypeError):
                continue
            client.send(ip, 'pub', port=reg['port'], topic=topic, msg=msg)
