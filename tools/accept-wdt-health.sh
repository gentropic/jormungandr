#!/usr/bin/env bash
# WDT health-gating acceptance (the serving self-probe). The heartbeat feeds the hardware WDT
# only while the HTTP server actually answers a loopback GET / — not merely while the event
# loop cycles (the c510 wedge: loop alive, server dead, watchdog fed forever). Two sim nodes:
# one healthy (probe hits its real server → never withholds), one whose probe points at a dead
# port (every probe fails → withholds after HEALTH_FAIL_MAX). The reboot itself is a WDT event
# only real silicon can prove; the sim proves the DECISION — feed follows serving-liveness.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
TOK="dev-token"
FS_OK="$ROOT/sim/fs-h-ok"; FS_BAD="$ROOT/sim/fs-h-bad"
fail() { echo "FAIL: $1"; echo "--- ok ---"; tail -6 /tmp/h-ok.log 2>/dev/null;
         echo "--- bad ---"; tail -6 /tmp/h-bad.log 2>/dev/null; exit 1; }
pass() { echo "  ok: $1"; }

[ -x "$MPY" ] || fail "micropython not found at $MPY"

rm -rf "$FS_OK" "$FS_BAD"; mkdir -p "$FS_OK" "$FS_BAD"
# healthy: fast probe cadence, probe the real API port (default).
printf '{"token":"%s","port":8000,"hostname":"jorm-hok","wdt_probe_s":1}\n' "$TOK" > "$FS_OK/settings.json"
# wedged: fast cadence, probe a dead port (nothing listens on 9) → every probe fails, even
# though its own server is fine. Exercises the fail-count -> withhold decision.
printf '{"token":"%s","port":8001,"hostname":"jorm-hbad","wdt_probe_s":1,"wdt_probe_port":9}\n' "$TOK" > "$FS_BAD/settings.json"

SIM_FS="$FS_OK" "$ROOT/sim/run.sh" >/tmp/h-ok.log 2>&1 & POK=$!
SIM_FS="$FS_BAD" "$ROOT/sim/run.sh" >/tmp/h-bad.log 2>&1 & PBAD=$!
trap 'kill $POK $PBAD 2>/dev/null || true; rm -rf "$FS_OK" "$FS_BAD"' EXIT

for i in $(seq 1 50); do
    curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 && break
    [ "$i" = 50 ] && fail "healthy sim never answered"
    sleep 0.2
done
pass "both sims booted (health probe at 1 s cadence)"

echo "== the healthy node probes its own server and never withholds the feed"
sleep 10          # ~10 probes at 1 s
curl -sf -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/node >/dev/null 2>&1 \
    || fail "healthy node stopped serving"
grep -q "withholding the watchdog feed" /tmp/h-ok.log \
    && fail "healthy node wrongly withheld the feed (a false positive would reboot-loop a good node!)"
pass "healthy node: ~10 probes, still serving, feed never withheld — no false positive"

echo "== the wedged node (probe hits a dead port) withholds after HEALTH_FAIL_MAX"
for i in $(seq 1 20); do
    grep -q "withholding the watchdog feed" /tmp/h-bad.log && break
    sleep 1
done
grep -q "withholding the watchdog feed" /tmp/h-bad.log || fail "wedged node never withheld the feed"
pass "wedged node: probes fail -> feed withheld (on silicon the WDT then reboots it)"

echo
echo "WDT-HEALTH acceptance (sim): ALL PASS — the feed follows serving-liveness, not loop-liveness"
