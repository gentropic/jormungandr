# Vendored web assets

Served from the node's own flash, **lazily** — the Shell tab fetches them on first
open, so a phone loading the dashboard never pays for a shell it did not ask for.

- `term.js`, `term.css` — [@gcu/term](https://github.com/gentropic/auditable/tree/main/ext/term):
  the GCU terminal emulator (VT/ANSI + DOM renderer). MIT. 74 KB → 21 KB gzipped.

  Chosen over xterm.js for three reasons, in order: it reads its palette from CSS
  custom properties (`--gcu-term-*`), so mapping them onto Switchboard's `--au-*`
  themes it in light and dark **without it knowing either word** — xterm.js needs a
  bridge and would be the only object on the node that disobeys the design system.
  It is zero-dependency. And it is a third of the size.

- `geas.js` — [@gcu/geas](https://github.com/gentropic/auditable/tree/main/ext/geas):
  the GCU shell. POSIX lexer, parser, executor, 100+ builtins. MIT. 690 KB →
  196 KB gzipped, ~2 s from the node once, then cached.

  Its VFS contract is nine methods, and `shell.js` binds them to `/api/fs` — so
  **the node's flash is the shell's filesystem**. `ls /guests`, `cat /lib/*.py`,
  `cat main.py | grep hal`, `for g in /guests/*; do …; done`. Pipes and globs, on
  an ESP32.

Copied unmodified from `auditable/ext/` on 2026-07-13; both are prebuilt ESM
bundles with no external imports.

**Nothing here runs on the MCU.** geas is JavaScript and runs in the browser; the
node's whole side of it is nine HTTP methods. That is the argument against a Unix
on the chip, made concrete — the shell is a client, and the node stays a node.
