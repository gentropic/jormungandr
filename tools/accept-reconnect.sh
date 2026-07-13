#!/usr/bin/env bash
# Reconnection hardening acceptance: a leaf survives its flagship rebooting. Before
# the keepalive, the leaf's uplink recv blocked on the zombie socket forever and only
# a power cycle brought it back. Sim-only: flagship + smart leaf, kill and restart the
# flagship, confirm the leaf re-uplinks on its own.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FSF="$ROOT/sim/fs-rcf"; FSL="$ROOT/sim/fs-rcl"
TOK="dev-token"
fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "  ok: $1"; }
[ -x "$MPY" ] || fail "micropython not found at $MPY"

rm -rf "$FSF" "$FSL"; mkdir -p "$FSF" "$FSL/guests/pinger"
printf '{"token":"%s","port":8000,"hostname":"jorm-flag","cluster":"RC"}\n' "$TOK" > "$FSF/settings.json"
printf '{"token":"%s","port":8001,"hostname":"leaf-rc","role":"leaf-host","flagship":"http://127.0.0.1:8000"}\n' "$TOK" > "$FSL/settings.json"
python3 -c "import json; m=json.load(open('$ROOT/examples/pinger/manifest.json')); m['autostart']=True; json.dump(m, open('$FSL/guests/pinger/manifest.json','w'))"
cp "$ROOT/examples/pinger/main.py" "$FSL/guests/pinger/"; printf '2\n' > "$FSL/guests/pinger/.num"

start_flag() { SIM_FS="$FSF" "$ROOT/sim/run.sh" >/tmp/rcf.log 2>&1 & echo $!; }
FLAG=$(start_flag)
SIM_FS="$FSL" "$ROOT/sim/run.sh" >/tmp/rcl.log 2>&1 &
LEAF=$!
trap 'kill $FLAG $LEAF 2>/dev/null || true; rm -rf "$FSF" "$FSL"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "flagship never answered"; sleep 0.2
done
sleep 6

cat > /tmp/rc-see.py <<'PYEOF'
import asyncio, sys
sys.path.insert(0, 'supervisor')
from jorm import wsclient
async def main():
    try:
        ws = await wsclient.connect('127.0.0.1', 8000, '/api/bus', 'dev-token')
        await ws.send('{"op":"sub","filters":["pinger/#"]}')
        await asyncio.wait_for(ws.recv(), 6)
        await ws.close()
        print('YES')
    except Exception:
        print('NO')
asyncio.run(main())
PYEOF
cd "$ROOT"

echo "== the leaf's guest reaches the flagship"
[ "$("$MPY" /tmp/rc-see.py)" = "YES" ] || fail "pinger never reached the flagship"
pass "pinger (on the leaf) is heard on the flagship"

echo "== the flagship reboots out from under the leaf"
kill "$FLAG"; sleep 3
[ "$("$MPY" /tmp/rc-see.py)" = "NO" ] || fail "flagship still answering after kill?"
FLAG=$(start_flag)
for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    sleep 0.2
done
pass "flagship killed and restarted"

echo "== the leaf notices and re-uplinks on its own — no reboot"
ok=NO
for _ in $(seq 1 12); do   # up to ~24 s: keepalive interval + retry
    if [ "$("$MPY" /tmp/rc-see.py)" = "YES" ]; then ok=YES; break; fi
    sleep 2
done
[ "$ok" = "YES" ] || fail "the leaf never reconnected — the zombie socket is back"
pass "pinger is heard again — the leaf reconnected itself"

echo
echo "RECONNECT acceptance: ALL PASS — a leaf outlives its flagship's reboot"
