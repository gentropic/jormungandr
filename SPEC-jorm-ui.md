# SPEC-jorm-ui — the single file (ratified 2026-07-12)

The M2/M3 UI: one HTML file, served from flash at `GET /`, equally at home opened from
`file://` and pointed at any node (URL + token, remembered per-node in localStorage) —
the UI was never coupled to the node serving it (spec §6). Proxmox's *shape*,
Switchboard's *body*, the bus's *pulse*.

## 1. Design system

**Switchboard 1.3 verbatim** (`auditable/ext/switchboard`) — tokens vendored into the
`<style>` block, components read `--au-*` only. Dark (basalt) default, light
(equipment gray) via the standard `data-theme` flip + `prefers-color-scheme`
first-paint. Fonts are the fallback stacks only — no Barlow/Space Mono binaries on
the MCU; the UI is **WU-class** (works underground). No emoji, no shadows, no seventh
accent; the humor lives in text fidelity.

**State → accent (glyph-paired, always):**

| state | accent | glyph |
|---|---|---|
| running | go (green) | ● |
| stopped | neutral (`--au-fg-soft`) | ○ |
| starting / stopping | caution | ◌ |
| crashed | fault | ✗ |
| unresponsive | caution | ⚠ |
| suspected (WDT) | fault badge | ⚠ |
| pending restart | caution chip | ⚠ |
| selected (tree) | violet | — |
| origin: ui (bus) | action (orange) | ▸ |

The jormungandr spec's "amber" reads as Switchboard **caution** (hue-honest: yellow).

## 2. Regions (Proxmox shape)

```
┌──────────────────────────────────────────────────────────┐
│ masthead: JORMUNGANDR · jorm-c510 │ readout gauges │ ◐    │
├───────────┬──────────────────────────────────────────────┤
│ tree      │ tabs (contextual to selection)               │
│  node     │                                              │
│   guests  │ content panels                               │
└───────────┴──────────────────────────────────────────────┘
```

- **Masthead** — identity (hostname · board · profile · version) + a Device Readout
  strip: heap (sparkline), uptime, guests running/total, and the **tick pulse** (◉
  blinking `steps(1)` at 1 Hz, driven by real `$sys/clock/tick` — the UI's heartbeat,
  exempt from reduced-motion because it carries state). Theme toggle.
- **Tree (left rail)** — resource tree: node → guests, each row a status glyph + id.
  Selected = violet. Zero-era it is one node deep; the markup is written for the v1
  cluster (nodes → guests) from day one. On `pointer: coarse` / narrow, the tree
  becomes a drawer. *This is Switchboard's missing tree pattern — jormungandr is the
  second consumer; feed the pattern back upstream once it settles.*
- **Content tabs** — node selected: `Summary · Bus · Claims · Log` (M3 adds
  `Dashboard`; M4 `USB`). Guest selected: `Overview · Console · Config` (+ `Files`
  later). Tabs render lazily; a hidden tab holds no WS.

## 3. Live data (one socket to rule them)

- **One WS to `/api/bus`**, subscribed `$sys/#` at boot. Retained
  `$sys/guest/+/state` seeds the tree instantly; `$sys/heap` feeds the gauge;
  `$sys/clock/tick` is liveness. `GET /api/guests` reconciles on every (re)connect.
- **Staleness is a first-class mode.** 3 missed ticks (>3.5 s) → the chrome greys,
  values freeze (they do not blank), a caution banner shows *"signal lost — last data
  Ns ago"*, reconnect with backoff. A frozen instrument beats a lying one; no fake
  data, ever (no-silent-losses).
- **Commands are optimistic-with-revert** (spec §11.8 generalized): press STOP →
  button enters caution-pending → confirmed by the retained state transition, or
  reverts with a fault toast after 5 s quoting the API error verbatim.
- **Consoles** — per-guest WS (`/console/stream`) opened when the Console tab shows,
  closed when it hides. Terminal pattern (§6.6): `sys` dim, `info` text, `error` red.
- **Bus monitor** — re-scopes the main WS to `#` while open (drops back to `$sys/#`
  after). Client-side ring, 500 lines, pause button, per-line topic in info blue,
  `origin: ui` lines marked in action orange. The publish box (topic + JSON + retain)
  lives here and only here.

## 4. Budget & construction

- **≤ 96 KB uncompressed, target ~24 KB gzipped in flash.** Hand-written vanilla JS +
  template literals; no framework, no build step. シングルファイルデプロイ.
- Served with `Cache-Control` friendly headers later; token entered once, kept in
  localStorage keyed by node URL; the WS uses `?token=` (browsers can't set headers).

## 4b. Phones and pointers (ratified 2026-07-13)

- **The tree becomes a drawer**, not a squashed column: on a phone you navigate,
  then read. ☰ opens it, a scrim dismisses it, selecting closes it. The masthead
  sits *above* the drawer — an open drawer that covers the button which closes it
  is a trap, and a 390px viewport found that one the moment it was asked to. The
  drawer's offset is *measured* from the masthead (which wraps to two rows on a
  phone), never guessed.
- **Switchboard's 44px tap floor is a floor** (`pointer: coarse`), applied to tree
  rows, tabs, buttons, and menu items.
- **Context menus, because Proxmox is a right-click program.** Right-click (or
  long-press — a phone has no right button, but it is the same gesture) on the
  node or any guest. Guest menus are state-aware: Start is disabled on a running
  guest, Remove on anything that isn't stopped. Destructive items **arm rather
  than ask** — one more deliberate click on a control that has visibly changed its
  mind about what it does. Escape closes. Menus are surface-bright + 1px border:
  depth from the surface register, never a drop shadow.
- **The bus monitor confesses its own drops.** A browser that falls behind loses
  its own messages (spec §5) — and is told so, in caution, because a monitor that
  quietly drops is a monitor that lies about what the bus carried.

## 5. Decisions (ratified 2026-07-12, against mock zero)

1. **Masthead carries link + tick**; drop counters live in the node Summary readout,
   not the masthead. The tick dot is lit by *received* ticks, not a free-running
   animation — the pulse is data, or it is nothing.
2. **Client-side scrollback: 500 lines** (consoles and bus monitor both).
3. **No empty rooms.** Tabs appear when their milestone lands — Dashboard and Config
   arrive with M3, USB with M4. M2 ships node `Summary · Bus · Claims · Log` and
   guest `Overview · Console`.
4. **Both themes from day one** (Switchboard doctrine), dark default, persisted.
5. The mock (`spec_inbox/jorm-ui-mock.html`, private) is the visual reference;
   the real file is `supervisor/ui.html`, served unauthenticated at `GET /` (the app
   shell is public; every byte of data behind it needs the token).
