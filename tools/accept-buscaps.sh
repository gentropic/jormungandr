#!/usr/bin/env bash
# Bus-cap acceptance: a guest drives real peripherals (i2c, and the new uart) through
# supervisor-owned buses it never imported `machine` to touch (spec §3) — and the pins are
# reserved, so two guests can't fight over one wire. Sim-only: the machine stubs give plausible
# data (a canned BMP280 over i2c, a loopback UART); real transactions are silicon's to prove.
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

rm -rf "$ROOT/sim/fs/guests"
"$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true; rm -f "$SIMLOG"' EXIT

for i in $(seq 1 50); do
    API "$JORM_URL/api/node" >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "sim never answered"
    sleep 0.2
done
pass "sim node up"

echo "== a guest reads a granted i2c device over a supervisor-owned bus"
$JORM create "$ROOT/examples/i2cprobe" >/dev/null || fail "install i2cprobe"
API -X POST "$JORM_URL/api/guests/i2cprobe/start" >/dev/null || fail "start i2cprobe"
ok=''
for i in $(seq 1 20); do
    API "$JORM_URL/api/bus/retained" | python3 -c "
import json,sys; d=json.load(sys.stdin); x=d.get('i2cprobe/reading')
assert x and x['chip']==88, x" 2>/dev/null && { ok=1; break; }
    sleep 0.5
done
[ -n "$ok" ] || fail "i2cprobe did not report the canned chip id (0x58)"
pass "i2c: guest read chip id 0x58 + a live register over the granted address-scoped bus"

echo "== a guest round-trips the new uart cap"
$JORM create "$ROOT/examples/uartecho" >/dev/null || fail "install uartecho"
API -X POST "$JORM_URL/api/guests/uartecho/start" >/dev/null || fail "start uartecho"
ok=''
for i in $(seq 1 20); do
    API "$JORM_URL/api/bus/retained" | python3 -c "
import json,sys; d=json.load(sys.stdin); x=d.get('uartecho/echo')
assert x and x['got'] and x['got']==x['sent'], x" 2>/dev/null && { ok=1; break; }
    sleep 0.5
done
[ -n "$ok" ] || fail "uartecho did not round-trip a line over the uart"
pass "uart: guest wrote a line and read it back over the granted uart (tx -> rx)"

echo "== the pins are reserved: a second guest can't grab uartecho's tx pin"
MAN='{"spec":0,"id":"uartclash","name":"clash","version":"0.1.0","runtime":"mpy","entry":"main.py","caps":{"uart":[{"id":1,"tx":17,"rx":22}]}}'
SRC='async def run(hal):\n    hal.uart(1)\n    while True:\n        await hal.sleep_ms(1000)\n'
API -H "Content-Type: application/json" -X POST "$JORM_URL/api/guests" \
    -d "{\"manifest\": $MAN, \"files\": {\"main.py\": \"$SRC\"}}" >/dev/null || fail "create uartclash"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $JORM_TOKEN" \
    "$JORM_URL/api/guests/uartclash/start")
[ "$code" = "409" ] || fail "conflicting uart start returned $code, expected 409 (pin already claimed)"
pass "pin conflict refused: tx 17 is uartecho's — two guests can't fight over one wire"

echo
echo "BUS-CAPS acceptance (sim): ALL PASS — i2c + the new uart, pins reserved, no machine import in a guest"
