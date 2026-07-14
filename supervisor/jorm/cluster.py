"""Cluster discovery (spec: jormungandr-one §1) — nodes find each other.

A cluster is leaderless and every node is the front door: you point the UI at any
one node and it draws the whole cluster, because the UI was never coupled to the
node serving it. So a node only has to answer two questions — "who else is here?"
and "are you still there?" — and it answers them with a UDP beacon.

Why a beacon and not mDNS: MicroPython gives one mDNS name per node and no service
API (proven on silicon — `EADDRINUSE` on 5353, no `mdns` module), and a browser can
neither browse mDNS nor listen to UDP. So discovery lives on the nodes, in ~30 lines
of broadcast, and the browser asks one node over HTTP (`GET /api/cluster`).

Membership is by cluster NAME: nodes that share a `cluster` string are one cluster,
and a beacon from a different cluster is ignored. Rename a node's cluster and it
leaves one and joins another — the tree is the unit of grouping the UI already has.

Two paths into the peer table, because one LAN is not the whole story:
  - the beacon: automatic, for boards on the same broadcast domain. Peers expire.
  - seed peers (`settings["peers"]`): explicit URLs that never expire, for a node
    on another subnet, or a sim on localhost the board can't broadcast to.
"""
import asyncio
import json
import socket
import time

PORT = 5354
LEAF_PORT = 5355          # a leaf's sealed-UDP door (jorm.leafapi.PORT), advertised in its beacon
BEACON_EVERY = 5          # seconds between our own announcements
PEER_TTL_MS = 20000       # a peer (or leaf) unheard for this long has left


def _sockaddr(host, port):
    # The unix port rejects a raw (host, port) tuple at bind()/sendto() — it wants
    # a resolved sockaddr. getaddrinfo returns one, and works identically on the
    # ESP32, so it is the portable idiom rather than a sim workaround.
    return socket.getaddrinfo(host, port)[0][-1]


