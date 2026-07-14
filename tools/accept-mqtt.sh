#!/usr/bin/env bash
# Bus<->MQTT bridge acceptance (flagship services, slice 3): a full node bridges its local bus to
# an MQTT broker — telemetry OUT (namespaced dev/jorm/<node>/, the path that auto-bridges to Home
# Assistant), commands IN (cmd/jorm/<node>/, treated as UNTRUSTED and scoped to cmd/). Tested
# against a THROWAWAY local broker, never the live 10.0.10.86. umqtt is real (vendored); the
# broker is a minimal QoS-0 test stand-in.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FS="$ROOT/sim/fs-mqtt"; TOK="dev-token"; BPORT=18831
B="$ROOT/tools/mqtt_test_broker.py"
fail() { echo "FAIL: $1"; echo "-- node --"; tail -8 /tmp/mqn.log 2>/dev/null;
         echo "-- broker --"; tail -8 /tmp/mqb.log 2>/dev/null; exit 1; }
pass() { echo "  ok: $1"; }
ret() { curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/bus/retained; }
[ -x "$MPY" ] || fail "no micropython"

rm -rf "$FS"; mkdir -p "$FS/guests/sensorpub" "$FS/guests/echo"
printf '{"token":"%s","port":8000,"hostname":"jorm-flag","cluster":"MQ","mqtt":{"broker":"127.0.0.1","port":%d,"out":["sensor/#"],"rate_hz":5}}\n' "$TOK" "$BPORT" > "$FS/settings.json"
cat > "$FS/guests/sensorpub/manifest.json" <<'J'
{"spec":0,"id":"sensorpub","name":"p","version":"0.1.0","runtime":"mpy","entry":"main.py","autostart":true,"caps":{"bus":{"pub":["sensor/#"]}}}
J
cat > "$FS/guests/sensorpub/main.py" <<'P'
async def run(hal):
    n = 0
    while True:
        hal.bus.publish('sensor/temp', {'n': n, 'c': 20 + n % 5})
        n += 1
        await hal.sleep_ms(300)
P
echo 100 > "$FS/guests/sensorpub/.num"
cat > "$FS/guests/echo/manifest.json" <<'J'
{"spec":0,"id":"echo","name":"e","version":"0.1.0","runtime":"mpy","entry":"main.py","autostart":true,"caps":{"bus":{"pub":["echo/#"],"sub":["cmd/echo/#"]}}}
J
cat > "$FS/guests/echo/main.py" <<'P'
async def run(hal):
    async for topic, msg in hal.bus.subscribe('cmd/echo/#'):
        hal.bus.publish('echo/last', {'topic': topic, 'got': msg}, retain=True)
P
echo 101 > "$FS/guests/echo/.num"

python3 "$B" "$BPORT" >/tmp/mqb.log 2>&1 & BRK=$!
SIM_FS="$FS" "$ROOT/sim/run.sh" >/tmp/mqn.log 2>&1 & NODE=$!
trap 'kill $BRK $NODE 2>/dev/null || true; rm -rf "$FS"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "node never answered"; sleep 0.2
done
pass "node + throwaway broker up"

echo "== OUT: a guest's bus topic is published to MQTT under dev/jorm/<node>/"
ok=''
for i in $(seq 1 30); do
    grep -q "PUB dev/jorm/jorm-flag/sensor/temp" /tmp/mqb.log && { ok=1; break; }
    sleep 0.5
done
[ -n "$ok" ] || fail "sensor/temp never reached the broker as dev/jorm/jorm-flag/sensor/temp"
pass "sensor/temp -> MQTT dev/jorm/jorm-flag/sensor/temp (this path auto-bridges to HA on the real broker)"

echo "== IN: an MQTT command reaches a guest on the local bus (scoped to cmd/)"
ok=''
for i in $(seq 1 20); do
    python3 "$B" pub "$BPORT" "cmd/jorm/jorm-flag/echo/hi" '{"v":7}'
    sleep 0.5
    ret | python3 -c "
import json,sys; x=json.load(sys.stdin).get('echo/last')
assert x and x['topic']=='cmd/echo/hi' and x['got']['v']==7, x" 2>/dev/null && { ok=1; break; }
done
[ -n "$ok" ] || fail "MQTT cmd never reached the guest as cmd/echo/hi"
pass "MQTT cmd/jorm/<node>/echo/hi -> local cmd/echo/hi -> guest (untrusted input, scoped)"

echo "== IN is scoped: a \$-rooted forge over MQTT does not inject"
python3 "$B" pub "$BPORT" 'cmd/jorm/jorm-flag/$sys/evil' '{"x":1}'
sleep 1
ret | python3 -c "
import json,sys; d=json.load(sys.stdin)
assert not any('evil' in k for k in d), [k for k in d if 'evil' in k]" || fail "a forge injected!"
pass "a \$-rooted MQTT topic is refused injection — the anonymous fleet bus can't forge \$sys"

echo
echo "MQTT acceptance (sim, throwaway broker): ALL PASS — bus<->MQTT, out namespaced, in scoped"
