"""Datagram bus-bridge (spec_inbox/ROADMAP-flagship-services): a leaf's bus reaches the flagship
over fire-and-forget sealed datagrams — the datagram replacement for the held-open WebSocket
uplink, for a leaf that can't afford a live connection (a battery sensor, the render-tight clock).

Coalesced, keep-latest per topic at a bounded rate, so a chatty topic can't flood the link — the
backpressure that makes datagram forwarding safe where the unbounded uplink was not. Outbound is
just the flagship's existing door `pub` op: a leaf publishing to the flagship's door IS the leaf
publishing to the flagship's bus.

Slice 1: outbound only (leaf -> flagship bus). Inbound (a leaf's declared subscriptions pushed
down from the flagship) and the flagship-side bus<->MQTT bridge are later slices.
"""
import asyncio
import json

from jorm.leafclient import LeafClient

DOOR_PORT = 5355


async def run_uplink(node, sup):
    br = node.settings.get('bridge') or {}
    host, ups = br.get('flagship'), br.get('up') or []
    if not host or not ups:
        node.log.append('sys', 'bus-bridge: no flagship / up-topics configured — not forwarding')
        return
    rate_hz = br.get('rate_hz', 2)
    retain = br.get('retain', True)
    port = br.get('port', DOOR_PORT)
    period_ms = max(1000 // max(rate_hz, 1), 100)
    client = LeafClient(node.token)
    sub = sup.bus.subscribe(ups, qlen=64, owner='bridge')
    latest = {}                                   # topic -> encoded msg (keep-latest coalescing)

    async def drain():
        while True:
            topic, enc, origin = await sub.get()
            if origin is not None:                # never re-forward what we imported from a peer
                continue
            latest[topic] = enc

    asyncio.create_task(drain())
    node.log.append('sys', 'bus-bridge: forwarding %s to %s:%d at %d Hz'
                    % (','.join(ups), host, port, rate_hz))
    while True:
        await asyncio.sleep_ms(period_ms)
        if not latest:
            continue
        batch = latest                            # swap is atomic vs drain (no await between)
        latest = {}
        for topic, enc in batch.items():
            try:
                msg = json.loads(enc)
            except (ValueError, TypeError):
                continue
            client.send(host, 'pub', port=port, topic=topic, msg=msg, retain=retain)
