# jormungandr — milestones

Zero targets the mpy profile on an ESP32-S3 (a WROOM-32 covers M1, but M2's live
WebSocket load is where classic-ESP32 heap runs out of grace). Each milestone has an
acceptance test; "done" is unambiguous or it isn't done.

## M1 — blinky over HTTP (the walking skeleton)

Supervisor skeleton: boot → `settings.json` → WiFi → mDNS (`jorm-<mac4>`) → bearer-token
API on microdot. Manifest loader, claims table (pins only), guest lifecycle
(start/stop/crash), structured console rings, `hal` core: `log`, `sleep_ms`, `ticks_ms`,
`pin`, `spawn`.

**Acceptance** (all via `jorm`, which is born in this milestone as API client + test
harness):

```
jorm node                    # board, heap, uptime — the node answers
jorm create examples/blinky  # POST /api/guests — manifest validated, bundle staged
jorm start blinky            # LED physically blinks
jorm console blinky          # structured lines stream live
jorm claims                  # pin 2: passed through to guest "blinky"
jorm start blinky2           # second claimant refused: pin 2 already passed through
jorm stop blinky             # LED stops; claims table shows pin 2 free
jorm create bad-manifest     # unknown cap key → refused, clear error
```

Plus the ungovernable-guest drill: a `while True: pass` guest starves the node → WDT
reset → node comes back reachable with the culprit's autostart disabled and badged
(attribution via the RTC-memory current-guest register, spec §1 — the drill also verifies
a *sleeping* bystander guest is not blamed).

**Status: done on the sim** (`tools/accept-m1.sh`, all pass) — the WDT drill is the one
item that needs real silicon.

## M2 — the bus + the UI file

Broker (topics, grants, bounded queues, drop counters, retained), `$sys/` telemetry +
`$sys/clock/tick`, WS bridge (`/api/bus`), guest console WS streams. The single HTML
file: node bar, guest cards, console panes, claims table, bus monitor.
**Acceptance**: two guests talking over the bus watched live from the bus monitor; a
slow subscriber visibly drops *its own* messages and nothing else.

**Status: done** — server side by `tools/accept-m2-bus.sh`, the UI by
`tools/verify-ui.mjs` (playwright, 18 checks incl. stale-mode honesty), both all-pass
against the sim. UI spec ratified in SPEC-jorm-ui.md.

## M3 — panels, config, the rest of hal

`hal.ui.panel` / `hal.ui.config` / `hal.config.*`, `$ui/` retained declarations,
dashboard compositing, pending-restart amber. hal fills out: `pwm`, `adc`, `i2c`, `spi`,
`net` (client), storage jail + quota.
**Acceptance**: a sensor guest with a gauge + slider panel, configured from the UI while
stopped, panel grays on crash and freezes last values.

**Status: done** — `tools/accept-m3.sh` (server) + `tools/verify-ui.mjs` (24 browser
checks), both all-pass against the sim. The sensor guest is `examples/thermo`.

## M4 — USB (needs an S3)

Composite planner, inert-when-stopped interfaces, replan-with-amber, `hal.usb.cdc/hid/midi`,
`"cdc": "console"`. **Acceptance**: the RelayKVM party trick — `ble: central` +
`hid: keyboard` in ~40 guest lines.

## M5 — silicon (the drill that software cannot run)

Flash `jorm-c510` (ESP32-S3 N16R8, `SPIRAM_OCT` build — the octal PSRAM is invisible to
the stock build), write real `settings.json`, run every acceptance suite against the
board, then the ungovernable-guest drill: `while True: pass` starves the node → hardware
WDT resets it → it comes back reachable with the culprit named from RTC memory, its
autostart disabled, and a sleeping bystander guest *not* blamed.
**Acceptance**: all four suites green against the board; the drill names the right guest.

**Status: DONE.** `tools/accept-drill.sh`, on `jorm-c510`, 2026-07-13:

```
watchdog reset: guest "hog" held the CPU — autostart disabled
```

The node starved (heartbeat, flagging, and web server all dead), the hardware
watchdog reset it, it came back on its own, named the culprit from the RTC
register that survived the reset, benched it — and did **not** blame `blinky`,
which slept through the whole thing. Under the spec's original stalest-`last_yield`
heuristic, the sleeping bystander is precisely who would have been accused.

**Status: board is up.** `jorm-c510` runs on an ESP32-S3 N16R8 (MicroPython 1.28.0,
SPIRAM_OCT — 8.3 MB free heap), reachable at `http://jorm-c510.local` (mDNS works;
`network.hostname()` registers the responder). All four suites pass on silicon.
`examples/beacon` drives the devkit's RGB LED on GPIO48 as the node's own status
light — green breathing on `$sys/clock/tick`, red when any guest crashes. **The
ungovernable-guest WDT drill is the one item left in zero.**

## Where zero stands

M1 ✓ · M2 ✓ · M3 ✓ · M5 ✓ (silicon, incl. the drill). **M4 (USB) is the last
milestone in zero**, and it is the only one that has not been started.

Beyond the milestones, zero also grew what the founding spec promised but did not
schedule: the `/lib` store and three-tier imports, supervisor OTA with
health-check rollback, NTP, sampled per-guest memory, an `rgb` capability, and a
UI that works on a phone. All of it is verified by `tools/accept-*.sh` +
`tools/verify-ui.mjs`, against the sim and against the board.

## Later (v1-shaped, out of zero)

mDNS discovery + cluster view + bundle migration; BLE GATT mux; AP-mode provisioning
portal; freeze-to-firmware builds; profile sol (§9 — the firmware project, paid for by a
validated spec).
