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

JORM="python3 $ROOT/cli/jorm.py"
fail() { echo "FAIL: $1"; echo "--- sim (tail) ---"; grep -v '\[sim\]' "$SIMLOG" | tail -15; exit 1; }
pass() { echo "  ok: $1"; }

rm -rf "$ROOT/sim/fs/guests"          # clean slate; the state op should start empty
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

echo "== start / stop / restart a guest over the sealed door"
$JORM create "$ROOT/examples/echoer" >/dev/null || fail "could not install echoer"
$LEAFCTL 127.0.0.1 start echoer --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('state')=='running', d" || fail "start echoer"
pass "start: echoer running"
$LEAFCTL 127.0.0.1 stop echoer --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('state') in ('stopped','unresponsive'), d" || fail "stop echoer"
pass "stop: echoer stopped"
$LEAFCTL 127.0.0.1 restart echoer --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('state')=='running', d" || fail "restart echoer"
pass "restart: echoer back to running"
$LEAFCTL 127.0.0.1 start nosuch --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') is False and 'no such guest' in d.get('err',''), d" || fail "unknown guest not refused"
pass "start of an unknown guest is refused, not crashed"

echo "== a nonce is single-use (replay of a mutating datagram is refused)"
uv run --quiet --with pycryptodome python - "$JORM_TOKEN" "$ROOT" <<'PY' || fail "replay guard"
import socket, json, sys, os
sys.path.insert(0, os.path.join(sys.argv[2], "tools"))
from leafctl import Sealer
seal = Sealer(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
def rpc(req):
    s.sendto(seal.seal(json.dumps(req).encode()), ("127.0.0.1", 5355))
    return json.loads(seal.unseal(s.recvfrom(8192)[0]))
n = rpc({"op": "nonce"})["nonce"]
r1 = rpc({"op": "stop", "guest": "echoer", "nonce": n})       # first use: accepted
assert r1.get("ok"), ("first use should work", r1)
r2 = rpc({"op": "start", "guest": "echoer", "nonce": n})      # replay same nonce: refused
assert r2.get("ok") is False and "nonce" in r2.get("err", ""), ("replay must be refused", r2)
r3 = rpc({"op": "start", "guest": "echoer"})                  # no nonce at all: refused
assert r3.get("ok") is False and "nonce" in r3.get("err", ""), ("no-nonce must be refused", r3)
print("replay-guard ok")
PY
pass "nonce is single-use: replayed / nonce-less mutating datagrams refused"

echo "== upload + install a guest bundle over sealed UDP (chunked put)"
$LEAFCTL 127.0.0.1 install "$ROOT/examples/parrot" --token "$JORM_TOKEN" 2>/dev/null | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('id')=='parrot' and d.get('num'), d" || fail "install parrot over UDP"
pass "install: parrot bundle chunk-uploaded + registered (num assigned)"
$LEAFCTL 127.0.0.1 state --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert any(g['id']=='parrot' for g in d['guests']), d" || fail "parrot not in state after install"
$LEAFCTL 127.0.0.1 start parrot --token "$JORM_TOKEN" | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert d.get('ok') and d.get('state')=='running', d" || fail "start of installed parrot"
pass "the UDP-installed guest runs — the bundle round-tripped intact"

echo "== a wrong-token datagram is dropped (no reply)"
if $LEAFCTL 127.0.0.1 ping --token WRONG-TOKEN --timeout 1.5 >/dev/null 2>&1; then
    fail "a wrong-token datagram got a reply — the seal is not gating"
fi
pass "wrong-token dropped silently (the seal is the door)"

echo
echo "LEAFAPI acceptance (sim): ALL PASS — sealed-UDP management, no HTTP server needed"
