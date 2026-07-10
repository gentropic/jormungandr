# SPEC-jormungandr-zero

**jormungandr** — a MicroPython guest supervisor with a Proxmox-flavored web UI and API.
The world-serpent, hosting the smallest pythons — deliberately the largest name in the GCU on
its smallest machines. Always spelled `jormungandr` (ASCII, no ö, no `-gand`); the CLI and the
daemon are **`jorm`** — nobody sees the whole serpent at once. `sol` (§9) reads natively in
both mythologies: SOL-8's sun, and Sól's.

Targets: ESP32-S3 N16R8 (flagship), RP2350 / Pico 2W (mpy portable; core1 exile is a platform extra), ESP32-C3-class leaf nodes (sol profile only).
Runtime profiles: **mpy** (MicroPython + asyncio + microdot) and **sol** (ESP-IDF + Lua) — §9. UI: one HTML file, servable from any flagship node. シングルファイルデプロイ.

---

## 1. Model

- The **supervisor** owns the machine: boot, WiFi, flash, web server, broker, watchdog.
- A **guest** is a bundle in flash: `/guests/<id>/manifest.json` + `main.py` (+ optional modules + `/data/` private dir).
- Guests are cooperatively-or-preemptively scheduled tasks in isolated execution contexts — asyncio tasks in namespaces (mpy) or one `lua_State` each (sol, §9). Isolation is soft in mpy, hard-walled in sol; either way the threat model is bugs, not adversaries.
- A guest never imports `machine`, `network`, `bluetooth`, `time`, or `os`. Everything arrives through `hal`.

### Guest contract

```python
# main.py
async def run(hal):
    led = hal.pin(2)
    while True:
        led.toggle()
        await hal.sleep_ms(500)
```

Rules: `run(hal)` is the sole entry point; await at least every ~50 ms; return = clean exit; uncaught exception = "crashed" state with traceback captured to the guest console; honor cancellation (`asyncio.CancelledError` propagates — cleanup goes in `finally`).

### Imports and child tasks

- Bundles may ship extra modules. Import resolution is three-tier: the bundle dir first, then the node's shared library store (`/lib`, managed via `PUT /api/lib/{name}` — install a driver on a *live* node), then the built-in whitelist of pure stdlib (`json`, `math`, `struct`, `collections`, `binascii`, `re`). `machine`, `network`, `bluetooth`, `socket`, `os`, `time`, and the event loop itself are not importable — the loop is reached only through `hal`.
- **Shared libraries are shared code *and shared state*** (mpy): Python executes a module once and caches it, so every importer gets the same module object — a mutable module-level global in a driver is a cross-guest leak. The convention, enforced by review rather than machinery: `/lib` drivers are **stateless at module level** (classes and functions only; instances live in the importing guest). A driver that crashes *in a guest's call* still detonates only that guest; a driver that keeps module-level state betrays everyone equally. The sol profile (§9) upgrades this from convention to guarantee — each `lua_State` executes shared libs against flash ROTables, per-guest by construction.
- Concurrency inside a guest goes through `hal.spawn(coro)`, which registers the child with the supervisor. Stop or crash cancels the whole family — no orphan tasks outlive their guest. `hal.gather(...)`, `hal.Event()`, and `hal.Queue(n)` round out the primitives.

### Lifecycle

`stopped → starting → running → stopping → stopped`, plus `crashed` and `unresponsive`.

