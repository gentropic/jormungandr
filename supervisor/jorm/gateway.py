"""ESP-NOW gateway (SPEC-three §1) — a flagship bridges its leaves' ESP-NOW to the bus.

The flagship runs WiFi and ESP-NOW at once (confirmed they coexist). A leaf's sealed
frames arrive here, are reassembled and unsealed by the transport, and their bus
messages are published onto the cluster bus — so a guest on any node hears a leaf that
never touched WiFi. It is the ESP-NOW twin of the /api/bus WebSocket handler: same
messages ({op:pub}, {op:sub}), a different wire.

v1 carries a leaf's traffic UP (pub) and remembers each leaf's MAC so commands can be
sent back DOWN; the down direction (forwarding a leaf's subscribed bus slice) is wired
but minimal, and discovery is seeded — a leaf knows this gateway's MAC and channel.
"""
import asyncio
import json

from jorm.espnow import EspNowLink


def _hex(mac):
    return ':'.join('%02x' % b for b in mac)


class Gateway:
    def __init__(self, node, sup):
        self.node = node
        self.sup = sup
        self.link = EspNowLink(node.token)
        self.leaves = {}          # mac -> {'name', 'sub': [filters], 'tap'}

    async def run(self):
        self.node.log.append('sys', 'espnow gateway up (mac %s)' % _hex(self.link.mac()))
        asyncio.create_task(self._hello_loop())
        while True:
            try:
                mac, text = await self.link.recv()
            except Exception as e:
                self.node.log.append('sys', 'gateway: recv error (%s)' % e)
                await asyncio.sleep_ms(200)
                continue
            self._on_frame(mac, text)

    async def _hello_loop(self):
        # Advertise for leaves to find, sealed so only token-holders can read it (§5).
        # Fast (1 Hz) so a scanning leaf, which sits on each channel only briefly,
        # reliably catches one while it is on ours. The HELLO states our real channel so
        # the leaf pins to it directly, rather than guessing from its own scan position.
        while True:
            ch = 0
            try:
                ch = self.node.wlan.config('channel')
            except (OSError, AttributeError):
                pass
            await self.link.send_hello(self.node.cluster, ch)
            await asyncio.sleep(1)

    def _on_frame(self, mac, text):
        try:
            frame = json.loads(text)
        except ValueError:
            return
        leaf = self.leaves.get(mac)
        if leaf is None:
            self.link.add_peer(mac)       # so we can answer this leaf
            leaf = self.leaves[mac] = {'name': _hex(mac), 'sub': [], 'tap': None}
            self.node.log.append('sys', 'espnow leaf %s joined' % _hex(mac))

        op = frame.get('op')
        if op == 'pub':
            topic = frame.get('topic')
            if not topic:
                return
            # A leaf's $sys/leaf/<name> announce carries its real name; adopt it so the
            # bus shows the hostname, not a MAC.
            if topic.startswith('$sys/leaf/'):
                leaf['name'] = topic.split('/', 2)[2]
            try:
                self.sup.bus.publish(topic, frame.get('msg'),
                                     retain=bool(frame.get('retain')),
                                     owner='espnow', origin=leaf['name'])
            except Exception:
                pass                      # a bad publish must not drop the gateway
        elif op == 'sub':
            leaf['sub'] = [f for f in frame.get('filters', []) if isinstance(f, str)]
            self._resubscribe(mac, leaf)

    def _resubscribe(self, mac, leaf):
        # Forward the bus slice this leaf asked for, back down to it. Minimal for v1: a
        # background task per leaf that seals and sends matching messages.
        if leaf['tap'] is not None:
            self.sup.bus.unsubscribe(leaf['tap'])
            leaf['tap'] = None
        if not leaf['sub']:
            return
        sub = self.sup.bus.subscribe(leaf['sub'], qlen=32, owner='espnow:' + leaf['name'])
        leaf['tap'] = sub

        async def pump():
            while True:
                topic, enc, origin = await sub.get()
                await self.link.send(mac, '{"topic": %s, "msg": %s}'
                                     % (json.dumps(topic), enc))
        asyncio.create_task(pump())


def run_gateway(node, sup):
    return asyncio.create_task(Gateway(node, sup).run())
