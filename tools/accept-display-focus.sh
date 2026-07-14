#!/usr/bin/env bash
# Focus/lease acceptance (two/§8, revised) — slice 2 of the host-owned-display roadmap.
# On a sim node that HAS a display inventory, a guest with the `display` cap takes the
# focus lease and draws; stop it and the host reclaims the panel for status. Proven off
# hardware: the sim's machine.SPI (no-op) + the unix framebuf stand in for the panel, and
# the handoff is observed on the bus via the $sys/display topic the supervisor publishes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
JORM="python3 $ROOT/cli/jorm.py"
SIMLOG="$(mktemp)"
TMP="$(mktemp -d)"

fail() { echo "FAIL: $1"; echo "--- sim output (tail) ---"; grep -v '\[sim\]' "$SIMLOG" | tail -20; exit 1; }
pass() { echo "  ok: $1"; }
api() { curl -s -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL$1"; }

# A sim node WITH a primary display in its inventory (the piece a bare sim lacks).
export SIM_FS="$TMP/fs"
mkdir -p "$SIM_FS"
cat > "$SIM_FS/settings.json" <<'JSON'
{"token": "dev-token", "port": 8000, "hostname": "jorm-sim",
 "displays": [{"id": "primary", "kind": "max7219", "spi": 1,
               "sck": 25, "mosi": 32, "cs": 33, "n": 4}]}
JSON

"$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true; rm -rf "$TMP" "$SIMLOG"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" >/dev/null && break
    [ "$i" = 50 ] && fail "sim node never answered (display init crash?)"
    sleep 0.2
done
pass "sim node booted with a primary display in its inventory"

echo "== a guest with the display cap installs and starts"
$JORM create "$ROOT/examples/paneltest" >/dev/null || fail "create paneltest refused (display cap rejected?)"
$JORM start paneltest >/dev/null || fail "start paneltest refused"
sleep 1.5

echo "== it holds the focus lease and is drawing"
api /api/bus/retained | python3 -c "
import json, sys
d = json.load(sys.stdin)
disp = d.get('\$sys/display')
assert disp is not None, 'no \$sys/display published'
assert disp.get('owner') == 'paneltest', 'focus owner is %r, want paneltest' % (disp.get('owner'),)
st = d.get('paneltest/state', {})
assert st.get('drawing') is True, 'guest never reported drawing: %r' % st
" || fail "guest did not take the display focus"
pass "guest owns \$sys/display and reports drawing — the lease is held"

echo "== stopping the guest returns the panel to the host console"
$JORM stop paneltest >/dev/null || fail "stop paneltest refused"
sleep 1
api /api/bus/retained | python3 -c "
import json, sys
d = json.load(sys.stdin)
disp = d.get('\$sys/display')
assert disp.get('owner') == 'host', 'after stop owner is %r, want host' % (disp.get('owner'),)
assert disp.get('status'), 'host reclaimed but drew no status: %r' % disp
" || fail "host did not reclaim the panel on guest stop"
pass "host reclaimed the panel and drew status (a stopped guest no longer owns the glass)"

echo
echo "DISPLAY-FOCUS acceptance (sim): ALL PASS — host leases the panel, guest draws, host reclaims"
