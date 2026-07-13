# jormungandr one — the cluster

Zero is one node. One is many nodes that no one node is in charge of. This spec
covers the first chapter of that — discovery and a single tree — and names the
chapters that follow so the shape is visible before they are built.

Everything here rests on a property zero already had and this spec finally uses:
**the UI was never coupled to the node serving it.** It takes a URL and a token and
speaks an API. Pointing it at a cluster is a data change, not an architecture change.

## 1. Discovery — nodes find each other

### The beacon

Each node broadcasts a small JSON beacon on UDP `255.255.255.255:5354` every 5 s,
and listens on the same port, keeping a peer table with a 20 s expiry. A peer
unheard for 20 s has left.

```json
{"t":"jorm","name":"jorm-c510","url":"http://10.0.10.74","board":"...","cluster":"Gentropic","rssi":-45}
```

- **Why a beacon and not mDNS.** MicroPython gives one mDNS name per node and no
  service-registration API — proven on silicon: binding UDP 5353 is `EADDRINUSE`
  before connect, after connect, with `SO_REUSEADDR`, and there is no `mdns`
  module. And a browser can neither browse mDNS nor listen to UDP. So discovery
  lives on the nodes, in ~30 lines, and the browser asks one node over HTTP.
- **The url is an IP, not a name.** A peer on another OS may not resolve
  `jorm-x.local`; the IP always routes on the LAN. Port is omitted when 80.
- **Portable sockets.** `bind()`/`sendto()` take a `getaddrinfo(...)[0][-1]`
  sockaddr, not a raw `(host, port)` tuple — the ESP32 accepts both, the unix port
  only the former. `setsockopt(SO_BROADCAST, 1)` with an int. Multicast would be
  cleaner for same-host listeners, but `IP_ADD_MEMBERSHIP` is not compiled into the
  unix-port build (`errno 92`), so broadcast it is.

### Membership is by cluster name

Nodes that share a `cluster` string are one cluster; a beacon from a different
cluster is ignored. Rename a node's cluster and it leaves one and joins another —
the tree is the unit of grouping the UI already has, and the cluster name is its
root. A cluster of one is how a cluster of eight begins.

### Seed peers — for what a broadcast can't reach

`settings["peers"]` is a list of URLs that enter the peer table directly and never
expire. This covers a node on another subnet, or a sim on localhost the board can't
broadcast to. When a seeded node's beacon later arrives under its real name, the
seed is superseded, not duplicated — it appears once, still pinned. Beacon
discovery is for boards on one broadcast domain; seeds are for everything else.

### `GET /api/cluster`

Returns this node and the peers it currently hears:

```json
{"cluster":"Gentropic",
 "self":{"name":"jorm-c510","board":"...","rssi":-45},
 "peers":[{"name":"jorm-9f31","url":"http://10.0.10.61","board":"...","rssi":-52,"seed":false}]}
```

A cluster of one returns an empty `peers` list — correct, not an error. The tree
still has its one node.

## 2. One tree, every node a front door

There is **no elected front door and no cluster VIP** — the same decision Proxmox
made, and for the same reason: you browse to any node and manage the whole cluster
from it. (The alternative, one node also serving `jormungandr.local`, is impossible
on stock MicroPython anyway — one mDNS name per node, welded to the port — and an
election buys flapping, split-brain, and a single point of failure to earn a name.)

- **The UI draws the whole cluster.** It connects to whichever node you opened,
  reads `/api/cluster`, and renders each node as a branch of one tree with its
  guests beneath it. Peers are read **cross-origin** with the shared token.
- **CORS, gated by the token.** Every node answers `/api/` with
  `Access-Control-Allow-Origin: *`. This widens who may *knock*, never who may
  *enter* — the bearer token still gates every call, so a call without it is a 401
  regardless of origin. A preflight (`OPTIONS`) is answered with the CORS headers
  via microdot's `options_handler`, because its auto-OPTIONS path runs before the
  request hooks.
- **Managing a peer means becoming its front door.** Clicking a peer *hops* the UI
  to it: the current node mints a one-time **ticket** on the peer (cross-origin,
  shared token) and navigates carrying it in the fragment — never the token itself,
  same contract as `jorm open`. The peer redeems it and you are now its UI, with the
  full single-node control surface. You always fully manage the *active* node; the
  tree shows all of them; one click moves the door.

### The token is shared

