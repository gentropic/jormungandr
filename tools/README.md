# tools

## Board tooling

```
uv venv .venv
uv pip install --python .venv esptool mpremote pyserial
```

Invoke as **modules**, never via the console scripts:

```
.venv/Scripts/python -m esptool --port COM14 chip-id     # Windows
.venv/Scripts/python -m mpremote connect COM14 ls
```

On Windows, Smart App Control blocks the generated `esptool.exe` shim
(`os error 4551`); `python -m esptool` is the same code and is not blocked.

## The watchdog problem (read before deploying)

A provisioned node arms the hardware WDT, and **an ESP32 WDT cannot be disarmed —
not even by a soft reset.** So the obvious deploy path does not work: Ctrl-C into
the REPL, and eight seconds later the watchdog reboots you out of it, mid-copy.
That is the WDT behaving exactly as designed (§1: the node always comes back
reachable) and it is fatal to deployment. There are two ways in, both of which
stop the node *before* it arms anything:

1. **Over the network** — `POST /api/node/maintenance`. The node reboots into a
   state where it starts nothing and arms nothing, and waits at the REPL. The
   flag is consumed on that boot, never sticky. This is the normal path, and
   `deploy.sh` takes it automatically when `JORM_URL` + `JORM_TOKEN` are set.
2. **Over serial** — the two-second boot escape window (`main.py`). This is the
   only way into a node that is off the network, and `push.py` exists because
   getting it right requires owning the timing (below).

## `push.py` vs `mpremote`

`mpremote` is the better tool and `deploy.sh` uses it — but it cannot bootstrap a
node from cold, because **opening the serial port resets the ESP32**, so
mpremote's Ctrl-C is sent while the board is still in the ROM bootloader and is
lost. `push.py` owns the timing instead: it resets the board, waits for the boot
banner, lets the escape window close, and *then* interrupts — during WiFi
association, before `asyncio.run()` arms the watchdog. An interrupt there raises
an ordinary `KeyboardInterrupt` and drops to the REPL.

(It interrupts *after* the window rather than inside it on purpose: an older
`main.py` escaped the window with `SystemExit`, and MicroPython treats a forced
exit from `main.py` as a **soft-reset request** — so catching the window bounced
the board straight back into the boot it was escaping, forever. A recovery tool
has to work against whatever vintage of `main.py` is already on the board.)

```
.venv/Scripts/python tools/push.py COM14 [--settings]   # cold bootstrap
JORM_URL=... JORM_TOKEN=... bash tools/deploy.sh COM14   # normal deploy
```
