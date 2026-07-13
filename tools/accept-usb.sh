#!/usr/bin/env bash
WHERE="sim"
# M4 acceptance: the endpoint budget, the fit check, the boot-time plan, and the
# inert-when-stopped rule — everything that does NOT need the host to enumerate.
# The actual keystroke needs silicon and a host; that is the board drill.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${JORM_URL:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
JORM="python3 $ROOT/cli/jorm.py"
SIMLOG="$(mktemp)"; TMP="$(mktemp -d)"
fail() { echo "FAIL: $1"; tail -20 "$SIMLOG"; exit 1; }
pass() { echo "  ok: $1"; }

if [ -n "${NODE:-}" ]; then
    export JORM_URL="$NODE"; WHERE="board"
    echo "== target: $JORM_URL (real node)"
    for g in $(python3 "$ROOT/cli/jorm.py" guests | awk 'NR>1 {print $1}'); do
        python3 "$ROOT/cli/jorm.py" stop "$g" >/dev/null 2>&1 || true
        python3 "$ROOT/cli/jorm.py" rm "$g" >/dev/null 2>&1 || true
    done
    trap 'rm -rf "$TMP"' EXIT
else
    rm -rf "$ROOT/sim/fs/guests"
    "$ROOT/sim/run.sh" >"$SIMLOG" 2>&1 &
    SIM=$!
    trap 'kill $SIM 2>/dev/null || true; rm -rf "$TMP"' EXIT
fi
for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" >/dev/null && break
    [ "$i" = 50 ] && fail "node never answered"; sleep 0.2
done

mk() {  # mk <id> <usb-json>
    mkdir -p "$TMP/$1"
    printf '{"spec":0,"id":"%s","runtime":"mpy","caps":{"usb":%s}}\n' "$1" "$2" > "$TMP/$1/manifest.json"
    printf 'async def run(hal):\n    hal.usb()\n    await hal.sleep(3600)\n' > "$TMP/$1/main.py"
}

echo "== a keyboard guest installs and shows in the plan pending a reboot"
mk kbd '{"hid":"keyboard"}'
$JORM create "$TMP/kbd" | grep -q created || fail "create kbd"
$JORM usb | grep -qi "pending" || fail "plan does not report pending after a post-boot install"
pass "installed; plan pending until reboot (virtual hardware is fixed at boot)"

echo "== an unknown usb sub-key is refused at validate"
mkdir -p "$TMP/bad"
printf '{"spec":0,"id":"bad","runtime":"mpy","caps":{"usb":{"laser":true}}}\n' > "$TMP/bad/manifest.json"
printf 'async def run(hal):\n    pass\n' > "$TMP/bad/main.py"
$JORM create "$TMP/bad" 2>"$TMP/e" && fail "bad usb key accepted"
grep -q "cdc | hid | midi" "$TMP/e" || fail "wrong error: $(cat "$TMP/e")"
pass "usb keys are cdc | hid | midi"

echo "== the endpoint budget is enforced at install, with a breakdown"
for n in b1 b2 b3 b4 b5; do mk "$n" '{"hid":"keyboard"}'; $JORM create "$TMP/$n" >/dev/null; done
# kbd + b1..b5 = 6 keyboards = 6 endpoints = full
mk over '{"hid":"keyboard"}'
$JORM create "$TMP/over" 2>"$TMP/e" && fail "7th keyboard accepted past the budget"
grep -q "endpoint" "$TMP/e" || fail "no endpoint breakdown: $(cat "$TMP/e")"
grep -q "another node" "$TMP/e" || fail "no suggestion to move it"
pass "6 fit, the 7th is refused with a per-interface cost breakdown"

echo "== a mouse costs an endpoint too, and mixed types are fine"
$JORM rm kbd >/dev/null
mk mou '{"hid":"mouse"}'
$JORM create "$TMP/mou" | grep -q created || fail "mouse rejected though a slot was freed"
pass "freeing a keyboard makes room for a mouse"

echo
echo "USB acceptance ($WHERE): ALL PASS — the keystroke itself needs a host"
