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

## 4. Budget & construction (measured 2026-07-13; the earlier number was invented)

The original "≤ 96 KB" was a figure I made up and then quoted back as though it
were a constraint. It is not. Measured on `jorm-c510`:

| what | measured | is it a limit? |
|---|---|---|
| flash | **13.5 MB free** of 14 MB; the UI is 72 KB | no — the UI is 0.5% of it |
| RAM to serve | **~1 KB**, independent of file size (microdot streams in 1 KB chunks) | no |
| wire | **~90 KB/s** over WiFi → 72 KB in 0.73 s | **yes — this is the only one** |

So the constraint is **load time, and nothing else** — and gzip mostly removes even
that. The node serves a pre-gzipped `ui.html.gz` (`Content-Encoding: gzip`) when the
client asks for it, falling back to the plain file when it cannot: **20.8 KB on the
wire, 0.38 s.** The node has no compressor, so the tool that pushes the UI produces
the `.gz` and ships *both* — a stale `.gz` beside a fresh `.html` would serve
yesterday's interface to every browser and today's to nobody.

What this changes: a much larger surface is affordable. A geas + terminal bundle at
~500 KB raw is ~150 KB gzipped — under two seconds from the node's own flash. So
**geas can be served by the node** (as a second page), not merely hosted elsewhere
and pointed at it. What the node still must not do is *run* a shell: geas is
JavaScript and runs in the browser, where it belongs (see spec-zero §11.24).

Construction is unchanged: hand-written vanilla JS + template literals, no
framework, no build step. シングルファイルデプロイ. Token entered once, kept in
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
- **The root is the cluster, and it has a name.** Not "Datacenter" — a cluster,
  renameable in place (right-click the root), persisted in `settings.json` beside
  the node it describes. Zero has exactly one node, so the cluster is an
  aspiration with a name; that is the point, and v1 adds nodes to this tree
  rather than rearranging it. Each level indents deeper than its parent, which
  sounds too obvious to state until the root is indented deeper than its own
  children and the tree quietly reads backwards.
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
