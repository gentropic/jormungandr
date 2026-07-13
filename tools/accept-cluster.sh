#!/usr/bin/env bash
# Cluster acceptance (one §1): two nodes discover each other, show each other in
# /api/cluster, and answer cross-origin (CORS). Sim-only — it spawns two nodes on
# one host, which is the whole point. The hop and the one-tree render are verify-ui's
# and the browser's job; this is the server side.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
# Dedicated flashes, not the canonical sim/fs — this suite runs two nodes with
# cluster-specific settings, and must not leave them behind for the next suite.
FS1="$ROOT/sim/fs-cl1"; FS2="$ROOT/sim/fs-cl2"
TOK="dev-token"
fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "  ok: $1"; }

[ -x "$MPY" ] || fail "micropython not found at $MPY (set MPY=...)"

# two flashes: :8000 jorm-cl1 and :8001 jorm-cl2, same cluster, seeding each other
rm -rf "$FS1" "$FS2"; mkdir -p "$FS1" "$FS2"
printf '{"token":"%s","port":8000,"hostname":"jorm-cl1","cluster":"AcceptCluster","peers":["http://127.0.0.1:8001"]}\n' "$TOK" > "$FS1/settings.json"
printf '{"token":"%s","port":8001,"hostname":"jorm-cl2","cluster":"AcceptCluster","peers":["http://127.0.0.1:8000"]}\n' "$TOK" > "$FS2/settings.json"

SIM_FS="$FS1" "$ROOT/sim/run.sh" >/tmp/cl1.log 2>&1 &
P1=$!
SIM_FS="$FS2" "$ROOT/sim/run.sh" >/tmp/cl2.log 2>&1 &
P2=$!
trap 'kill $P1 $P2 2>/dev/null || true; rm -rf "$FS1" "$FS2"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 \
      && curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8001/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "nodes never answered"
    sleep 0.2
done

get() { curl -s -H "Authorization: Bearer $TOK" "$1"; }

echo "== each node names itself, and finds the other"
c1=$(get http://127.0.0.1:8000/api/cluster)
c2=$(get http://127.0.0.1:8001/api/cluster)
echo "$c1" | grep -q '"name": "jorm-cl1"' || fail "node1 self wrong: $c1"
echo "$c1" | grep -q '127.0.0.1:8001' || fail "node1 does not see node2: $c1"
echo "$c2" | grep -q '127.0.0.1:8000' || fail "node2 does not see node1: $c2"
pass "two nodes, one cluster, each sees the other"

echo "== the peer appears once, not twice (seed superseded by beacon)"
n=$(echo "$c2" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["peers"]))')
[ "$n" = "1" ] || fail "node2 shows $n peers, expected 1 (dedup broken)"
pass "seed + beacon dedupe to one peer"

echo "== a foreign cluster is not joined"
FS3="$ROOT/sim/fs-cl3"; rm -rf "$FS3"; mkdir -p "$FS3"
printf '{"token":"%s","port":8002,"hostname":"jorm-other","cluster":"NotOurs"}\n' "$TOK" > "$FS3/settings.json"
SIM_FS="$FS3" "$ROOT/sim/run.sh" >/tmp/cl3.log 2>&1 &
P3=$!
trap 'kill $P1 $P2 $P3 2>/dev/null || true; rm -rf "$FS1" "$FS2" "$FS3"' EXIT
sleep 8
get http://127.0.0.1:8000/api/cluster | grep -q 'jorm-other' && fail "a different cluster was joined"
pass "a beacon from another cluster is ignored"

echo "== CORS: a preflight is answered, so a browser on one node can read another"
h=$(curl -s -i -X OPTIONS -H "Origin: http://127.0.0.1:8000" http://127.0.0.1:8001/api/guests)
echo "$h" | grep -qi 'Access-Control-Allow-Origin: \*' || fail "no CORS allow-origin on preflight"
echo "$h" | grep -qi 'Access-Control-Allow-Headers:.*Authorization' || fail "preflight does not allow Authorization"
pass "preflight returns the cors headers a cross-origin read needs"

echo "== but CORS does not open the door: the token still gates every call"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Origin: http://127.0.0.1:8000" http://127.0.0.1:8001/api/guests)
[ "$code" = "401" ] || fail "cross-origin call without a token got $code, expected 401"
pass "cross-origin without a token is still 401 — the token is the door"

echo
echo "CLUSTER acceptance: ALL PASS — two nodes, one tree's worth of truth"
