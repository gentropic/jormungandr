#!/usr/bin/env bash
# Datagram bus-bridge acceptance (flagship services, slices 1+2): a leaf's bus and the flagship's
# join over fire-and-forget sealed datagrams, both ways, coalesced — no held-open uplink.
#   outbound: a leaf guest's topic appears on the flagship's bus.
#   inbound:  a command published on the flagship reaches the leaf's guest (round-trips back as an
#             ack). The leaf runs its door on a non-5355 port so the two sim nodes coexist.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FSF="$ROOT/sim/fs-brf"; FSL="$ROOT/sim/fs-brl"
TOK="dev-token"
fail() { echo "FAIL: $1"; echo "-- flagship --"; tail -8 /tmp/brf.log 2>/dev/null;
         echo "-- leaf --"; tail -8 /tmp/brl.log 2>/dev/null; exit 1; }
pass() { echo "  ok: $1"; }
get() { curl -s -H "Authorization: Bearer $TOK" "$1"; }
pub() { curl -s -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
             -X POST "http://127.0.0.1:8000/api/bus/publish" -d "$1" >/dev/null; }
[ -x "$MPY" ] || fail "micropython not found at $MPY"

rm -rf "$FSF" "$FSL"; mkdir -p "$FSF" "$FSL/guests/sensorpub" "$FSL/guests/echo"
printf '{"token":"%s","port":8000,"hostname":"jorm-flag","cluster":"BR"}\n' "$TOK" > "$FSF/settings.json"
# leaf: door ON but on :5356 (so the flagship keeps :5355 on loopback); bridge up + down.
printf '{"token":"%s","hostname":"jorm-leaf","cluster":"BR","role":"leaf-host","mgmt_port":5356,"bridge":{"flagship":"127.0.0.1","port":5355,"up":["sensor/#","ack/#"],"down":["cmd/leaf/#"],"rate_hz":5}}\n' "$TOK" > "$FSL/settings.json"
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
cat > "$FSL/guests/echo/manifest.json" <<'J'
{"spec":0,"id":"echo","name":"echo","version":"0.1.0","runtime":"mpy","entry":"main.py","autostart":true,"caps":{"bus":{"pub":["ack/#"],"sub":["cmd/leaf/#"]}}}
J
cat > "$FSL/guests/echo/main.py" <<'P'
async def run(hal):
    async for topic, msg in hal.bus.subscribe('cmd/leaf/#'):
        hal.bus.publish('ack/%s' % topic.split('/')[-1], {'got': msg})
P
echo 101 > "$FSL/guests/echo/.num"

SIM_FS="$FSF" "$ROOT/sim/run.sh" >/tmp/brf.log 2>&1 & PF=$!
SIM_FS="$FSL" "$ROOT/sim/run.sh" >/tmp/brl.log 2>&1 & PL=$!
trap 'kill $PF $PL 2>/dev/null || true; rm -rf "$FSF" "$FSL"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "flagship never answered"; sleep 0.2
done
pass "flagship + a bridging leaf up"

echo "== outbound: the leaf's guest topic appears on the flagship's bus"
ok=''
for i in $(seq 1 30); do
    get http://127.0.0.1:8000/api/bus/retained | python3 -c "
import json,sys; x=json.load(sys.stdin).get('sensor/temp'); assert x and 'c' in x, x" 2>/dev/null && { ok=1; break; }
    sleep 0.5
done
[ -n "$ok" ] || fail "sensor/temp never reached the flagship"
pass "sensor/temp bridged leaf -> flagship over datagrams"

echo "== inbound: a command on the flagship reaches the leaf's guest (round-trips as an ack)"
ok=''
for i in $(seq 1 30); do
    pub '{"topic":"cmd/leaf/ping","msg":{"v":42}}'      # keep re-publishing until the leaf registers
    sleep 0.5
    get http://127.0.0.1:8000/api/bus/retained | python3 -c "
import json,sys; x=json.load(sys.stdin).get('ack/ping')
assert x and x['got']['v']==42, x" 2>/dev/null && { ok=1; break; }
done
[ -n "$ok" ] || fail "cmd/leaf/ping never reached the leaf's guest (no ack came back)"
pass "cmd pushed flagship -> leaf, guest handled it, ack bridged back -> full round trip"

echo
echo "BUS-BRIDGE acceptance (sim): ALL PASS — leaf<->flagship bus over datagrams, both ways"
