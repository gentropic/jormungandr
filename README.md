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
