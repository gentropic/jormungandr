#!/usr/bin/env bash
# Run the supervisor as a sim node on MicroPython's unix port (spec §11.15).
#
# The sim "flash" is sim/fs/ — and it holds the supervisor itself, exactly as a
# real node's flash does. That fidelity is not decoration: OTA rewrites files in
# the flash, so a sim that ran the supervisor from the repo instead would test a
# code path that does not exist on any board.
#
#   sim/run.sh                 sync the supervisor from the repo, then boot
#   SIM_NO_SYNC=1 sim/run.sh   boot whatever is already in the flash (OTA tests)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
# SIM_FS points the flash elsewhere, so a second node (a second port, a second
# hostname) can run on the same host — which is how cluster discovery is tested.
FS="${SIM_FS:-$ROOT/sim/fs}"

if [ ! -x "$MPY" ]; then
    echo "error: micropython not found at $MPY (set MPY=/path/to/micropython)" >&2
    exit 1
fi

mkdir -p "$FS"
if [ ! -f "$FS/settings.json" ]; then
    printf '{"token": "dev-token", "port": 8000, "hostname": "jorm-sim"}\n' > "$FS/settings.json"
    echo "sim: created $FS/settings.json (token: dev-token, port: 8000, as jorm-sim)"
fi

if [ -z "${SIM_NO_SYNC:-}" ]; then
    mkdir -p "$FS/jorm"
    cp "$ROOT/supervisor/main.py" "$ROOT/supervisor/boot.py" "$ROOT/supervisor/ui.html" "$FS/"
    cp "$ROOT"/supervisor/jorm/*.py "$FS/jorm/"
fi
# SIM_SYNC_ONLY: refresh the flash and exit, for harnesses that then run their own
# respawn loop (a node reboots; the sim exits, so something has to restart it).
[ -n "${SIM_SYNC_ONLY:-}" ] && exit 0

# sim/ first so the machine/network stubs shadow the real thing; then the flash
# (where the supervisor lives); then the vendored libs.
export MICROPYPATH="$ROOT/sim:$FS:$ROOT/supervisor/lib:.frozen"
cd "$FS"
# a real node runs boot.py then main.py; the unix port needs to be told
exec "$MPY" -c "exec(open('boot.py').read()); exec(open('main.py').read())"
