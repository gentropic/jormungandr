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

## Status

**Slice 1 (WiFi bus-client leaf): the target of this work.** Stripped boot, one
outbound bus connection, the I/O map, measured on the actual C3 — does dropping the
IP server leave lwIP the room the beacon and the uplink need? That measurement is the
point. ESP-NOW, BLE, auto-discovery, and UI-side leaf editing are named for later.
