#!/usr/bin/env bash
# Leaf acceptance (SPEC-two slice 1): a leaf node runs no server, connects out to a
# flagship's bus, announces itself, and publishes its sensors there. Sim-only — a
# flagship and a leaf on one host. The C3 measurement (does it fit real silicon)
# lives in the session log; this is the behaviour.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FSF="$ROOT/sim/fs-flag"; FSL="$ROOT/sim/fs-leaf"
TOK="dev-token"
fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "  ok: $1"; }
[ -x "$MPY" ] || fail "micropython not found at $MPY"

rm -rf "$FSF" "$FSL"; mkdir -p "$FSF" "$FSL"
printf '{"token":"%s","port":8000,"hostname":"jorm-flag","cluster":"L"}\n' "$TOK" > "$FSF/settings.json"
# the leaf: role=leaf, connects to the flagship, publishes vitals fast for the test
printf '{"token":"%s","port":8001,"hostname":"leaf-sim","role":"leaf","flagship":"http://127.0.0.1:8000","io":{"sensors":[{"type":"vitals","topic":"leaf-sim/vitals","every_s":1}],"actuators":[{"type":"digital","pin":5,"topic":"cmd/leaf-sim/relay"}]}}\n' "$TOK" > "$FSL/settings.json"
SIM_FS="$FSF" "$ROOT/sim/run.sh" >/tmp/lf.log 2>&1 &
PF=$!
SIM_FS="$FSL" "$ROOT/sim/run.sh" >/tmp/ll.log 2>&1 &
PL=$!
trap 'kill $PF $PL 2>/dev/null || true; rm -rf "$FSF" "$FSL"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "flagship never answered"
    sleep 0.2
done
sleep 5   # let the leaf boot and connect out

echo "== the leaf ran no server of its own"
if curl -sf -m 2 -H "Authorization: Bearer $TOK" http://127.0.0.1:8001/api/node >/dev/null 2>&1; then
    fail "the leaf answered an HTTP request — a leaf must run no IP server"
fi
pass "the leaf serves no API — it is a client, not a server"

echo "== the leaf announced itself on the flagship's bus"
cat > /tmp/leaf-watch.py <<'PYEOF'
import asyncio, sys
sys.path.insert(0, 'supervisor')
from jorm import wsclient
async def main(filt):
    ws = await wsclient.connect('127.0.0.1', 8000, '/api/bus', 'dev-token')
    await ws.send('{"op":"sub","filters":["%s"]}' % filt)
    seen = []
    for _ in range(6):
        try: seen.append(await asyncio.wait_for(ws.recv(), 4))
        except Exception: break
    await ws.close()
    print('\n'.join(seen))
asyncio.run(main(sys.argv[1]))
PYEOF
cd "$ROOT"
"$MPY" /tmp/leaf-watch.py '$sys/leaf/#' | grep -q '"name": "leaf-sim"' \
    || fail "no \$sys/leaf announce on the flagship bus"
pass "the flagship sees \$sys/leaf/leaf-sim — enough to represent it in the tree"

echo "== the leaf's sensor readings land on the flagship's bus"
out=$("$MPY" /tmp/leaf-watch.py 'leaf-sim/#')
echo "$out" | grep -q 'leaf-sim/vitals' \
    || fail "the leaf's vitals never reached the flagship"
echo "$out" | grep -q '"heap"' || fail "the vitals payload is malformed"
pass "leaf-sim/vitals flows onto the flagship's bus — a guest there could react"

echo
echo "LEAF acceptance: ALL PASS — a leaf senses, a flagship hears, no server between"
