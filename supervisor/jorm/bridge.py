"""Bus bridging (one §4) — the cluster's separate buses become one nervous system.

Chapter 1 let nodes SEE each other; this lets them ACT on each other. Each node pulls
a declared slice of every peer's bus into its own, so a guest here can react to a
sensor there, with neither guest knowing the other exists — they just pub/sub topics,
and the bridge carries them across.

What crosses is opt-in and per-node: settings["bridge"] is the list of topic filters
THIS node wants imported from its peers. Nothing is bridged by default, because
bridging everything is both wasteful and rarely what you mean.

Two disciplines keep it honest:
  - Split horizon: a node exports only its OWN traffic to a peer's bridge, never what
    it imported from a third node (enforced in bus.py). So imports do not relay
    onward — the topology is direct, not a rumor mill. Multi-hop is a later chapter.
  - $-roots stay home: $sys/heap, $sys/temp and kin are a node's private telemetry.
    Bridging them would collide every node's readings onto one topic; the UI already
    shows each node's own $sys by reading /api/cluster. So the bridge drops them even
    if a filter would match.
"""
import asyncio
import json

from jorm import wsclient
from jorm.bus import BusError

RETRY_S = 5


def _host_port(url):
    # http://10.0.10.74  ->  ('10.0.10.74', 80);  http://127.0.0.1:8001 -> (..., 8001)
    rest = url.split('://', 1)[-1].rstrip('/')
    if ':' in rest:
        host, port = rest.rsplit(':', 1)
        return host, int(port)
    return rest, 80


class BridgeManager:
    def __init__(self, sup):
        self.sup = sup
        self.node = sup.node
        self.filters = [f for f in sup.node.settings.get('bridge', []) if f]
        self.tasks = {}       # peer url -> the task pulling from it

    async def run(self):
        if not self.filters:
            return            # bridging is opt-in; this node imports nothing
        self.node.log.append('sys', 'bridge: importing %s from peers' % self.filters)
        while True:
            live = {p['url']: p for p in self.sup.cluster.live()}
            for url, peer in live.items():
                t = self.tasks.get(url)
                if t is None or t.done():
                    self.tasks[url] = asyncio.create_task(self._pull(peer))
            for url in list(self.tasks):
                if url not in live and self.tasks[url].done():
                    del self.tasks[url]   # peer gone and its puller has exited
            await asyncio.sleep(RETRY_S)

    async def _pull(self, peer):
        url, name = peer['url'], peer['name']
        host, port = _host_port(url)
        ws = None
        try:
            ws = await wsclient.connect(host, port, '/api/bus', self.node.token)
            await ws.send(json.dumps(
                {'op': 'sub', 'filters': self.filters, 'bridge': True}))
            while True:
                frame = json.loads(await ws.recv())
                topic = frame.get('topic')
                if not topic or topic.startswith('$'):
                    continue          # $-roots stay on their home node
                try:
                    # origin=name is what makes split horizon work AND tells local
                    # consumers this crossed from `name`. owner distinguishes bridge
                    # traffic in the pub-counts table.
                    self.sup.bus.publish(topic, frame.get('msg'),
                                         owner='bridge:' + name, origin=name)
                except BusError:
                    pass              # one malformed import must not drop the bridge
        except (OSError, EOFError, ValueError):
            pass                      # peer down/refused; run() retries on its pass
        finally:
            if ws is not None:
                await ws.close()
