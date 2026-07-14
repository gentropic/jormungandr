#!/usr/bin/env bash
# Sealed-UDP leaf management acceptance (datagram-leaf, slice 1). A sim node serves the
# sealed-UDP door; tools/leafctl.py drives it over loopback — ping / state / log — and a
# wrong-token datagram is dropped silently. All off hardware: the unix port does real UDP
# sockets (same path cluster.py's discovery beacon already uses).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"          # uv, for the pycryptodome-backed client
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
SIMLOG="$(mktemp)"
LEAFCTL="uv run --quiet --with pycryptodome python $ROOT/tools/leafctl.py"

fail() { echo "FAIL: $1"; echo "--- sim (tail) ---"; grep -v '\[sim\]' "$SIMLOG" | tail -15; exit 1; }
pass() { echo "  ok: $1"; }

"$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true; rm -f "$SIMLOG"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" >/dev/null && break
    [ "$i" = 50 ] && fail "sim node never answered"
    sleep 0.2
done
pass "sim node up (and serving the sealed-UDP management door)"

echo "== ping over a sealed datagram"
$LEAFCTL 127.0.0.1 ping --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('name'), d" || fail "ping did not round-trip"
pass "ping: node identified itself over sealed UDP"

echo "== state (guests + heap + sync)"
$LEAFCTL 127.0.0.1 state --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and 'guests' in d and 'heap_free' in d and 'synced' in d, d" || fail "state incomplete"
pass "state: guests, heap_free and sync over the door"

echo "== log tail"
$LEAFCTL 127.0.0.1 log --token "$JORM_TOKEN" --n 5 | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('log'), d" || fail "log empty"
pass "log: recent node lines returned"

echo "== a wrong-token datagram is dropped (no reply)"
if $LEAFCTL 127.0.0.1 ping --token WRONG-TOKEN --timeout 1.5 >/dev/null 2>&1; then
    fail "a wrong-token datagram got a reply — the seal is not gating"
fi
pass "wrong-token dropped silently (the seal is the door)"

echo
echo "LEAFAPI acceptance (sim): ALL PASS — sealed-UDP management, no HTTP server needed"
