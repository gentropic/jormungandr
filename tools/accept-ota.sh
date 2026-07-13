#!/usr/bin/env bash
# Supervisor OTA acceptance: apply, confirm — and the one that matters, revert.
# A rollback path you have not fired is a rollback path you do not have.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${NODE:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
JORM="python3 $ROOT/cli/jorm.py"
SIMLOG="$(mktemp)"
TMP="$(mktemp -d)"

fail() { echo "FAIL: $1"; echo "--- node output ---"; tail -25 "$SIMLOG"; exit 1; }
pass() { echo "  ok: $1"; }
api() { curl -s -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL$1"; }
put() { curl -s -X PUT -H "Authorization: Bearer $JORM_TOKEN" --data-binary @"$2" "$JORM_URL$1"; }
post() { curl -s -X POST -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL$1"; }

wait_up() {
    for _ in $(seq 1 60); do
        curl -sf -o /dev/null -H "Authorization: Bearer $JORM_TOKEN" "$JORM_URL/api/node" && return 0
        sleep 1
    done
    return 1
}

if [ -z "${NODE:-}" ]; then
    rm -rf "$ROOT/sim/fs/guests" "$ROOT/sim/fs/staged" "$ROOT/sim/fs/backup"
    rm -f "$ROOT/sim/fs/.trial" "$ROOT/sim/fs/.update" "$ROOT/sim/fs/.rolled-back"
    SIM_SYNC_ONLY=1 "$ROOT/sim/run.sh"     # put a fresh supervisor in the flash
    # A reboot on the sim is the process exiting, so something has to restart it —
    # that is what silicon does for free. NO_SYNC, or each respawn would copy the
    # repo back over the very files the OTA just wrote, and we'd test nothing.
    # `|| true` matters: a crashing supervisor exits nonzero, and set -e would
    # kill the respawn loop at exactly the moment we are testing for — leaving a
    # dead node and a test that blames the rollback for the harness's own failure.
    ( while true; do SIM_NO_SYNC=1 "$ROOT/sim/run.sh" >>"$SIMLOG" 2>&1 || true; sleep 1; done ) &
    SUP=$!
    trap 'kill $SUP 2>/dev/null; pkill -f "micropython.*boot.py" 2>/dev/null; rm -rf "$TMP"' EXIT
fi
wait_up || fail "node never answered"

echo "== a good update applies and confirms"
cp "$ROOT/supervisor/jorm/__init__.py" "$TMP/init.orig"
printf "VERSION = '0.0.2-ota'\nSPEC = 0\n" > "$TMP/init.new"
put /api/node/files/jorm/__init__.py "$TMP/init.new" | grep -q staged || fail "stage"
api /api/node/update | grep -q "jorm/__init__.py" || fail "not staged"
post /api/node/update >/dev/null
sleep 3
wait_up || fail "node did not come back from a good update"
api /api/node | grep -q "0.0.2-ota" || fail "the update did not take"
pass "staged, applied, node came back with the new version"

echo "== and it confirms itself by being healthy"
for _ in $(seq 1 30); do
    [ "$(api /api/node/update | python3 -c 'import sys,json; print(json.load(sys.stdin)["trial"])')" = "None" ] && break
    sleep 1
done
api /api/node/update | grep -q '"trial": null' || fail "trial never confirmed"
pass "trial confirmed — the node came back, so the update stands"

echo "== a BROKEN update reverts itself"
printf "VERSION = '0.0.3-broken'\nSPEC = 0\nraise RuntimeError('this supervisor cannot boot')\n" > "$TMP/init.bad"
put /api/node/files/jorm/__init__.py "$TMP/init.bad" >/dev/null
post /api/node/update >/dev/null
sleep 3
wait_up || fail "node never recovered from a broken update"
version=$(api /api/node | python3 -c 'import sys,json; print(json.load(sys.stdin)["version"])')
[ "$version" = "0.0.2-ota" ] || fail "expected the previous supervisor back, got $version"
api /api/node/update | grep -q "did not come back" || fail "no rolled-back marker"
pass "broken supervisor shipped → node reverted itself → back on the air ($version)"

echo "== restore"
put /api/node/files/jorm/__init__.py "$TMP/init.orig" >/dev/null
post /api/node/update >/dev/null
sleep 3
wait_up || fail "node did not come back from the restore"
pass "original supervisor restored"

echo
echo "OTA acceptance: ALL PASS — including a supervisor that could not boot"
