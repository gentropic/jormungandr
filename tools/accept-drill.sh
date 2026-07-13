#!/usr/bin/env bash
# The ungovernable-guest drill (MILESTONES M1/M5) — the one acceptance test that
# software cannot run. It needs a real hardware watchdog on real silicon.
#
#   NODE=http://jorm-c510.local JORM_TOKEN=... tools/accept-drill.sh
#
# A guest that never yields starves the whole event loop: the heartbeat, the
# flagging logic, and the web server with it. Exactly when you would want the UI
# to say "unresponsive", the UI is unreachable. That is the worst case §1 was
# written for, and this is the only test that fires it.
#
# What must be true afterwards:
#   1. the node comes back on its own,
#   2. it names the guilty guest — from the current-guest register in RTC memory,
#      which survives the reset — and disables its autostart,
#   3. and it does NOT blame a bystander that was merely asleep.
#
# (3) is the whole reason the register exists. The original spec blamed the guest
# with the stalest last_yield, which is the *sleeping* one — innocent by
# construction. A watchdog that names the wrong culprit is worse than one that
# names nobody: it benches the innocent and lets the guilty autostart again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${NODE:?set NODE=http://<node> — the sim has no watchdog to fire}"
export JORM_TOKEN="${JORM_TOKEN:?set JORM_TOKEN}"
JORM="python3 $ROOT/cli/jorm.py"

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "  ok: $1"; }

wait_up() {
    for _ in $(seq 1 90); do
        curl -sf -o /dev/null -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" && return 0
        sleep 1
    done
    return 1
}

echo "== the bystander: a guest that sleeps, and must not be blamed for it"
$JORM create "$ROOT/examples/blinky" >/dev/null 2>&1 || true
$JORM start blinky >/dev/null 2>&1 || true
$JORM guests | grep -q "blinky.*running" || fail "the bystander is not running"
pass "blinky is running and sleeping between toggles"

echo "== arming: install the ungovernable guest"
$JORM create "$ROOT/examples/hog" >/dev/null 2>&1 || true
$JORM guests | grep -q "hog" || fail "hog is not installed"
pass "hog installed (while True: pass)"

echo "== starting hog. the node is about to stop answering — that is the test"
# The start request itself may never get a response: the guest begins spinning
# inside the handler. A timeout here is a pass, not a failure.
curl -s -m 6 -X POST -H "Authorization: Bearer $JORM_TOKEN" \
    "$JORM_URL/api/guests/hog/start" >/dev/null 2>&1 || true

echo "== waiting for the node to go dark"
went_dark=0
for _ in $(seq 1 20); do
    if ! curl -sf -m 2 -o /dev/null -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node"; then
        went_dark=1
        break
    fi
    sleep 1
done
[ "$went_dark" = 1 ] || fail "the node never stopped answering — did hog actually run?"
pass "the node is starved and unreachable (heartbeat, flagging, web server, all of it)"

echo "== waiting for the hardware watchdog to reset it, and for it to come back"
wait_up || fail "the node never came back — the watchdog did not save it"
pass "the node came back on its own"

sleep 3
echo "== who does it blame?"
$JORM log -n 40 | grep -qi "watchdog reset" || fail "no watchdog-reset line in the node log"
blamed=$($JORM log -n 40 | grep -i "watchdog reset" | tail -1)
echo "     $blamed"
echo "$blamed" | grep -q '"hog"' || fail "the node blamed the wrong guest"
pass "it names hog — from the RTC register, which survived the reset"

echo "== and the guilty guest is benched"
$JORM guests | grep "hog" | grep -q "suspected" || fail "hog was not badged suspected"
$JORM guest hog | grep -qi "suspected in watchdog reset" || fail "no suspicion badge on hog"
pass "hog: autostart disabled, badged — it does not get to do that twice"

echo "== and the bystander is not blamed"
$JORM guests | grep "blinky" | grep -q "suspected" && fail "the SLEEPING guest was blamed — the register is wrong"
pass "blinky slept through it and was not accused"

echo
echo "THE DRILL: ALL PASS — the node starved, reset, named its culprit, and came back."
