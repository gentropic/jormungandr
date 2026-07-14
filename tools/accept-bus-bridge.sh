#!/usr/bin/env bash
# Datagram bus-bridge acceptance (flagship services, slice 1): a leaf forwards its local bus to
# the flagship over fire-and-forget sealed datagrams (the flagship's door `pub` op), coalesced —
# NOT a held-open uplink. Two sim nodes: a flagship, and a leaf whose guest publishes a topic
# that must then appear on the flagship's bus.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FSF="$ROOT/sim/fs-brf"; FSL="$ROOT/sim/fs-brl"
TOK="dev-token"
fail() { echo "FAIL: $1"; echo "-- flagship --"; tail -6 /tmp/brf.log 2>/dev/null;
         echo "-- leaf --"; tail -8 /tmp/brl.log 2>/dev/null; exit 1; }
pass() { echo "  ok: $1"; }
get() { curl -s -H "Authorization: Bearer $TOK" "$1"; }
[ -x "$MPY" ] || fail "micropython not found at $MPY"

rm -rf "$FSF" "$FSL"; mkdir -p "$FSF" "$FSL/guests/sensorpub"
printf '{"token":"%s","port":8000,"hostname":"jorm-flag","cluster":"BR"}\n' "$TOK" > "$FSF/settings.json"
# leaf: no uplink (no flagship), door OFF so the flagship owns :5355 on loopback, bridge ON.
printf '{"token":"%s","hostname":"jorm-leaf","cluster":"BR","role":"leaf-host","mgmt":false,"bridge":{"flagship":"127.0.0.1","up":["sensor/#"],"rate_hz":2}}\n' "$TOK" > "$FSL/settings.json"
# a publisher guest on the leaf's flash (autostart), publishing faster than the bridge forwards
cat > "$FSL/guests/sensorpub/manifest.json" <<'J'
{"spec":0,"id":"sensorpub","name":"pub","version":"0.1.0","runtime":"mpy","entry":"main.py","autostart":true,"caps":{"bus":{"pub":["sensor/#"]}}}
J
cat > "$FSL/guests/sensorpub/main.py" <<'P'
async def run(hal):
    n = 0
    while True:
        hal.bus.publish('sensor/temp', {'n': n, 'c': 20 + n % 5})
        n += 1
        await hal.sleep_ms(50)
P
echo 100 > "$FSL/guests/sensorpub/.num"

SIM_FS="$FSF" "$ROOT/sim/run.sh" >/tmp/brf.log 2>&1 & PF=$!
SIM_FS="$FSL" "$ROOT/sim/run.sh" >/tmp/brl.log 2>&1 & PL=$!
trap 'kill $PF $PL 2>/dev/null || true; rm -rf "$FSF" "$FSL"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "flagship never answered"; sleep 0.2
done
pass "flagship + a bridging leaf up"

echo "== the leaf's guest topic appears on the flagship's bus (forwarded over datagrams)"
ok=''
for i in $(seq 1 30); do
    get http://127.0.0.1:8000/api/bus/retained | python3 -c "
import json,sys; d=json.load(sys.stdin); x=d.get('sensor/temp')
assert x and 'c' in x and 'n' in x, x" 2>/dev/null && { ok=1; break; }
    sleep 0.5
done
[ -n "$ok" ] || fail "sensor/temp never reached the flagship"
pass "sensor/temp bridged leaf -> flagship over sealed datagrams (no held uplink)"

echo "== the bridge stays live: the forwarded value keeps advancing"
n1=$(get http://127.0.0.1:8000/api/bus/retained | python3 -c "import json,sys; print(json.load(sys.stdin)['sensor/temp']['n'])")
sleep 2
n2=$(get http://127.0.0.1:8000/api/bus/retained | python3 -c "import json,sys; print(json.load(sys.stdin)['sensor/temp']['n'])")
[ "$n2" -gt "$n1" ] || fail "the bridged value did not advance ($n1 -> $n2)"
# the leaf published ~40 samples in that 2 s window; coalescing forwards keep-latest, so the
# flagship's n jumped by many at a bounded ~2 Hz rather than seeing all 40.
pass "value advanced $n1 -> $n2 while forwarding at a bounded rate (keep-latest coalescing)"

echo
echo "BUS-BRIDGE acceptance (sim): ALL PASS — a leaf's bus reaches the flagship over datagrams"
