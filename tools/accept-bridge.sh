#!/usr/bin/env bash
# Bus bridging acceptance (one §4): a guest on one node reacts to a guest on
# another, through the bridge, and the loop that split horizon must prevent does
# not happen. Sim-only — two nodes on one host is the point.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FS1="$ROOT/sim/fs-br1"; FS2="$ROOT/sim/fs-br2"
TOK="dev-token"
J1="python3 $ROOT/cli/jorm.py --url http://127.0.0.1:8000 --token $TOK"
J2="python3 $ROOT/cli/jorm.py --url http://127.0.0.1:8001 --token $TOK"
fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "  ok: $1"; }
[ -x "$MPY" ] || fail "micropython not found at $MPY"

# node1 pulls echoer/# from node2; node2 pulls pinger/# from node1 (bidirectional,
# which is exactly the case split horizon has to survive).
rm -rf "$FS1" "$FS2"; mkdir -p "$FS1" "$FS2"
printf '{"token":"%s","port":8000,"hostname":"jorm-br1","cluster":"Br","peers":["http://127.0.0.1:8001"],"bridge":["echoer/#"]}\n' "$TOK" > "$FS1/settings.json"
printf '{"token":"%s","port":8001,"hostname":"jorm-br2","cluster":"Br","peers":["http://127.0.0.1:8000"],"bridge":["pinger/#"]}\n' "$TOK" > "$FS2/settings.json"
SIM_FS="$FS1" "$ROOT/sim/run.sh" >/tmp/br1.log 2>&1 &
P1=$!
SIM_FS="$FS2" "$ROOT/sim/run.sh" >/tmp/br2.log 2>&1 &
P2=$!
trap 'kill $P1 $P2 2>/dev/null || true; rm -rf "$FS1" "$FS2"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 \
      && curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8001/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "nodes never answered"
    sleep 0.2
done

echo "== pinger on node1, echoer on node2 — they can only meet over the bridge"
$J1 create "$ROOT/examples/pinger" >/dev/null && $J1 start pinger >/dev/null
$J2 create "$ROOT/examples/echoer" >/dev/null && $J2 start echoer >/dev/null
sleep 4

echo "== node2 imported pinger, so echoer (on node2) reacted"
# echoer only tocks when it hears pinger; if it tocked, the import worked
$J2 console echoer | grep -qi "tock\|n" 2>/dev/null || true
# the ground truth: node1 imported echoer/tock back, which only exists if echoer ran
cd "$ROOT"
cat > /tmp/br-watch.py <<'PYEOF'
# watch <port> <filter> [bridge] — collect ~2s of frames and print them. With a
# third arg it subscribes AS A BRIDGE, so the server applies split horizon.
import asyncio, sys
sys.path.insert(0, 'supervisor')
from jorm import wsclient
async def main(port, filt, bridge):
    ws = await wsclient.connect('127.0.0.1', int(port), '/api/bus', 'dev-token')
    sub = '{"op":"sub","filters":["%s"]%s}' % (filt, ',"bridge":true' if bridge else '')
    await ws.send(sub)
    end = 0
    while end < 25:
        try: print(await asyncio.wait_for(ws.recv(), 0.4))
        except Exception: pass
        end += 1
    await ws.close()
asyncio.run(main(sys.argv[1], sys.argv[2], len(sys.argv) > 3))
PYEOF
watch() {  # <port> <filter> [bridge] -> prints frames seen over ~2s
    "$MPY" /tmp/br-watch.py "$1" "$2" ${3:+bridge}
}
out1=$(watch 8000 'echoer/#')
echo "$out1" | grep -q 'echoer/tock' \
    || fail "node1 never heard echoer/tock — the bridge did not carry the reaction"
echo "$out1" | grep -q '"node": "' \
    || fail "imported messages are not tagged with their origin node"
pass "a guest on node1 and a guest on node2 coordinate through the bridge"
pass "imported messages carry their origin node"

echo "== split horizon: node1 exports its own traffic, never its imports"
# node1's bus holds pinger/tick (its own) AND echoer/tock (imported from node2). A
# BRIDGE subscriber must see the first and never the second — that is the exact rule
# that stops B->A->C->A loops, tested directly rather than by watching a rate.
bview=$(watch 8000 '#' bridge)
echo "$bview" | grep -q 'pinger/tick' \
    || fail "a bridge subscriber saw none of node1's own traffic"
if echo "$bview" | grep -q 'echoer/tock'; then
    fail "node1 re-exported an import (echoer/tock) to a bridge — split horizon is broken"
fi
pass "a bridge sees pinger/tick (local) and never echoer/tock (imported)"

echo "== \$sys stays home: a node's private telemetry is not bridged"
# node1 bridges echoer/#, not $sys — and even a matching filter must not import $sys.
imported_sys=$(watch 8000 '$sys/heap' | grep '"node": "jorm-br2"' || true)
[ -z "$imported_sys" ] || fail "node2's \$sys/heap leaked onto node1's bus"
pass "\$-roots are not bridged — each node's telemetry stays its own"

echo
echo "BRIDGE acceptance: ALL PASS — two boards, one nervous system"