One token for the whole cluster. One paste provisions a board into it, and the
cross-origin reads and the hop-ticket mint all use it. Per-node tokens were the
alternative (a compromised board is not a skeleton key); shared won on the ergonomics
of a LAN of six-dollar boards, and the token never travels in a URL regardless.

## 2b. Heterogeneous nodes — a hardware finding

A cluster is meant to mix a flagship S3 with cheaper silicon. We flashed MicroPython
onto an ESP32-C3 supermini (single core, ~170 KB heap) to make it a real node, and
learned exactly where the line is:

- The C3 **boots the full supervisor** — WiFi up, API listening, NTP synced. Getting
  there needed two fixes that help every node: the USB device stack (~1500 lines)
  loads lazily, only on a node with a usb guest, and the heavy supervisor imports
  happen **after** `wifi_up`, so the WiFi driver claims its RX buffers while internal
  RAM is most free (otherwise: "WiFi Out of Memory"). A down-cycle of the radio on
  boot clears the stale state a reset leaves, which the flagship tolerated and the
  small node did not.
- But the full supervisor **starves the C3's network stack**. lwIP cannot spare a
  buffer for the UDP beacon broadcast (`ENOMEM`), and the same exhaustion stops the
  HTTP server accepting connections — the event loop runs, the node is network-dead.
  And the hardware WDT resets it during the ~1 s single-core GC pauses, because the
  heartbeat can't be fed in time.

The conclusion is the one §2b of the design predicted, now measured: **the flagship
supervisor is over a C3's weight class.** A C3 joins not by running the whole thing
but as a **limb** — a stripped build (beacon + bus + a minimal API and a declarative
I/O map, no UI serving, no USB, no full guest hosting) that leaves lwIP its buffers —
or, later, the sol/Lua firmware. The full-node path is for flagships. This is a
boundary to design around, not a bug to cram past.

## 3. Placement — the score, when it is time

A node reports `rssi` (dBm, or `null` for a node with no radio — never `0`, which
would rank a wired sim as the best-connected board). This is a **placement** input,
not a routing one: the question "least load, best link" belongs to *where a migrated
guest should land*, not to DNS. `jorm open` already ranks nodes by round-trip time
and breaks ties on RSSI; migration will reuse the same instinct.

## 4. The chapters after this one

Named here so the first slice is built toward them, not around them:

- **Bus bridging — DONE (chapter 2).** A node opens a WebSocket to a peer's
  `/api/bus` (the node grew an async WS *client*, `jorm/wsclient.py`), subscribes to
  the slice declared in `settings["bridge"]`, and republishes each message on its own
  bus stamped with the peer as `origin`. A guest here reacts to a guest there through
  the bus alone, neither knowing the other is on a different board — verified: pinger
  on one node, echoer on another, coordinating only across the bridge. Two disciplines
  keep it honest: **split horizon** (a node exports only its own traffic to a peer's
  bridge, never its imports — so `B→A→C→A` can't loop; enforced by a `bridge` flag on
  the subscription and an `origin` on every message) and **`$`-roots stay home** (a
  node's `$sys` telemetry is private; bridging it would collide every board's heap
  onto one topic). Retained-value sync across the bridge and multi-hop relay are left
  for later; today's bridge is direct and live. `tools/accept-bridge.sh`: 5 checks.
- **Migration.** `GET /api/guests/{id}/bundle` → `POST` to a peer → **precheck its
  claims table** → flip autostart → stop here. It must *refuse honestly*: "that node
  has no free pin 48" is the answer, not a crash after the move. Live-migrating a
  blinking LED between six-dollar boards, claims-checked, is the most neo-dada
  sentence this project has produced.
- **ESP-NOW as a bus transport.** For a leaf node not on WiFi at all: MAC-addressed,
  a 250-byte payload to fragment against the 4 KB message cap. A transport, not a
  feature.
- **An MQTT bridge.** Nearly free, because the bus already speaks the shape — how the
  cluster talks to a Home Assistant someone already runs.

## Status

**Chapter 1: done.** Beacon + peer table + seeds + `/api/cluster` + CORS + one tree +
hop, verified with two sim nodes discovering each other, showing each other's guests
in one tree, and hopping between them with a ticket handoff. A board runs it as a
cluster of one, unchanged.

**Chapter 2 (bus bridging): done.** A node pulls a declared slice of every peer's bus
into its own over an async WS client, split-horizon and `$`-private, so guests on
different boards coordinate through one bus. The UI's monitor shows each bridged
message's origin board (`↯jorm-x`). Chapters 3–4 (migration, ESP-NOW) are not started.
