# jormungandr two — leaves

A **leaf** is a node too small to be a node in the full sense: an ESP32-C3, a
WROOM, an ESP8266 — cheap silicon whose job is to *sense and actuate*, not to host
guests or serve a UI. One proved on silicon (SPEC-one §2b) that the flagship
supervisor is over a C3's weight class: it opens too many sockets for the C3's
fixed lwIP buffer pool, so the beacon can't broadcast and the HTTP server can't
accept connections. This spec is the node that fits.

## 1. The one rule

**A leaf never runs an IP server.** No listening TCP socket, no accept loop, no
per-connection buffers, no UI. That is the thing that exhausted lwIP; removing it is
what makes a leaf fit. A leaf is a **client**: it opens exactly one connection
*outward*, to a flagship, and does everything over it.

The rule is about IP servers specifically, not about forwarding in general — a leaf
may one day relay another leaf's ESP-NOW frames (SPEC-one §4 territory), which is
cheap precisely because ESP-NOW never touches lwIP. What a leaf must not do is hold
IP-stack server state.

## 2. What a leaf is

Boot: settings → wifi → **connect out to a flagship's bus** → sense and actuate over
it. No `microdot`, no `Supervisor`, no USB, no `Bus` object — the heavy imports that
starve lwIP are never loaded. The leaf path is a separate, light module
(`jorm/leaf.py`); a node enters it when `settings["role"] == "leaf"`.

The uplink is the flagship's existing `/api/bus` WebSocket, reached with the same
async WS client bus bridging uses (`jorm/wsclient.py`). Over it the leaf:

- **Announces itself** — publishes a retained `$sys/leaf/<name>` carrying its name,
  board, and I/O map, so the flagship can represent it in the tree. A leaf has no
  `/api/node` for the UI to read; the flagship reads this instead.
- **Publishes its sensors** — each reading is a normal bus message on the flagship's
  bus, so a guest on the flagship (or any node bridged to it) can subscribe with no
  idea a leaf produced it.
- **Subscribes its actuator commands** — exact topics, no wildcards; a message on one
  drives a pin.

## 3. The I/O map — a leaf's whole program

A leaf runs no code, only a declarative map in `settings["io"]`. This is the line
that keeps a leaf a leaf and not a guest host: a fixed schema of
read-pin→publish / subscribe→write-pin, never arbitrary logic.

```json
"io": {
  "sensors": [
    {"type": "vitals", "topic": "leaf-c3/vitals", "every_s": 5},
    {"type": "adc",    "pin": 4, "topic": "leaf-c3/soil", "every_s": 30},
    {"type": "temp",   "topic": "leaf-c3/temp", "every_s": 30}
  ],
  "actuators": [
    {"type": "digital", "pin": 5, "topic": "cmd/leaf-c3/relay"}
  ]
}
```

