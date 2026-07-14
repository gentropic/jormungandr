#!/usr/bin/env bash
WHERE="sim"
# Clock-guest acceptance (two/§7): the clock as a GUEST, run on a sim node. The panel is
# stubbed — sim machine.SPI (a no-op write) plus the unix port's real framebuf — so this
# exercises all the SOFTWARE off-hardware: the matrix cap surviving manifest validation,
# hal.matrix() not tripping the guest import guard, the panel/config declaration, and the
# render loop actually running. The physical MAX7219 render is the board's job (and is
# proven separately — the panel lights). Every bug this would have caught was, shamefully,
# first caught on real silicon over serial.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
JORM="python3 $ROOT/cli/jorm.py"
SIMLOG="$(mktemp)"
TMP="$(mktemp -d)"

fail() { echo "FAIL: $1"; echo "--- sim output (tail) ---"; grep -v 'spi. write' "$SIMLOG" | tail -20; exit 1; }
pass() { echo "  ok: $1"; }
api() { curl -s -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL$1"; }

if [ -n "${NODE:-}" ]; then
    export JORM_URL="$NODE"; WHERE="board"
    echo "== target: $JORM_URL (real node)"
    trap 'rm -rf "$TMP"' EXIT
else
    # The clock leases the host-owned display, so the sim node needs a display in its
    # inventory (a bare sim has none). Isolate it in a temp flash via SIM_FS.
    export SIM_FS="$TMP/fs"
    mkdir -p "$SIM_FS"
    cat > "$SIM_FS/settings.json" <<'JSON'
{"token": "dev-token", "port": 8000, "hostname": "jorm-sim",
 "displays": [{"id": "primary", "kind": "max7219", "spi": 1,
               "sck": 25, "mosi": 32, "cs": 33, "n": 4}]}
JSON
    "$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
    SIM=$!
    trap 'kill $SIM 2>/dev/null || true; rm -rf "$TMP"' EXIT
fi

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" >/dev/null && break
    [ "$i" = 50 ] && fail "sim node never answered"
    sleep 0.2
done

echo "== the clock guest installs (display cap survives manifest validation)"
$JORM create "$ROOT/examples/clock" >/dev/null || fail "create clock refused (unknown/rejected cap?)"
pass "installed — display is a known, supported cap"

echo "== it starts and stays running (hal.display past the import guard; panel/config ok)"
$JORM start clock >/dev/null || true
sleep 2
api /api/guests/clock | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d['state'] != 'running':
    print('clock is %s' % d['state'])
    if d.get('traceback'): print(d['traceback'])
    sys.exit(1)
" || fail "clock guest did not stay running (cap/guard/panel/config crash)"
pass "running — hal.display() leased the panel, render loop alive"

echo "== it holds the display focus lease (the host handed it the panel)"
api /api/bus/retained | python3 -c "
import json, sys
d = json.load(sys.stdin)
disp = d.get('\$sys/display', {})
assert disp.get('owner') == 'clock', 'display owner is %r, want clock' % (disp.get('owner'),)
" || fail "clock did not take the display focus lease"
pass "clock owns \$sys/display — the host leased it the panel"

echo "== it declared its panel + config and publishes clock/state"
r="$(api /api/bus/retained)"
echo "$r" | grep -q '"\$ui/clock/panel"' || fail "no \$ui/clock/panel declared"
echo "$r" | grep -q '"clock/state"'      || fail "no clock/state (render loop never published)"
pass "panel, config, and live clock/state on the bus"

echo "== a brightness command reaches it (the deployed control path)"
$JORM pub cmd/clock/brightness 5 >/dev/null
sleep 1
api /api/bus/retained | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('clock/state', {})
assert s.get('brightness') == 5, 'brightness not applied: %r' % s
" || fail "brightness command did not take"
pass "cmd/clock/brightness applied and reflected in clock/state"

echo
echo "CLOCK acceptance ($WHERE): ALL PASS — the clock is a guest, proven off-hardware"
