#!/usr/bin/env bash
# M1 acceptance drill (MILESTONES.md) against a sim node — everything except
# the WDT/ungovernable-guest drill, which needs real silicon.
# Run under WSL/Linux with the unix port built (see sim/run.sh).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
JORM="python3 $ROOT/cli/jorm.py"
SIMLOG="$(mktemp)"
TMP="$(mktemp -d)"

fail() { echo "FAIL: $1"; echo "--- sim output ---"; tail -20 "$SIMLOG"; exit 1; }
pass() { echo "  ok: $1"; }

# NODE=<url> runs against a real board; otherwise a fresh sim node is spawned.
if [ -n "${NODE:-}" ]; then
    export JORM_URL="$NODE"
    echo "== target: $JORM_URL (real node)"
    for g in $(python3 "$ROOT/cli/jorm.py" guests | awk 'NR>1 {print $1}'); do
        python3 "$ROOT/cli/jorm.py" stop "$g" >/dev/null 2>&1 || true
        python3 "$ROOT/cli/jorm.py" rm "$g" >/dev/null 2>&1 || true
    done
    trap 'rm -rf "$TMP"' EXIT
else
    rm -rf "$ROOT/sim/fs/guests"   # fresh flash
    "$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
    SIM=$!
    trap 'kill $SIM 2>/dev/null || true; rm -rf "$TMP"' EXIT
fi

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" >/dev/null && break
    [ "$i" = 50 ] && fail "sim node never answered"
    sleep 0.2
done

echo "== jorm node"
$JORM node | grep -q "jorm-c510" || fail "node info"
pass "the node answers"

echo "== create + start blinky"
$JORM create "$ROOT/examples/blinky" | grep -q created || fail "create blinky"
$JORM start blinky | grep -q running || fail "start blinky"
sleep 1.2
if [ -n "${NODE:-}" ]; then
    # On silicon we can't see the LED from here. Verify what software can:
    # the guest is running, it owns the pin, and it has not crashed.
    $JORM guests | grep -q "blinky.*running" || fail "blinky not running"
    $JORM guest blinky | grep -qi "traceback" && fail "blinky crashed"
    pass "blinky runs and holds pin 2 (the photons are yours to confirm)"
else
    grep -q "pin 2 -> 1" "$SIMLOG" || fail "LED not blinking"
    pass "LED blinks (sim pin toggles)"
fi

echo "== claims"
$JORM claims | grep -q "pin 2.*blinky" || fail "claims table"
pass 'pin 2: passed through to guest "blinky"'

echo "== second claimant refused"
mkdir -p "$TMP/blinky2"
sed 's/"blinky"/"blinky2"/; s/"Blinky"/"Blinky 2"/' \
    "$ROOT/examples/blinky/manifest.json" > "$TMP/blinky2/manifest.json"
cp "$ROOT/examples/blinky/main.py" "$TMP/blinky2/"
$JORM create "$TMP/blinky2" >/dev/null
if $JORM start blinky2 2>"$TMP/err"; then fail "blinky2 was allowed to start"; fi
grep -q 'already passed through to guest "blinky"' "$TMP/err" \
    || fail "wrong refusal: $(cat "$TMP/err")"
pass "pin 2 already passed through"

echo "== stop releases the claim"
$JORM stop blinky | grep -q stopped || fail "stop blinky"
$JORM claims | grep -q "no pins claimed" || fail "claim not released"
$JORM start blinky2 | grep -q running || fail "blinky2 could not claim the freed pin"
$JORM stop blinky2 >/dev/null
pass "stop frees pin 2"

echo "== bad manifest refused"
mkdir -p "$TMP/bad-manifest"
printf '{"spec": 0, "id": "bad", "runtime": "mpy", "caps": {"lasers": true}}\n' \
    > "$TMP/bad-manifest/manifest.json"
echo "async def run(hal): pass" > "$TMP/bad-manifest/main.py"
if $JORM create "$TMP/bad-manifest" 2>"$TMP/err"; then fail "bad manifest accepted"; fi
grep -q "unknown cap" "$TMP/err" || fail "wrong error: $(cat "$TMP/err")"
pass "unknown cap refused, clear error"

echo "== crash captures a traceback"
mkdir -p "$TMP/crashy"
printf '{"spec": 0, "id": "crashy", "runtime": "mpy"}\n' > "$TMP/crashy/manifest.json"
cat > "$TMP/crashy/main.py" <<'EOF'
async def run(hal):
    hal.log("about to divide by zero")
    return 1 // 0
EOF
$JORM create "$TMP/crashy" >/dev/null
$JORM start crashy >/dev/null
sleep 0.5
$JORM guests | grep -q "crashy.*crashed" || fail "crashy not in crashed state"
$JORM guest crashy | grep -q "ZeroDivisionError" || fail "no traceback captured"
$JORM console crashy | grep -q "about to divide" || fail "hal.log line missing"
pass "crashed state + traceback + console"

echo "== forbidden import refused"
mkdir -p "$TMP/sneaky"
printf '{"spec": 0, "id": "sneaky", "runtime": "mpy"}\n' > "$TMP/sneaky/manifest.json"
printf 'import machine\nasync def run(hal):\n    pass\n' > "$TMP/sneaky/main.py"
$JORM create "$TMP/sneaky" >/dev/null
$JORM start sneaky >/dev/null 2>&1 || true
$JORM guest sneaky | grep -q "not importable in a guest" || fail "import machine not refused"
pass "import machine refused"

echo "== rm"
for g in blinky blinky2 crashy sneaky; do $JORM rm "$g" >/dev/null; done
$JORM guests | grep -q "no guests" || fail "guests not removed"
pass "guests removed"

echo
echo "M1 acceptance (sim): ALL PASS — the WDT drill still needs real silicon"
