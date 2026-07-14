#!/usr/bin/env bash
# Build a jormungandr ESP32 firmware with the supervisor frozen in (bytecode runs from
# flash, off the GC heap). Run in WSL:  bash firmware/build.sh [BOARD]
#
# Needs: ESP-IDF at $IDF_PATH (default ~/esp/esp-idf) and the MicroPython tree at
# $MPY_DIR (default ~/mpy-build). The frozen source is read straight from this repo.
set -euo pipefail

BOARD="${1:-ESP32_GENERIC}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_SRC="$REPO/supervisor"                 # read by firmware/manifest.py
export IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
MPY_DIR="${MPY_DIR:-$HOME/mpy-build}"

echo "[build] board=$BOARD  jorm=$JORM_SRC  idf=$IDF_PATH"
# IDF's Python env — built with 3.12 because the system python3 (3.14) is too new for
# IDF 5.5's tooling. Activate it so export.sh finds the matching venv.
IDF_VENV="${IDF_VENV:-$HOME/.espressif/python_env/idf5.5_py3.12_env/bin/activate}"
# shellcheck disable=SC1090
[ -f "$IDF_VENV" ] && source "$IDF_VENV"
# shellcheck disable=SC1091
source "$IDF_PATH/export.sh" >/dev/null
make -C "$MPY_DIR/mpy-cross" >/dev/null           # frozen modules need mpy-cross

cd "$MPY_DIR/ports/esp32"
make BOARD="$BOARD" FROZEN_MANIFEST="$REPO/firmware/manifest.py"

BUILD="$MPY_DIR/ports/esp32/build-$BOARD"
echo "[build] done -> $BUILD/firmware.bin"
