#!/usr/bin/env bash
# Smart-leaf management acceptance (SPEC-two §7): a central node manages a mini
# node's guests over the bus — list, install, start, stop, rm — with no server on
# the leaf. Sim-only: a flagship and a smart leaf on one host.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FSF="$ROOT/sim/fs-lhf"; FSL="$ROOT/sim/fs-lhl"
TOK="dev-token"
J="python3 $ROOT/cli/jorm.py --url http://127.0.0.1:8000 --token $TOK"
fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "  ok: $1"; }
[ -x "$MPY" ] || fail "micropython not found at $MPY"

rm -rf "$FSF" "$FSL"; mkdir -p "$FSF" "$FSL"
printf '{"token":"%s","port":8000,"hostname":"jorm-flag","cluster":"LH"}\n' "$TOK" > "$FSF/settings.json"
printf '{"token":"%s","port":8001,"hostname":"leaf-sim","role":"leaf-host","flagship":"http://127.0.0.1:8000"}\n' "$TOK" > "$FSL/settings.json"
SIM_FS="$FSF" "$ROOT/sim/run.sh" >/tmp/lhf.log 2>&1 &
PF=$!
SIM_FS="$FSL" "$ROOT/sim/run.sh" >/tmp/lhl.log 2>&1 &
PL=$!
trap 'kill $PF $PL 2>/dev/null || true; rm -rf "$FSF" "$FSL"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "flagship never answered"; sleep 0.2
done
sleep 5   # let the leaf boot and uplink

echo "== the leaf runs no server; it is reached only over the bus"
if curl -sf -m 2 -H "Authorization: Bearer $TOK" http://127.0.0.1:8001/api/guests >/dev/null 2>&1; then
    fail "the smart leaf answered HTTP — it must run no IP server"
fi
pass "no server on the leaf"

echo "== install a guest onto the leaf, from the flagship, over the bus"
$J leaf leaf-sim install "$ROOT/examples/blinky" 2>&1 | grep -q 'ok:' || fail "install failed"
sleep 1
$J leaf leaf-sim guests | grep -q 'blinky' || fail "installed guest not in the roster"
pass "install blinky over the bus; it appears in the roster"

echo "== start it on the leaf, from the flagship"
$J leaf leaf-sim start blinky 2>&1 | grep -q 'ok:' || fail "start failed"
sleep 2
$J leaf leaf-sim guests | grep -qE 'blinky *running' || fail "guest not running after start"
pass "start blinky — the leaf runs it, the flagship sees running"

echo "== stop it, from the flagship"
$J leaf leaf-sim stop blinky 2>&1 | grep -q 'ok:' || fail "stop failed"
sleep 2
$J leaf leaf-sim guests | grep -qE 'blinky *stopped' || fail "guest not stopped after stop"
pass "stop blinky — the flagship sees stopped"

echo "== a bad command comes back as an honest error, not a hang"
# the CLI correctly exits non-zero on a failed command (good for scripts), so
# capture rather than pipe — pipefail would read that expected failure as our own
out=$($J leaf leaf-sim start nonesuch 2>&1 || true)
echo "$out" | grep -qi 'failed' || fail "starting a missing guest should fail loudly: $out"
pass "start of a missing guest fails with a clear error"

echo "== rm it, from the flagship"
$J leaf leaf-sim rm blinky 2>&1 | grep -q 'ok:' || fail "rm failed"
sleep 1
$J leaf leaf-sim guests | grep -q 'blinky' && fail "guest still present after rm"
pass "rm blinky — gone from the roster"

echo
echo "LEAF-HOST acceptance: ALL PASS — a mini node's guests, managed from the center"