Sensor types in v1: `vitals` (heap + uptime, zero wiring — health and a first demo),
`adc` (analog read, `read_u16`), `temp` (the MCU's own sensor). Actuator types:
`digital` (a pin driven high/low by the command's truthiness). More types are more
schema, not more code paths that matter.

## 4. Config and storage

A leaf stores only what keeps it itself across a reboot — its identity and its I/O
map — in `settings.json`, exactly as a full node stores its settings. Readings live
on the flagship's bus (its retained table remembers the last of each); the leaf does
not remember what it sensed, only what it *is*. Editing a leaf's map from the UI, and
NVS-persisting it, is a later slice; v1's map is provisioned at flash time.

## 5. Finding a flagship

v1: seeded. `settings["flagship"]` is a URL the leaf connects to. Auto-discovery —
the leaf *listening* (receive-only) for flagship beacons and picking the nearest — is
a clean later addition and needs no IP server, so it does not break rule §1. The leaf
never broadcasts to be found; flagships do not connect into leaves.

## 6. Transports (the shape, for orientation)

- **WiFi bus-client** — v1, this spec. One outbound WebSocket to a flagship. Reliable,
  reuses everything. The subject of the first slice.
- **ESP-NOW** — for a leaf off WiFi or on a battery. Bypasses lwIP entirely (802.11
  action frames, no sockets), so it structurally dodges the memory wall that made
  this spec necessary. A flagship bridges ESP-NOW ↔ bus. ESP-NOW and WiFi share one
  radio and one channel, so an ESP-NOW gateway's channel is its WiFi channel. Later.
- **BLE** — a central/flagship capability, or a dedicated BLE-bridge leaf, not stacked
  on a WiFi+ESP-NOW leaf: BLE time-shares the 2.4 GHz radio with WiFi and costs
  memory, which a small node juggling two transports does not have. Later.

Transports become something a node **advertises**, next to `runtimes`: `wifi`,
`espnow`, `ble`. A guest declaring `ble: central` lands only on a node offering BLE;
an ESP-NOW-only leaf is represented through its gateway. The transport question and
the capability/placement model are the same question.

## 7. The smart leaf — a leaf that hosts guests (measured, real)

The dumb leaf (§2–§5) runs a fixed I/O map and no code. But the question came up:
does memory *force* that, or could a leaf run actual guests? Measured on the C3, the
answer is that memory does not force it.

A **smart leaf** (`role: "leaf-host"`, `jorm/leafhost.py`) keeps the guest machinery —
`hal`, `guests`, the bus, claims — and drops only the HTTP server. Guests run locally;
their bus traffic is pushed up to a flagship over one outbound connection (split-horizon,
`$`-private, like the bridge). What the measurement showed on the actual C3:

- **It fits.** With the full machinery loaded and a guest scanned, the C3 has ~96 KB
  of heap free — room for a dozen guests. The server was ~10 KB of heap; dropping it
  mattered for the *sockets* (lwIP), not the bytes.
- **A real guest runs.** `pinger` hosted on the C3, its ticks pushed to the S3 over
  WiFi. Arbitrary guest code, on a two-dollar board.
- **The safety net holds.** `hog` (`while True: pass`) wedged the single core — there
  is no preemption — and the hardware WDT reset the node; on reboot it named hog from
  RTC memory, disabled its autostart, and came back hosting the good guest. The M5
  silicon drill, on a C3.

So the tiers are a real spectrum, and the middle one is proven:

| tier | hosts guests | isolation | fits C3 |
|---|---|---|---|
| dumb leaf (§2) | no — I/O map | can't run away | yes |
| **smart leaf (§7)** | **yes, serverless** | **soft: no preempt, no wall** | **yes, ~96 KB free** |
| sol leaf | yes | hard: arena, preemptible | yes (later) |

What a smart leaf costs is not memory but *trust*: mpy's soft isolation, sharper on a
single core with less headroom — a runaway wedges the node until the WDT catches it,
and one greedy guest can OOM it. The dumb leaf's fixed map is immune to both, which is
what it buys. Guest *management* over the bus is **built**: a central node lists,
installs, starts, stops and removes a leaf's guests by publishing `cmd/leaf/<name>/<verb>`
and reading `leaf/<name>/result`; the leaf forwards its guests' state up as
`leaf/<name>/guest/<id>` so the flagship sees the roster with no `/api/guests` to call
(`jorm leaf <name> [guests|start|stop|restart|rm|install]`).

## Status

**Slice 1 (dumb WiFi bus-client leaf): done.** Stripped boot, one outbound bus
connection, the I/O map — proven on a real C3 streaming to a real S3, no ENOMEM.

**Smart leaf (§7): done, on silicon.** Fits (~96 KB free), hosts a real guest,
survives a runaway via the WDT drill, and is **managed from a central node over the
bus** — verified in the sim and on a real C3 driven from an S3. `jorm/leafhost.py`,
`role: "leaf-host"`. ESP-NOW, BLE, auto-discovery, UI-side leaf editing, and
reconnection hardening (a flagship that re-associates WiFi after a drop, an uplink
that detects a rebooted flagship) are named for later.
