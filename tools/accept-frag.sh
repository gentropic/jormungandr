#!/usr/bin/env bash
# Fragment-retransmit acceptance (SPEC-three §4): the receiver-driven NAK that recovers
# a fragment which ACKed at the radio but was dropped. Runs on the unix port with a
# faked, deliberately-lossy radio — the only way to PROVE the NAK fires, since a real
# link drops fragments when it feels like it, not when a test needs it to.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MPY="${MPY:-$HOME/.local/bin/micropython}"
[ -x "$MPY" ] || { echo "FAIL: micropython not found at $MPY"; exit 1; }
cd "$ROOT"
"$MPY" tools/frag_test.py
