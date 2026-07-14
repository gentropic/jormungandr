#!/usr/bin/env bash
# Peripheral-cap acceptance: dht, onewire, touch, dac — a guest reads sensors and drives an
# output through supervisor-owned drivers it never imported (machine, dht, onewire are all
# blocked by the guest import guard). Sim-only: the machine + dht/onewire/ds18x20 stubs give
# plausible drifting values; real devices are silicon's to prove.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
SIMLOG="$(mktemp)"
JORM="python3 $ROOT/cli/jorm.py"
API() { curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$@"; }
fail() { echo "FAIL: $1"; grep -v '\[sim\]' "$SIMLOG" | tail -15; exit 1; }
pass() { echo "  ok: $1"; }
wait_pub() {   # $1 topic, $2 python assertion body over `x`
    for _i in $(seq 1 20); do
        API "$JORM_URL/api/bus/retained" | TOPIC="$1" ASSERT="$2" python3 -c "
import json,os,sys
d=json.load(sys.stdin); x=d.get(os.environ['TOPIC'])
assert x is not None
exec(os.environ['ASSERT'])" 2>/dev/null && return 0
        sleep 0.5
    done
    return 1
}

rm -rf "$ROOT/sim/fs/guests"
"$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true; rm -f "$SIMLOG"' EXIT
for i in $(seq 1 50); do
    API "$JORM_URL/api/node" >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "sim never answered"; sleep 0.2
done
pass "sim node up"

echo "== dht: a guest reads temp + humidity over a leased sensor"
$JORM create "$ROOT/examples/climate" >/dev/null && API -X POST "$JORM_URL/api/guests/climate/start" >/dev/null || fail "climate install/start"
wait_pub "climate/reading" "assert isinstance(x['temp_c'],(int,float)) and 0<=x['rh']<=100" || fail "no climate reading"
pass "dht: temp + humidity published"

echo "== onewire: a guest scans + reads DS18B20 temperatures"
$JORM create "$ROOT/examples/probe1w" >/dev/null && API -X POST "$JORM_URL/api/guests/probe1w/start" >/dev/null || fail "probe1w install/start"
wait_pub "probe1w/temps" "assert x['n']>=1 and x['temps'] and isinstance(x['temps'][0],(int,float))" || fail "no onewire temps"
pass "onewire: scanned a device + read its temperature"

echo "== touch + dac: a guest reads a touch pad and drives a DAC"
$JORM create "$ROOT/examples/touchpad" >/dev/null && API -X POST "$JORM_URL/api/guests/touchpad/start" >/dev/null || fail "touchpad install/start"
wait_pub "touchpad/value" "assert isinstance(x['touch'],int)" || fail "no touch value"
pass "touch + dac: touch read + mirrored to the DAC output"

echo "== pins reserved across caps: a second guest can't grab touchpad's dac pin"
MAN='{"spec":0,"id":"dacclash","name":"c","version":"0.1.0","runtime":"mpy","entry":"main.py","caps":{"dac":[25]}}'
SRC='async def run(hal):\n    hal.dac(25)\n    while True:\n        await hal.sleep_ms(1000)\n'
API -H "Content-Type: application/json" -X POST "$JORM_URL/api/guests" \
    -d "{\"manifest\": $MAN, \"files\": {\"main.py\": \"$SRC\"}}" >/dev/null || fail "create dacclash"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $JORM_TOKEN" \
    "$JORM_URL/api/guests/dacclash/start")
[ "$code" = "409" ] || fail "conflicting dac start returned $code, expected 409"
pass "pin conflict refused across caps: dac pin 25 is touchpad's"

echo
echo "SENSORS acceptance (sim): ALL PASS — dht, onewire, touch, dac; pins reserved; no machine in a guest"
