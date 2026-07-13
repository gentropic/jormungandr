# Vendored web assets

- `term.js`, `term.css` — [@gcu/term](https://github.com/gentropic/auditable/tree/main/ext/term),
  the GCU terminal emulator (VT/ANSI + DOM renderer), MIT. Copied from
  `auditable/ext/term/` on 2026-07-13. Unmodified — the prebuilt ESM bundle.

  Chosen over xterm.js for three reasons, in order: it reads CSS custom
  properties for its palette (`--gcu-term-*`), so mapping it onto Switchboard's
  `--au-*` themes it in both light and dark for free; it is zero-dependency; and
  it is a third of the size (74 KB vs ~250 KB).

These are served from the node's own flash, lazily — the Shell tab fetches them
on first open, so a phone loading the dashboard never pays for a terminal it did
not ask for.
