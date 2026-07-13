#!/usr/bin/env bash
# Sealer acceptance (SPEC-three §6): the app-layer Encrypt-then-MAC that gives ESP-NOW
# encryption without the 6-peer cap. Runs on the unix port — cryptolib + sha256 are
# there, so this needs no radio.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
[ -x "$MPY" ] || { echo "FAIL: micropython not found at $MPY"; exit 1; }
cd "$ROOT"
"$MPY" tools/seal_test.py
