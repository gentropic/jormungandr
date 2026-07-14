#!/usr/bin/env bash
# Flagship-as-leaf-console acceptance (UDP slice 1). A full node queries and drives a leaf
# over the sealed-UDP door through its own HTTP /api/leaves* — the browser's path to a leaf
# it has no HTTP route to. Self-contained: the sim node points its leaf-list at its OWN door
# (127.0.0.1:5355), so the whole leafclient -> seal -> UDP -> leafapi -> reply loop runs in one
# process, off hardware (the unix port does real UDP, like the discovery beacon already does).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
SIMLOG="$(mktemp)"
SETTINGS="$ROOT/sim/fs/settings.json"
SETTINGS_BAK="$(mktemp)"
JORM="python3 $ROOT/cli/jorm.py"
API() { curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$@"; }
fail() { echo "FAIL: $1"; echo "--- sim (tail) ---"; grep -v '\[sim\]' "$SIMLOG" | tail -15; exit 1; }
pass() { echo "  ok: $1"; }

rm -rf "$ROOT/sim/fs/guests"          # clean slate; state should start empty
cp "$SETTINGS" "$SETTINGS_BAK"
# Point the flagship's leaf-list at its own door (loopback): the "leaf" is this node, so the
# transport is exercised end to end without a second board. run.sh won't clobber an existing
# settings.json, so this survives the supervisor sync.
python3 - "$SETTINGS" <<'PY'
import json, sys
p = sys.argv[1]
s = json.load(open(p))
s['leaves'] = [{'name': 'self', 'host': '127.0.0.1', 'port': 5355}]
json.dump(s, open(p, 'w'))
PY

"$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true; cp "$SETTINGS_BAK" "$SETTINGS"; rm -f "$SIMLOG" "$SETTINGS_BAK"' EXIT

for i in $(seq 1 50); do
    API "$JORM_URL/api/node" >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "sim node never answered"
    sleep 0.2
done
pass "sim flagship up (leaf-list points at its own sealed door)"

echo "== /api/leaves lists the configured leaf"
API "$JORM_URL/api/leaves" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert any(L['name']=='self' and L['port']==5355 for L in d), d" || fail "leaf not listed"
pass "leaf 'self' listed from settings"

echo "== /api/leaves/self/state queries the leaf over the sealed door"
API "$JORM_URL/api/leaves/self/state" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('online') and 'guests' in d and 'heap_free' in d and 'synced' in d, d" \
    || fail "leaf state not returned over the door"
pass "leaf state (guests, heap_free, synced) fetched over UDP through HTTP"

echo "== install a guest, then drive it on the leaf via the console"
$JORM create "$ROOT/examples/echoer" >/dev/null || fail "could not install echoer"
API -X POST "$JORM_URL/api/leaves/self/guests/echoer/start" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('state')=='running', d" || fail "start over leaf console"
pass "start: echoer running (driven over the sealed door, with a nonce)"
API -X POST "$JORM_URL/api/leaves/self/guests/echoer/stop" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('state') in ('stopped','unresponsive'), d" || fail "stop over leaf console"
pass "stop: echoer stopped"
API -X POST "$JORM_URL/api/leaves/self/guests/echoer/restart" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('state')=='running', d" || fail "restart over leaf console"
pass "restart: echoer back to running"

echo "== /api/leaves/self/log tails the leaf's log over the door"
API "$JORM_URL/api/leaves/self/log?n=5" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('log'), d" || fail "leaf log empty"
pass "leaf log tailed over the door"

echo "== an unknown leaf is a clean 404, not a hang"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JORM_TOKEN" \
       "$JORM_URL/api/leaves/nosuch/state")
[ "$code" = "404" ] || fail "unknown leaf returned $code, expected 404"
pass "unknown leaf -> 404"

echo "== a bad guest action is refused, not proxied blindly"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $JORM_TOKEN" \
       "$JORM_URL/api/leaves/self/guests/echoer/frobnicate")
[ "$code" = "400" ] || fail "bad action returned $code, expected 400"
pass "unknown action -> 400"

echo
echo "LEAF-CONSOLE acceptance (sim): ALL PASS — flagship queries + drives a leaf over the sealed door"