class Discovery:
    def __init__(self, node):
        self.node = node
        self.peers = {}       # name -> {url, board, cluster, rssi, last_ms, seed}
        self.leaves = {}      # name -> {host, port, board, transport, cluster, last_ms}
        self.sock = None
        self._seed(node.settings.get('peers', []))

    def _seed(self, urls):
        for url in urls or []:
            url = url.rstrip('/')
            # A seed is known by its URL until a beacon gives it a real name; keying
            # by URL keeps it from duplicating the same node once its beacon arrives
            # under the node's hostname.
            self.peers['@' + url] = {
                'name': url, 'url': url, 'board': None, 'cluster': self.node.cluster,
                'rssi': None, 'last_ms': None, 'seed': True,
            }

    def _url(self):
        # Advertise an address the BROWSER can reach — the node's IP, not its mDNS
        # name (a peer on another OS may not resolve jorm-x.local, but the IP always
        # routes on the LAN). Port is omitted when it is 80, so the URL reads clean.
        ip = self.node.ip or '127.0.0.1'
        port = self.node.port
        return 'http://%s%s' % (ip, '' if port == 80 else ':%d' % port)

    def _beacon(self):
        return json.dumps({
            't': 'jorm',
            'name': self.node.hostname,
            'url': self._url(),
            'board': self.node.board_name(),
            'cluster': self.node.cluster,
            'rssi': self.node.rssi(),
        }).encode()

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(_sockaddr('0.0.0.0', PORT))
        s.settimeout(0)       # non-blocking: we drain and yield, never park the loop
        self.sock = s

    def _ingest(self, raw, from_ip):
        try:
            b = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(b, dict) or b.get('t') != 'jorm':
            return
        name = b.get('name')
        if not name or name == self.node.hostname:
            return            # not ours to record, or it is us
        if b.get('cluster') != self.node.cluster:
            return            # a different cluster is not our business
        if b.get('leaf'):
            # A leaf has no HTTP server; it advertises its sealed-UDP door, not a url. Trust
            # the ip it self-reports (like a full node self-reports its url) — portable across
            # the sim and the board; the packet's source ip is only a fallback, and on the
            # unix port that source is not even a dotted string. Kept apart from full-node
            # peers: the browser reaches a leaf through this node's /api/leaves, not by hopping.
            self.leaves[name] = {
                'name': name, 'host': b.get('ip') or from_ip, 'port': int(b.get('door', LEAF_PORT)),
                'board': b.get('board'), 'transport': b.get('transport', 'wifi'),
                'cluster': b.get('cluster'), 'last_ms': time.ticks_ms(),
            }
            return
        # A beacon carries the sender's self-reported url; trust it, but if it looks
        # unset fall back to the packet's source ip so the peer is still reachable.
        url = (b.get('url') or ('http://%s' % from_ip)).rstrip('/')
        # A seeded node announcing itself is not a second node. The seed was keyed by
        # url under a placeholder name; now that the beacon gives the real name, drop
        # the seed so the peer appears once — but remember it WAS seeded, so it keeps
        # its pinned status and does not expire if the beacon later stops.
        was_seed = self.peers.pop('@' + url, None) is not None
        self.peers[name] = {
            'name': name, 'url': url, 'board': b.get('board'),
            'cluster': b.get('cluster'), 'rssi': b.get('rssi'),
            'last_ms': time.ticks_ms(), 'seed': was_seed,
        }

    def _reap(self):
        now = time.ticks_ms()
        for name in list(self.peers):
            p = self.peers[name]
            if p['seed'] or p['last_ms'] is None:
                continue      # seeds and not-yet-heard entries do not time out
            if time.ticks_diff(now, p['last_ms']) > PEER_TTL_MS:
                del self.peers[name]
        for name in list(self.leaves):   # a leaf that stops beaconing has left, same TTL
            if time.ticks_diff(now, self.leaves[name]['last_ms']) > PEER_TTL_MS:
                del self.leaves[name]

    def live(self):
        """Peers to show right now — reaped of the departed, seeds always in."""
        self._reap()
        return sorted(self.peers.values(), key=lambda p: p['name'])

    def discovered_leaves(self):
        """Leaves heard on the beacon right now — reaped of the departed. The flagship
        fronts these over /api/leaves without any settings entry."""
        self._reap()
        return sorted(self.leaves.values(), key=lambda leaf: leaf['name'])

    async def announce(self):
        """Broadcast our beacon on an interval. One task, forever."""
        if self.sock is None:
            self._open()
        dst = _sockaddr('255.255.255.255', PORT)
        while True:
            try:
                self.sock.sendto(self._beacon(), dst)
            except OSError as e:
                self.node.log.append('sys', 'cluster: beacon send failed — %s' % e)
            await asyncio.sleep(BEACON_EVERY)

    async def listen(self):
        """Drain incoming beacons without ever blocking the event loop."""
        if self.sock is None:
            self._open()
        while True:
            drained = 0
            while drained < 32:    # bound the drain so a flood can't starve the loop
                try:
                    raw, addr = self.sock.recvfrom(512)
                except OSError:
                    break          # EAGAIN: nothing more waiting
                self._ingest(raw, addr[0])
                drained += 1
            await asyncio.sleep(0.5)


async def announce_leaf(node):
    """A WiFi leaf broadcasts its presence and its sealed-UDP door, so a flagship on the
    LAN discovers it with no seed — the beacon's twin for a node that has no HTTP url to
    advertise. (An ESP-NOW leaf has no IP to broadcast from; its gateway forwards it over
    the bus instead.) Fire-and-forget: nothing held open, so it cannot starve a guest.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0)
    dst = _sockaddr('255.255.255.255', PORT)
    while True:
        # Rebuilt each tick so a cluster rename (or a late hostname) is picked up.
        beacon = json.dumps({
            't': 'jorm', 'leaf': True, 'name': node.hostname, 'cluster': node.cluster,
            'board': node.board_name(), 'door': LEAF_PORT, 'transport': 'wifi',
            'ip': node.ip or '',      # self-reported: the address the flagship dials the door at
        }).encode()
        try:
            s.sendto(beacon, dst)
        except OSError as e:
            # No link yet, or the broadcast route is not up — try again next tick.
            node.log.append('sys', 'leaf-beacon: send failed — %s' % e)
        await asyncio.sleep(BEACON_EVERY)
