#!/usr/bin/env bash
# Boot-narration acceptance (two/§8, revised) — slice 3 of the host-owned-display roadmap.
# The host console narrates the boot on the panel (boot -> wifi -> ntp -> up) and owns the
# panel at idle when no guest holds the focus lease. Proven on a sim node with a display
# inventory but no display guest: the narration is observed on $sys/display, which the
# supervisor publishes as the host paints each phase.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
SIMLOG="$(mktemp)"
TMP="$(mktemp -d)"

fail() { echo "FAIL: $1"; echo "--- sim output (tail) ---"; grep -v '\[sim\]' "$SIMLOG" | tail -20; exit 1; }
pass() { echo "  ok: $1"; }
api() { curl -s -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL$1"; }

# A full node (no role) WITH a primary display, and no display guest installed.
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
    [ "$i" = 50 ] && fail "sim node never answered (boot narration crashed the boot?)"
    sleep 0.2
done
pass "node booted with a display and did not choke on the boot narration"

echo "== the host narrated the boot and owns the idle panel"
sleep 1
api /api/bus/retained | python3 -c "
import json, sys
d = json.load(sys.stdin)
disp = d.get('\$sys/display')
assert disp is not None, 'no \$sys/display — the host never narrated'
assert disp.get('owner') == 'host', 'idle owner is %r, want host (no guest installed)' % (disp.get('owner'),)
phase = disp.get('status')
assert phase, 'host owns the panel but drew no status phase'
known = ('boot', 'wifi', 'ntp', 'up', 'no net', 'e-now')
assert phase in known, 'unexpected boot phase %r (not one of %r)' % (phase, known)
print('  (final boot phase on the panel: %r)' % phase)
" || fail "host did not narrate a boot phase on the panel"
pass "host console owns the panel and shows a boot phase (\$sys/display)"

echo
echo "DISPLAY-BOOT acceptance (sim): ALL PASS — the panel narrates the boot instead of sitting dark"
