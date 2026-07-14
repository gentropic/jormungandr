#!/usr/bin/env bash
# Deploy the supervisor to a real node (spec §11: plain .py on the board FS;
# freeze-to-firmware is a later luxury).
#
#   JORM_URL=http://10.0.10.74 JORM_TOKEN=... tools/deploy.sh COM14 [settings]
#
# A provisioned node arms the hardware WDT, and an ESP32 WDT cannot be disarmed —
# not even by a soft reset. So you cannot simply Ctrl-C into the REPL and copy
# files: eight seconds later the watchdog reboots you out of it, mid-copy. That
# is the WDT doing its job (§1: the node always comes back reachable), and it is
# fatal to deployment. The way in is to stop the node BEFORE it arms anything:
# ask it over HTTP to reboot into maintenance, where it starts nothing, arms
# nothing, and waits at the REPL.
#
# For a node that is off the network (or has never been provisioned), use the
# serial bootstrap instead — it owns the boot timing:
#
#   .venv/Scripts/python tools/push.py COM14 --settings
set -euo pipefail

PORT="${1:?usage: JORM_URL=... JORM_TOKEN=... deploy.sh <port> [settings]}"
WITH_SETTINGS="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/Scripts/python"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
MP="$PY -m mpremote connect $PORT"

if [ -z "${JORM_URL:-}" ] || [ -z "${JORM_TOKEN:-}" ]; then
    echo "set JORM_URL and JORM_TOKEN (or use tools/push.py for a cold board)" >&2
    exit 1
fi

echo "== asking the node to reboot into maintenance (no WDT, free REPL)"
curl -sf -X POST -H "Authorization: Bearer $JORM_TOKEN" \
    "$JORM_URL/api/node/maintenance" >/dev/null
sleep 5

echo "== dirs"
$MP exec "
import os
for d in ('lib', 'lib/microdot', 'jorm', 'guests'):
    try: os.mkdir(d)
    except OSError: pass
"

echo "== supervisor"
$MP cp "$ROOT/supervisor/main.py" :main.py
for f in "$ROOT"/supervisor/jorm/*.py; do
    $MP cp "$f" ":jorm/$(basename "$f")"
done
for f in "$ROOT"/supervisor/lib/microdot/*.py; do
    $MP cp "$f" ":lib/microdot/$(basename "$f")"
done

echo "== ui (html + a FRESH gz — index() serves ui.html.gz to any gzip client, so a"
echo "   stale .gz left beside a new .html silently serves yesterday's interface)"
$MP cp "$ROOT/supervisor/ui.html" :ui.html
_uigz="${TMPDIR:-/tmp}/jorm-ui.html.gz"
gzip -9 -c "$ROOT/supervisor/ui.html" > "$_uigz"
$MP cp "$_uigz" :ui.html.gz
rm -f "$_uigz"

if [ "$WITH_SETTINGS" = "settings" ]; then
    [ -f "$ROOT/settings.json" ] || { echo "no settings.json — see settings.example.json"; exit 1; }
    echo "== settings.json (secrets)"
    $MP cp "$ROOT/settings.json" :settings.json
fi

echo "== reset into a normal boot"
$MP reset
echo "== deployed."