- **starting**: manifest parsed → capabilities checked against the claims table → all-or-nothing grant → namespace built → task created.
- **stopping**: `task.cancel()` → grace period (default 2000 ms) → if still alive, mark `unresponsive` (cannot preempt; UI shows it hung, like a VM that won't ACPI-shutdown).
- **crashed**: traceback stored; optional `restart` policy in manifest (`never` | `on-crash` | `always`, with exponential backoff, max 5 tries).
- Watchdog: a supervisor heartbeat task timestamps each loop pass, and every `hal` await stamps its guest's `last_yield`. On starvation > 250 ms, the running guest with the stalest `last_yield` is flagged `unresponsive` — attribution by evidence, not by guessing at the scheduler.
- **The worst case, said out loud** (mpy): a guest that *never* yields starves the whole loop — the heartbeat, the flagging logic, **and the web server**. Exactly when you'd want the UI to show "unresponsive," it's unreachable. So the supervisor arms the **hardware WDT**, and keeps a **blame file**: the stalest `last_yield` guest id is persisted to flash on each heartbeat degradation. After a WDT reset, the suspect's `autostart` is **disabled** and the guest badged: *"suspected in watchdog reset — autostart disabled."* Crash-loop protection with a named culprit; the node always comes back reachable.

---

## 2. Manifest

```json
{
  "spec": 0,
  "id": "blinky",
  "name": "Blinky",
  "version": "0.1.0",
  "runtime": "mpy",
  "entry": "main.py",
  "autostart": false,
  "restart": "never",
  "caps": {
    "pins":    [ {"pin": 2, "mode": "out"}, {"pin": 15, "mode": "in", "pull": "up"} ],
    "pwm":     [ 16 ],
    "adc":     [ 4 ],
    "i2c":     [ {"bus": 0, "addrs": [60, 87]} ],
    "spi":     [ {"bus": 1, "cs": 10} ],
    "net":     { "client": true },
    "ble":     "peripheral",
    "bus":     { "pub": ["blinky/#"], "sub": ["clock/tick", "cmd/blinky/#"] },
    "ui":      true,
    "storage": { "quota_kb": 128 },
    "mem_kb":  64
  }
}
```

- `spec`: manifest format version; the supervisor refuses manifests from the future with a clear error.
- `id`: `[a-z0-9-]{1,24}`, unique per node, doubles as the storage dir and default topic root.
- `runtime`: `"mpy"` or `"sol"` (§9); a guest runs only on nodes offering its runtime. `"wasm"` stays reserved. Authoring language ≠ runtime: a bundle's Lua may be hand-written or transpiled (AIR → Lua, §9) — the node neither knows nor cares.
- `mem_kb`: advisory in zero (we can't enforce per-task heaps). Supervisor samples `gc.mem_free()` deltas per guest for the UI graph and refuses to *start* a guest if free heap < declared need + reserve. Honest label in UI: "declared", not "limit".
- Unknown cap keys → refuse to start (fail closed, clear error).

## 3. Capability grammar and conflict rules

Grant time is start time. All-or-nothing. On stop/crash, all claims release.

| Capability | Sharing | Conflict rule |
|---|---|---|
| `pins` | exclusive | one owner per pin, any mode. Supervisor-reserved pins (status LED, boot strap pins) are never grantable. |
| `pwm` | exclusive per pin | implies the pin claim; also bounded by hardware PWM channel count. |
| `adc` | exclusive per channel | implies the pin claim. |
| `i2c` | bus shared, address exclusive | supervisor owns the bus object; guests get address-scoped handles. Two guests on bus 0 is fine; two guests claiming addr 60 is not. Each `hal.i2c` call is **one atomic bus transaction**; separate calls may interleave with another guest's — that's what `mem_read`/`mem_write` exist for. |
| `spi` | bus shared, CS pin exclusive | CS claim implies the pin claim. |
| `net` | shared | supervisor owns WiFi + IP stack (it needs them for the API). **Client-only in zero** — the supervisor owns the listener; guest servers are a later cap. Guests get async sockets/HTTP; HTTPS-as-client works on flagships (a TLS context costs ~30–40 KB) and is not offered on leaves. Per-guest byte counters for the UI. |
| `ble` | **exclusive** | one BLE stack, one owner. `"peripheral"` or `"central"`. Second requester fails to start: `device busy: BLE passed through to guest "blinky-ble"`. Shared-GATT mux is a later iteration. |
| `usb` | exclusive per interface | device mode only. Interfaces are planned at boot into one composite descriptor (§8); a guest owns its interface(s) exclusively. HID grants are flagged in the claims table — a keyboard-capable guest is a keystroke injector and deserves visibility. |
| `bus` | non-conflicting | permissions only, see §5. |
| `ui` | non-conflicting | gate for §7 declarations. Limits enforced at declaration: ≤ 16 widgets / ≤ 4 KB panel; config writes schema-validated. |
| `storage` | private | `/guests/<id>/data/`, path-jailed open(), advisory quota checked on write (the returned file object is wrapped, so `seek`-past-quota is covered too). |

Error strings are Proxmox-flavored on purpose. `pin 2 already passed through to guest "blinky"` is both accurate and funny.

## 4. hal surface

Everything a guest can touch. Async where it blocks. The *names* are the contract; each runtime binds them idiomatically — `await hal.sleep_ms()` in mpy, plain `hal.sleep_ms()` in sol where every hal call is an implicit yield point.

```
hal.log(*args)                      → guest console (ring buffer + live stream)
hal.sleep_ms(ms) / hal.sleep(s)     → await-able; the only sleep that exists
hal.ticks_ms() / hal.time()         → monotonic ms / wall clock (read-only; supervisor NTP-syncs at boot + daily)
hal.rand(n)                         → urandom bytes
hal.spawn(coro)                     → tracked child task, cancelled with the guest
hal.gather(*aws) / hal.Event() / hal.Queue(n)

hal.pin(n)                          → .on() .off() .toggle() .value([v]) .irq(cb, edge)   # cb queued to guest loop, not real ISR
hal.pwm(n)                          → .freq(hz) .duty(0..1023)
hal.adc(ch)                         → .read_u16()
hal.i2c(bus)                        → .read(addr, n) .write(addr, buf) .mem_read/.mem_write   # addr checked against grant
hal.spi(bus, cs)                    → .xfer(buf) …

hal.net.socket(...)                 → async socket (subset of asyncio streams)
hal.net.get(url) / .post(url, …)    → tiny async HTTP client

hal.ble.advertise(...) / .gatt(...)  (peripheral)   hal.ble.scan() / .connect()  (central)

hal.usb.cdc()                       → async stream to the host (read/write/drain)
hal.usb.hid()                       → .send_report(buf) + helpers per profile:
                                       keyboard: .type(str) .press(code) .release(code)
                                       mouse: .move(dx, dy) .click(btn) · gamepad: .state(...)
hal.usb.midi()                      → .send(msg) / async iterator of incoming messages

hal.bus.publish(topic, msg)
hal.bus.subscribe(topic_filter)     → async iterator of (topic, msg)
hal.bus.retained(topic)             → last retained msg or None

hal.storage.open(path, mode)        → file in the guest's private dir
hal.status(text)                    → one-line status shown on the guest's UI card

hal.ui.panel(widgets)               → declare live panel (§7)
hal.ui.config(fields)               → declare configuration schema (§7)
hal.config.get(key[, default]) / .all()  → supervisor-owned persisted settings
hal.config.watch()                  → async iterator of (key, value) — live fields only
```

Pin IRQs: real ISRs run supervisor-side and enqueue events; the guest callback runs in its own task context. Latency is worse, safety is much better, and it keeps hard-ISR constraints out of the guest SDK. IRQ events get the bus treatment (§5): a bounded per-guest queue, drop-oldest on overflow, drop counter in the UI — a bouncy switch floods only its own guest, visibly.

## 5. Bus (the virtual switch)

Supervisor-owned pub/sub broker. In-memory, no persistence except retained messages.

- **Topics**: `segment/segment/...`, MQTT-style wildcards in filters: `+` one segment, `#` tail. Reserved roots: `$sys/` for supervisor telemetry (`$sys/heap`, `$sys/guest/<id>/state`, …) and `$ui/` for panel/config declarations (§7) — both supervisor-written, read-only to guests.
- **Messages**: JSON-serializable values only (dict/list/str/num/bool/None), ≤ 4 KB encoded. No live objects cross guest boundaries, ever — this is what keeps isolation honest.
- **Permissions**: `caps.bus.pub` / `caps.bus.sub` are lists of topic filters. Default grant if `bus` is present but empty: pub `"<id>/#"`, sub nothing. Publishing outside your grant raises in the guest; it never silently drops.
- **Delivery**: at-most-once, FIFO per subscription, via bounded queues (default 16). Overflow policy: drop-oldest, increment a per-subscription drop counter visible in the UI. A slow guest loses *its own* messages and nothing else. A chatty publisher is throttled only by its own awaits, so per-guest publish counters are surfaced in the UI — visible before they're a problem.
- **Retained**: `publish(topic, msg, retain=True)` keeps last message per topic (small cap, e.g. 32 topics) and hands it to new subscribers — right pattern for sensor-latest-value.
- **Port mirroring**: the broker bridges to a WebSocket (`/api/bus`). The UI and Claude Code can subscribe to `#`, watch all traffic live, and inject messages into any topic. This is the debugging feature. Slow WS clients get the same drop-oldest treatment as guests — a browser can starve itself, never the node.
- **A-Bus**: message envelope kept dialect-compatible with Auditable Works' A-Bus (topic + JSON body + optional retain semantics), so a notebook talking to a node over the WS bridge needs an adapter, not a translation layer. Compatibility is aspirational in zero, not tested.

## 6. HTTP API

Single bearer token (set at provision, stored in supervisor config), `Authorization: Bearer <token>`. JSON everywhere. Designed to be driven by curl / Claude Code; an `openapi.json` ships in flash at `/api/spec`.

```
GET    /api/node                        node info: board, profile, runtimes, heap, uptime, version
POST   /api/node/reboot                 reboot the node (applies any pending USB plan)
GET    /api/node/log                    the supervisor's own log ring

GET    /api/lib                         list the shared library store
PUT    /api/lib/{name}                  install/replace a shared library on a live node
DELETE /api/lib/{name}                  remove (refused while an installed guest imports it)
GET    /api/guests                      list: id, name, state, caps summary, mem, drops
POST   /api/guests                      create (multipart or JSON {manifest, files:{...}}); 409 if the id exists
GET    /api/guests/{id}                 full detail incl. manifest, claims, last traceback
PUT    /api/guests/{id}/files/{path}    upload/replace a bundle file (guest must be stopped)
GET    /api/guests/{id}/files/{path}    read a bundle file
GET    /api/guests/{id}/bundle          download the bundle as .tar (backup; the future migration primitive)
DELETE /api/guests/{id}                 remove bundle (must be stopped)

POST   /api/guests/{id}/start
POST   /api/guests/{id}/stop            ?grace_ms=2000
POST   /api/guests/{id}/restart
GET    /api/guests/{id}/console         last N lines (ring buffer)
WS     /api/guests/{id}/console/stream  live console
GET    /api/guests/{id}/config          values + declared schema + pending-restart set
PUT    /api/guests/{id}/config          validate & write → {applied_live: [], pending_restart: []}

GET    /api/claims                      the whole claims table (who owns which pin/addr/BLE)
GET    /api/usb                         current composite plan: interfaces, owners, live/inert
POST   /api/usb/replan                  rebuild descriptor from installed guests (re-enumerates!)
WS     /api/bus                         bus bridge: subscribe/publish frames
POST   /api/bus/publish                 one-shot inject {topic, msg, retain?}
GET    /api/bus/retained                retained message table
```

Uploads are atomic: file PUTs stage to `<path>.tmp` and rename, and the manifest is validated before acceptance — a failed or interrupted upload can never corrupt an installed bundle.

`/api/lib` semantics: `PUT` is last-write-wins on the store; replacing a library currently imported by a **running** guest is refused without `?force` (the guest keeps its loaded copy until restart regardless — Python's import cache sees to that; `?force` is for people who know this).

Transport is plain HTTP + bearer token; the trust boundary is the LAN (or the tailnet, if the node must be reachable beyond it). TLS on-MCU buys little here and costs RAM plus certificate liturgy — stated as a decision, not an oversight.

UI is one HTML file consuming exactly this API — node summary bar, guest cards (state, status line, mem sparkline, start/stop), console panes, claims table, bus monitor, per-guest panels and config forms, and the node dashboard.

## 7. Guest panels & configuration (declarative micro-UI)

Guests never serve UI. A guest *declares* a panel — a flat JSON widget list — and the supervisor's single HTML file renders it. State flows in over ordinary bus bindings; input flows out as ordinary publishes. The feature is a thin convention on top of §5, not new machinery.

### Declaration

`hal.ui.panel(widgets)` publishes the list retained to `$ui/<id>/panel` (supervisor-written; guests cannot publish to `$ui/` directly). Requires `"ui": true` in caps. Limits: ≤ 16 widgets, ≤ 4 KB encoded, `"v": 0` envelope for forward compat. Redeclaring replaces the panel (dynamic panels are allowed but discouraged — declare once at startup).

### Widget vocabulary (zero)

| type | reads (`bind`) | writes (`set`) | extras |
|---|---|---|---|
| `value` | ✓ | — | `unit`, `fmt`, `path`, `spark: true` |
| `gauge` | ✓ | — | `min`, `max`, `unit` |
| `indicator` | ✓ | — | bool → seal dot; `on`/`off` labels |
| `text` | ✓ | — | last string; `lines: N` for a mini log |
| `button` | — | ✓ | `msg` payload, `confirm: true` for destructive acts |
| `toggle` | ✓ | ✓ | bool state topic + command topic (MQTT switch pattern) |
| `slider` | ✓ | ✓ | `min`, `max`, `step`, `unit` |
| `select` | ✓ | ✓ | `options: [...]` |

Common fields: `w` (type), `id` (unique in panel), `label`, `size: "s"|"m"|"l"` (flow-layout hint; no nesting in zero). `bind` = state topic, `path` = dotted key into a JSON payload, `set` = command topic.

### Rules that keep it honest

- **History lives in the browser.** `spark`/`gauge` trends are client-side ring buffers; the guest publishes single values and stores nothing.
- **Text nodes only.** Labels and values are never interpreted as HTML. No links, no images, no style injection.
- **Grant-checked at declaration.** Every `bind` must be a topic *someone* may publish; every `set` must fall within the declaring guest's own `sub` grants — a panel can command its guest, never its neighbors.
- **Unknown widget types render as an inert chip** ("unknown widget: xyz"), so old firmware degrades gracefully under a newer UI and vice versa.
- **Panels outlive their guests.** The retained declaration persists across crash/stop; the renderer grays the panel and freezes last values (staleness driven by `$sys/guest/<id>/state`). A frozen instrument beats a vanished one.

### Configuration schema

Same primitives, different tense. `hal.ui.config(fields)` declares a settings form; fields carry `key`, `default`, and a `live` flag instead of `bind`/`set`:

```python
await hal.ui.config([
  {"key": "period_ms", "w": "slider", "label": "Default period",
   "min": 50, "max": 2000, "step": 50, "unit": "ms", "default": 500, "live": True},
  {"key": "invert", "w": "toggle", "label": "Invert output",
   "default": False, "live": False},
])
```

- **The supervisor owns the store**: `/guests/<id>/config.json`, validated against the declared schema on every write. The schema itself is retained at `$ui/<id>/config`, beside the panel. Editable from UI and API **even while the guest is stopped** — exactly like VM options.
- **Reads**: `hal.config.get(key)` / `.all()` at startup; `live: true` fields also stream through `hal.config.watch()` while running.
- **Pending values**: editing a `live: false` field on a running guest stores the value and badges it *pending restart* (amber, Proxmox-style). Restart applies and clears.
- Defaults materialize into the store on first declaration. Keys in the store that the schema no longer declares are preserved but flagged in the UI.
- Rule of thumb: panels answer *what is it doing*; config answers *how should it behave*.

### Where panels appear

On the guest's Overview tab, and composited on a node-level **Dashboard** tab — every running guest's panel on one wall. UI events publish with an `origin` field (`{"origin": "ui", ...}` merged into `msg`) so guests and the bus monitor can tell a human tap from machine traffic.

## 8. USB (device passthrough)

Because jormungandr's management plane is WiFi, the native USB port isn't sacred — the supervisor reclaims it and hands it out to guests as a peripheral. Device mode only in zero; MicroPython's host-mode story is not settled enough to spec against. (On S3 devkits the separate UART bridge port keeps a hardware debug serial alive regardless.)

### Capability grammar

```json
"usb": {
  "cdc": true,
  "hid": "keyboard",
  "midi": true
}
```

- `cdc`: a serial interface to the host, surfaced as `hal.usb.cdc()` async stream.
- `hid`: `"keyboard"` | `"mouse"` | `"gamepad"` | `{"report_desc": "file-in-bundle.bin"}` for raw descriptors.
- `midi`: a MIDI interface.
A guest may declare several; each granted interface is exclusively owned, listed in the claims table, and HID grants carry a visible flag (keystroke injector — say it out loud in the UI).

### The enumeration rule

A composite USB descriptor is built **once, at boot**, from the declared caps of **all installed guests** — not just running ones. You cannot hot-add an interface without the host seeing the whole device drop and re-enumerate, so:

- A stopped guest's interfaces stay enumerated but **inert** (HID sends nothing, CDC reports no carrier). Starting the guest brings them to life — the host never sees a disconnect.
- Installing or removing a guest with `usb` caps changes the plan; the change is **pending until reboot** or an explicit `POST /api/usb/replan` (which re-enumerates, and the UI says so in amber). This is precisely the "changing virtual hardware requires a restart" rule VMs live by, so the fiction holds.
- Endpoint budget is finite (roughly 6 usable endpoints on the S3's OTG controller; CDC costs more than HID). The planner allocates in install order and refuses installs that don't fit, with a per-interface cost breakdown in the error.

### Notes

- The supervisor's own REPL/CDC is *not* part of the plan by default — management is WiFi. A node config flag can reserve one CDC for a supervisor emergency console.
- Obvious demo: `"ble": "central"` + `"hid": "keyboard"` reimplements RelayKVM as ~40 lines of guest code; a CDC guest bridging `hal.bus` topics to serial gives any host process a wired tap into the node's bus.

## 9. Runtime profiles

Everything above is deliberately runtime-agnostic: manifests, capabilities, claims, bus, panels, config, and the API never mention a language. A profile is an engine room implementing that contract.

### Profile mpy — MicroPython + asyncio

Stock firmware, richest driver ecosystem, fastest path to a running node. Soft isolation: namespaces, kill-at-await, advisory memory (§10 trade-offs apply in full). The right first implementation and the right flagship while the project is young — a weekend to blinky-over-HTTP.

### Profile sol — ESP-IDF + Lua

The convergent form of SOL-8, less mad: the sun hosting the moon (and Sól holding her own against the serpent). Custom C firmware where each guest is its own `lua_State`. What the C buys:

- **One state per guest, ROTables for everything shared.** Stdlib and hal bindings compile into read-only tables in flash, shared by all states at zero RAM. A bare state is 4–5 KB; a typical guest 8–16 KB; driver-heavy, 24–32 KB.
- **Arena allocator per guest.** Every allocation a guest makes comes from its private region, so `mem_kb` becomes an *enforced* quota (over-allocation raises inside the guest) and kill is "free the arena" — instant, complete, leak-proof.
- **Preemption via instruction-count hooks** (`lua_sethook`): a tight loop physically cannot starve the node. Two scheduling models:
  - *(a) Single supervisor task, round-robin between states.* Zero per-guest C stack; preemption at Lua-instruction granularity; hal C functions must return quickly (async completion via callbacks). Leaf nodes take this.
  - *(b) Task-per-guest on FreeRTOS.* Full preemption including mid-C-call, priority scheduling, core pinning; costs 4–8 KB resident stack per guest. Flagships may take this.
- **Kill in layers**: error-from-hook → clean Lua unwind with `finally`-equivalent cleanup; `vTaskDelete` + arena free as the hammer (model b only). The §1 watchdog demotes to telemetry — starvation is structurally impossible.
- **The firmware boundary rule**: the C core knows *buses*, never *devices*. I2C/SPI/UART/GPIO/PWM primitives in C, written once; every driver that knows what a BME280 is lives in Lua in `/lib`, installed over HTTP. Shared libs are bytecode-precompiled on device (`lua_dump`).
- **OTA**: A/B app partitions with health-check rollback; `/guests` + `/lib` live on the data partition and survive updates. A board is flashed exactly once (esp-web-tools from the browser), then never cabled again.
- **Authoring**: Lua directly, or transpiled. AIR already has structured regions and JS/adder frontends; an AIR → Lua backend makes the authoring language a bundle build detail the node never sees. A guest written in adder's Python, compiled through AIR, running in a Lua arena on a two-dollar board is left as an exercise in GCU maximalism.

Budget check on a C3 (~400 KB SRAM): IDF + WiFi + supervisor ≈ 120–150 KB, leaving room for **8–12 real guests** with enforced walls — versus mpy needing ~100+ KB before the first guest exists.

### Cluster shape (v1, for orientation)

Heterogeneous: flagship nodes (either profile) serve the single-file UI; sol leaf nodes run headless, exposing only the API — the UI manages any node from anywhere, because it was never coupled to the node serving it. Guests migrate to whichever node has the right peripherals and enough arena. And the maddest SOL-8 idea survives translation: a ladder-logic interpreter is just a guest — the fantasy PLC becomes an app on the fantasy hypervisor.

## 10. Trade-offs accepted in zero

- **No preemption** *(mpy; resolved in sol)*. A guest in a tight loop can't be killed, only flagged — and while it spins, the API is starved too. The WDT blame-file + autostart-disable (§1) is the recovery story: the node reboots reachable, with the culprit named and benched. RP2350 `_thread` exile to core1 is the partial remedy.
- **Advisory memory** *(mpy; enforced in sol)*. Real per-guest heaps don't exist in MicroPython; we measure and refuse-to-start rather than pretend to enforce.
- **Soft isolation** *(mpy; arena-walled in sol)*. A determined guest could fish objects out of builtins, and shared `/lib` modules share module-level state (§1) — stateless-driver convention, not machinery. Out of scope; the guest author is you.
- **BLE exclusive.** Multiplexing GATT under one advertisement is real work; exclusive mode ships in a weekend.
- **Single node, single token.** mDNS discovery + cluster view + "migration" (POST bundle to peer, flip autostart) is the obvious v1 feature, deliberately out of zero.
- **USB is device-only and boot-planned.** No host mode, no hot-plug of interfaces; layout changes re-enumerate. Honest, and matches the VM-hardware fiction.
- **sol costs a firmware project.** The C core, hal bindings, and every driver are yours to write; no community modules. The compensation is hard isolation and two-dollar leaf nodes. mpy first is how the spec gets validated before that bill is paid.

## 11. Open questions

1. ~~Working name~~ — **RESOLVED: jormungandr.** The world-serpent hosting the smallest pythons; the GCU's largest name on its smallest machines, on purpose. One ASCII spelling (`jormungandr`), `jorm` for the CLI/daemon, `sol` doubles as Sól so the Norse and SOL-8 registers cohere. Culture-ship names stay reserved for hardware nodes. (Collision note: the retired Cardano-era node of the same name is faded and lives in an unrelated ecosystem.)
2. Profile sequencing: mpy-zero first then sol (recommended: validates the spec cheaply), or straight to sol and accept the longer road to first demo? *Proposed: mpy first, emphatically — sol is a firmware project; validate the contract where the contract is cheap.*
3. sol scheduling on flagships: single-task round-robin (a) everywhere for simplicity, or task-per-guest (b) where RAM allows? *Proposed: (a) everywhere for zero/v1 — uniform semantics across the fleet; (b) becomes a flagship build flag when a workload proves the need.*
4. AIR → Lua backend: prerequisite for sol adoption, or a later luxury with hand-written Lua sufficing for zero-era guests? *Proposed: later luxury — don't chain a compiler project to a firmware project.*
5. Should `caps.pins` support a `"shared-read"` input mode (two guests watching one button)? Cheap to add, breaks the clean exclusivity story. *Proposed: yes, as an explicit distinct mode (`"mode": "in-shared"`) — exclusivity stays the default story, the claims table shows N watchers, and two guests watching one button is too real to forbid for aesthetics.*
6. Console encoding: plain lines, or structured `{ts, level, text}` from day one? Structured is nicer for the UI, slightly heavier on flash-side ring buffers. *Proposed: structured from day one — this project is built for Claude-driven debugging, and structured logs are the difference between grep and archaeology. Pack tuples in the flash ring; JSON only at the WS boundary.*
7. Does the supervisor expose `hal.bus` to *itself* for built-in publishers (clock tick, button events on reserved pins)? Probably yes — `$sys/clock/tick` at 1 Hz makes demo guests trivially interesting. *Proposed: emphatically yes — it dogfoods the bus and makes every demo guest interesting in five lines.*
8. Panels: does a `slider` echo its own `set` publishes optimistically in the UI, or wait for the guest to republish state on `bind`? Waiting is honest but feels laggy; probably optimistic-with-revert. *Proposed: optimistic-with-revert — the established MQTT-UI pattern, and the `origin` field already provides the reconciliation machinery.*
9. USB: should the supervisor offer a zero-code option to mirror a guest's console onto a CDC interface (`"cdc": "console"`)? Free debugging for headless setups, but it blurs "guest owns the interface". *Proposed: yes, but supervisor-owned — the claims table lists that interface as `owner: supervisor, purpose: console of <guest>`; ownership stays unblurred because the table says exactly what it is.*
