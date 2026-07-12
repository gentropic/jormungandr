#!/usr/bin/env bash
# M2 acceptance, server side (MILESTONES.md): broker, grants, bounded queues,
# drop counters, retained, $sys telemetry, WS bridge. The UI file is the other
# half of M2 and is tested by eyeball.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="http://localhost:8000"
export JORM_TOKEN="dev-token"
JORM="python3 $ROOT/cli/jorm.py"
CURL="curl -s -H \"Authorization: Bearer $JORM_TOKEN\""
SIMLOG="$(mktemp)"
TMP="$(mktemp -d)"

fail() { echo "FAIL: $1"; echo "--- sim output ---"; tail -20 "$SIMLOG"; exit 1; }
pass() { echo "  ok: $1"; }
api() { curl -s -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL$1"; }

rm -rf "$ROOT/sim/fs/guests"   # fresh flash
"$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true; rm -rf "$TMP"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" >/dev/null && break
    [ "$i" = 50 ] && fail "sim node never answered"
    sleep 0.2
done

echo "== \$sys telemetry over the WS bridge"
timeout 10 $JORM bus -c 2 '$sys/clock/tick' | grep -q 'clock/tick' \
    || fail "no \$sys/clock/tick on the bridge"
pass "the supervisor publishes on its own bus"

echo "== two guests talking, watched live"
$JORM create "$ROOT/examples/pinger" >/dev/null
$JORM create "$ROOT/examples/echoer" >/dev/null
$JORM start pinger >/dev/null
$JORM start echoer >/dev/null
timeout 10 $JORM bus -c 6 'pinger/#' 'echoer/#' > "$TMP/traffic" \
    || fail "bus watch timed out"
grep -q 'pinger/tick' "$TMP/traffic" || fail "no pinger traffic"
grep -q 'echoer/tock' "$TMP/traffic" || fail "no echoer traffic"
pass "pinger and echoer over the bus, mirrored to the WS bridge"

echo "== a slow subscriber drops its own messages and nothing else"
mkdir -p "$TMP/slowpoke"
printf '{"spec": 0, "id": "slowpoke", "runtime": "mpy", "caps": {"bus": {"sub": ["pinger/#"]}}}\n' \
    > "$TMP/slowpoke/manifest.json"
cat > "$TMP/slowpoke/main.py" <<'EOF'
async def run(hal):
    async for topic, msg in hal.bus.subscribe('pinger/#'):
        await hal.sleep_ms(500)
EOF
$JORM create "$TMP/slowpoke" >/dev/null
$JORM start slowpoke >/dev/null
sleep 4
api /api/guests/slowpoke | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['bus']['subs'][0]['drops'] > 0, 'slowpoke dropped nothing'
" || fail "slowpoke shows no drops"
api /api/guests/echoer | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['bus']['subs'][0]['drops'] == 0, 'echoer dropped messages'
assert d['bus']['published'] > 10, 'echoer barely published'
" || fail "echoer was harmed by the slowpoke"
pass "slowpoke drops counted; echoer untouched"

echo "== publishing outside the grant raises, never silently drops"
mkdir -p "$TMP/rogue"
printf '{"spec": 0, "id": "rogue", "runtime": "mpy", "caps": {"bus": {}}}\n' \
    > "$TMP/rogue/manifest.json"
printf 'async def run(hal):\n    hal.bus.publish("intruder/hello", 1)\n' \
    > "$TMP/rogue/main.py"
$JORM create "$TMP/rogue" >/dev/null
$JORM start rogue >/dev/null
sleep 0.5
$JORM guest rogue | grep -q 'outside guest "rogue" pub grant' || fail "rogue not refused"
$JORM guests | grep -q "rogue.*crashed" || fail "rogue not crashed"
pass "grant violation raises in the guest"

echo "== retained messages greet late subscribers"
$JORM pub sensors/last '{"t": 21.5}' --retain >/dev/null
$JORM retained | grep -q 'sensors/last' || fail "retained table missing entry"
timeout 10 $JORM bus -c 1 'sensors/#' | grep -q '21.5' \
    || fail "late subscriber got no retained message"
pass "retained: publish once, greet forever"

echo "== \$sys guest state is retained on the bus"
timeout 10 $JORM bus -c 1 '$sys/guest/pinger/state' | grep -q 'running' \
    || fail "no retained guest state"
pass "\$sys/guest/pinger/state = running"

echo "== teardown"
$JORM stop pinger >/dev/null && $JORM stop echoer >/dev/null && $JORM stop slowpoke >/dev/null
for g in pinger echoer slowpoke rogue; do $JORM rm "$g" >/dev/null; done
pass "guests removed"

echo
echo "M2 acceptance, server side (sim): ALL PASS — the UI file is the other half"
