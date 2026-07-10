# jormungandr

**The world-serpent, hosting the smallest pythons.** jormungandr is a guest supervisor
for microcontrollers with a Proxmox-flavored web UI and API: guests are little
capability-sandboxed programs (MicroPython first, Lua later) that check in with a
manifest, get granted pins/buses/radios from a claims table, talk to each other over a
built-in pub/sub bus, and show up as cards — consoles, panels, config forms, amber
badges — in one single-file web UI. Deliberately the largest name in the GCU on its
smallest machines. The CLI is `jorm`; nobody sees the whole serpent at once.

## Status

**Spec stage.** The whole design — model, capability grammar, hal surface, bus, API,
declarative panels, USB passthrough, and the two runtime profiles (`mpy` now, `sol`
someday) — lives in **[SPEC-jormungandr-zero.md](SPEC-jormungandr-zero.md)**. Zero is
the mpy profile on an ESP32-S3: a weekend to blinky-over-HTTP, honesty about every
limit it can't enforce.

## Lineage

A GCU project. The bus speaks A-Bus dialect (Auditable Works); the `sol` profile is
SOL-8's convergent form (the sun hosting the moon — and Sól holding her own against
the serpent); espmox is the sibling that hosts *emulated* guests where jormungandr
hosts real ones.

Sister repos:

- **[auditable](https://github.com/gentropic/auditable)** — Auditable Works: the
  single-file notebook, the `ext/` component library, A-Bus.
- **[espmox](https://github.com/gentropic/espmox)** — the fantasy datacenter: emulated
  PDP-11s booting real 2.11BSD on the same class of silicon.
- **[SOL-8](https://github.com/endarthur/SOL-8)** — the fantasy PLC; `sol` is its sun,
  the mesh and the SCADA dress are its inheritance.
- **[design](https://github.com/gentropic/design)** — the GCU's design system,
  conventions, and spec inbox.

Local convention: `spec_inbox/` is gitignored — draft specs live there privately;
ratified ones graduate to the repo root.
