#!/usr/bin/env bash
# Leaf discovery acceptance (beacon). A WiFi leaf-host announces itself on the discovery
# beacon (UDP 5354) carrying its sealed-UDP door, and a flagship on the same LAN lists it
# via /api/leaves with NO seed — the datagram twin of full-node discovery. Sim-only: two
# nodes on one host (real UDP broadcast, the same path accept-cluster uses).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FSF="$ROOT/sim/fs-flag"; FSL="$ROOT/sim/fs-leaf"
TOK="dev-token"
fail() { echo "FAIL: $1"; echo "--- flagship ---"; tail -8 /tmp/ld-flag.log 2>/dev/null;
         echo "--- leaf ---"; tail -8 /tmp/ld-leaf.log 2>/dev/null; exit 1; }
pass() { echo "  ok: $1"; }

[ -x "$MPY" ] || fail "micropython not found at $MPY (set MPY=...)"

rm -rf "$FSF" "$FSL"; mkdir -p "$FSF" "$FSL"
printf '{"token":"%s","port":8000,"hostname":"jorm-flag","cluster":"AcceptLeaf"}\n' "$TOK" > "$FSF/settings.json"
# A leaf-host: no HTTP server, no flagship (the uplink just no-ops), mgmt off so it does not
# fight the flagship for :5355 on loopback. It still announces — that is the thing under test.
printf '{"token":"%s","hostname":"jorm-leaf","cluster":"AcceptLeaf","role":"leaf-host","mgmt":false}\n' "$TOK" > "$FSL/settings.json"

SIM_FS="$FSF" "$ROOT/sim/run.sh" >/tmp/ld-flag.log 2>&1 &
PF=$!
SIM_FS="$FSL" "$ROOT/sim/run.sh" >/tmp/ld-leaf.log 2>&1 &
PL=$!
trap 'kill $PF $PL 2>/dev/null || true; rm -rf "$FSF" "$FSL"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "flagship never answered"
    sleep 0.2
done
pass "flagship up; leaf-host booting"

get() { curl -s -H "Authorization: Bearer $TOK" "$1"; }

echo "== the flagship discovers the leaf over the beacon (no seed)"
L=''
for i in $(seq 1 20); do          # a couple of beacon intervals (BEACON_EVERY = 5 s)
    L=$(get http://127.0.0.1:8000/api/leaves)
    echo "$L" | grep -q '"jorm-leaf"' && break
    sleep 1
done
echo "$L" | grep -q '"jorm-leaf"' || fail "leaf never discovered: $L"
pass "jorm-leaf appears in /api/leaves"

echo "== marked discovered, with a door port, a real host, and its transport"
echo "$L" | python3 -c "
import json,sys
import re
d=[x for x in json.load(sys.stdin) if x['name']=='jorm-leaf'][0]
assert d.get('discovered') is True, d
assert d.get('port')==5355, d
assert isinstance(d.get('host'), str) and re.match(r'^\d+\.\d+\.\d+\.\d+$', d['host']), \
    ('host must be a dotted IP the flagship can dial, got %r' % d.get('host'))
assert d.get('transport')=='wifi', d
print('  leaf:', d)" || fail "discovered leaf shape wrong"
pass "discovered:true, door :5355, host from the packet, transport wifi"

echo "== a leaf that goes quiet ages out (TTL), not pinned forever"
kill $PL 2>/dev/null || true
gone=''
for i in $(seq 1 30); do          # PEER_TTL_MS = 20 s
    get http://127.0.0.1:8000/api/leaves | grep -q '"jorm-leaf"' || { gone=1; break; }
    sleep 1
done
[ -n "$gone" ] || fail "leaf not reaped after it stopped beaconing"
pass "a departed leaf ages out of /api/leaves"

echo
echo "LEAF-DISCOVERY acceptance (sim): ALL PASS — a leaf announces, the flagship lists it, no seed"
