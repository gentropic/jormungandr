#!/usr/bin/env bash
# Host display-console acceptance (two/§8, revised) — slice 1 of the host-owned-display
# roadmap. Proves the Display interface + MAX7219 backend + host status Console off
# hardware: the sim's machine.SPI (no-op write) plus the unix port's real framebuf let us
# check that the host actually paints status frames and clears them, with no board.
#
# This is a library test, not a sim node: it points MICROPYPATH straight at supervisor/
# (so `jorm.*` resolves) with sim/ shadowing `machine`. The physical MAX7219 render is the
# board's job (slice 4).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"

if [ ! -x "$MPY" ]; then
    echo "error: micropython not found at $MPY (set MPY=/path/to/micropython)" >&2
    exit 1
fi

export MICROPYPATH="$ROOT/sim:$ROOT/supervisor:$ROOT/supervisor/lib:.frozen"

TEST="$(mktemp --suffix=.py)"
trap 'rm -f "$TEST"' EXIT
cat > "$TEST" <<'PY'
from jorm.display import open_display, Display
from jorm.console import Console

SPEC = {'id': 'primary', 'kind': 'max7219',
        'spi': 1, 'sck': 25, 'mosi': 32, 'cs': 33, 'n': 4}


def check(cond, msg):
    if not cond:
        print('FAIL:', msg)
        raise SystemExit(1)
    print('  ok:', msg)


# the factory builds a real Display through the interface
d = open_display(SPEC)
check(isinstance(d, Display), 'open_display returns a Display (max7219 backend)')
check(d.size() == (32, 8), 'size is 32x8, got %r' % (d.size(),))
check(sum(d.buf) == 0, 'panel blank after init()')

c = Console(d)

# a status paints pixels
c.status('boot')
boot = bytes(d.buf)
check(sum(boot) > 0, 'status("boot") drew pixels')

# a DIFFERENT status yields a DIFFERENT frame (text actually renders content)
c.status('wifi')
check(bytes(d.buf) != boot, 'a different status produced a different frame')

# same text is deterministic
c.status('boot')
check(bytes(d.buf) == boot, 'same status re-renders the same frame')

# a line wider than the panel clips instead of crashing
c.status('no link')
check(sum(d.buf) > 0, 'over-wide status clips and still draws')

# reclaim message renders
c.down('clock')
check(sum(d.buf) > 0, 'down("clock") renders a reclaim frame')

# clear blanks it
c.clear()
check(sum(d.buf) == 0, 'clear() blanks the panel')

print('DISPLAY-CONSOLE OK')
PY

echo "== Display interface + MAX7219 backend + host Console (sim, no board)"
OUT="$("$MPY" "$TEST" 2>&1)" || { echo "$OUT" | grep -v '^\[sim\]'; echo; echo "DISPLAY-CONSOLE acceptance: FAIL"; exit 1; }
echo "$OUT" | grep -v '^\[sim\]'      # drop the SPI/pin stub chatter, keep the checks
echo "$OUT" | grep -q 'DISPLAY-CONSOLE OK' || { echo "DISPLAY-CONSOLE acceptance: FAIL (no OK marker)"; exit 1; }
echo
echo "DISPLAY-CONSOLE acceptance (sim): ALL PASS — the host owns the panel and paints status"
