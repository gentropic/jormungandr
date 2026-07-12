#!/usr/bin/env bash
# Run the supervisor as a sim node on MicroPython's unix port (see spec §11).
# The sim "flash" is sim/fs/ — cwd of the process, like / on a real node.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
FS="$ROOT/sim/fs"

if [ ! -x "$MPY" ]; then
    echo "error: micropython not found at $MPY (set MPY=/path/to/micropython)" >&2
    exit 1
fi

if [ ! -f "$FS/settings.json" ]; then
    printf '{"token": "dev-token", "port": 8000}\n' > "$FS/settings.json"
    echo "sim: created $FS/settings.json (token: dev-token, port: 8000)"
fi

# the UI deploys beside main.py on a real node; mirror that into the sim flash
cp "$ROOT/supervisor/ui.html" "$FS/ui.html"

# sim/ first so the machine/network stubs shadow nothing real; .frozen keeps
# the unix port's frozen stdlib (asyncio) reachable once MICROPYPATH is set.
export MICROPYPATH="$ROOT/sim:$ROOT/supervisor:$ROOT/supervisor/lib:.frozen"
cd "$FS"
exec "$MPY" "$ROOT/supervisor/main.py"
