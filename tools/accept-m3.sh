#!/usr/bin/env bash
# M3 acceptance, server side (MILESTONES.md): panels, config, the rest of hal.
# The rendered-panel half lives in tools/verify-ui.mjs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="http://localhost:8000"
export JORM_TOKEN="dev-token"
JORM="python3 $ROOT/cli/jorm.py"
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

echo "== thermo: declared panel + config, retained on \$ui"
$JORM create "$ROOT/examples/thermo" >/dev/null
$JORM start thermo | grep -q running || fail "start thermo"
sleep 1
api /api/bus/retained | grep -q '"\$ui/thermo/panel"' || fail "no retained panel declaration"
api /api/bus/retained | grep -q '"\$ui/thermo/config"' || fail "no retained config schema"
$JORM claims | grep -q "pin 4.*adc.*thermo" || fail "adc claim missing"
pass "panel + config schema retained; adc claimed"

echo "== config: defaults materialize"
$JORM config thermo | grep -Eq "period_ms +1000" || fail "default not materialized"
pass "defaults in the store on first declaration"

echo "== live write streams to hal.config.watch"
$JORM config thermo period_ms=500 | grep -q "applied live: period_ms" || fail "live apply"
sleep 0.5
$JORM console thermo | grep -q "config: period_ms -> 500" || fail "guest never saw the live write"
pass "live field applied while running"

echo "== non-live write goes pending, restart clears it"
$JORM config thermo unit_f=true | grep -q "pending restart: unit_f" || fail "pending"
$JORM config thermo | grep -q "unit_f.*PENDING RESTART" || fail "pending badge"
$JORM restart thermo >/dev/null; sleep 0.5
! $JORM config thermo | grep -q "PENDING RESTART" || fail "restart did not clear pending"
$JORM guests | grep -q "°F" || fail "unit_f did not apply on restart"
pass "pending-restart amber, cleared by restart"

echo "== editable while stopped, like VM options"
$JORM stop thermo >/dev/null
$JORM config thermo period_ms=900 >/dev/null || fail "config write while stopped refused"
$JORM config thermo | grep -Eq "period_ms +900" || fail "stopped write not stored"
pass "config editable while stopped (schema from sidecar)"

echo "== validation fails closed"
if $JORM config thermo period_ms=99999 2>"$TMP/err"; then fail "range violation accepted"; fi
grep -q "within" "$TMP/err" || fail "wrong error: $(cat "$TMP/err")"
if $JORM config thermo nonsense=1 2>"$TMP/err"; then fail "undeclared key accepted"; fi
grep -q "not a declared config key" "$TMP/err" || fail "wrong error: $(cat "$TMP/err")"
pass "schema violations refused with clear errors"

echo "== panels outlive their guests"
api /api/bus/retained | grep -q '"\$ui/thermo/panel"' || fail "panel vanished with the guest"
pass "retained panel persists across stop"

echo "== panel slider command path (set topic, origin: ui)"
$JORM start thermo >/dev/null; sleep 0.5
$JORM pub cmd/thermo/period '{"value": 300, "origin": "ui"}' >/dev/null
sleep 0.5
$JORM console thermo | grep -q "period -> 300 (origin: ui)" || fail "set command never reached the guest"
$JORM retained | grep -q "thermo/period.*300" || fail "state topic not republished"
pass "widget set → guest → state topic round trip"

echo "== storage jail + quota"
mkdir -p "$TMP/scribbler"
cat > "$TMP/scribbler/manifest.json" <<'EOF'
{"spec": 0, "id": "scribbler", "runtime": "mpy", "caps": {"storage": {"quota_kb": 1}}}
EOF
cat > "$TMP/scribbler/main.py" <<'EOF'
async def run(hal):
    with hal.storage.open('notes.txt', 'w') as f:
        f.write('within quota')
    hal.log('wrote notes.txt')
    try:
        hal.storage.open('../../../settings.json')
        hal.log('JAILBREAK')
    except Exception as e:
        hal.log('jail held:', e)
    try:
        with hal.storage.open('big.bin', 'w') as f:
            f.write('x' * 2048)
        hal.log('QUOTA MISSED')
    except OSError as e:
        hal.log('quota held:', e)
EOF
$JORM create "$TMP/scribbler" >/dev/null
$JORM start scribbler >/dev/null; sleep 0.5
$JORM console scribbler | grep -q "wrote notes.txt" || fail "storage write failed"
$JORM console scribbler | grep -q "jail held" || fail "path escaped the jail"
$JORM console scribbler | grep -q "quota held" || fail "quota not enforced"
pass "jailed, quota'd, honest"

echo "== mem_kb refuses what cannot fit"
mkdir -p "$TMP/hog"
printf '{"spec": 0, "id": "hog", "runtime": "mpy", "caps": {"mem_kb": 999999}}\n' > "$TMP/hog/manifest.json"
printf 'async def run(hal):\n    pass\n' > "$TMP/hog/main.py"
$JORM create "$TMP/hog" >/dev/null
if $JORM start hog 2>"$TMP/err"; then fail "hog was allowed to start"; fi
grep -q "exceeds free heap" "$TMP/err" || fail "wrong error: $(cat "$TMP/err")"
pass "declared mem_kb checked against free heap"

echo "== teardown"
$JORM stop thermo >/dev/null
for g in thermo scribbler hog; do $JORM rm "$g" >/dev/null; done
pass "guests removed"

echo
echo "M3 acceptance, server side (sim): ALL PASS — the rendered half is verify-ui's job"
