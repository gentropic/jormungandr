#!/usr/bin/env bash
# The shell, from a terminal: `jorm shell` runs geas — the same geas the browser
# runs, with the same VFS and the same builtins. This asserts that the terminal
# front-end is not a second implementation quietly drifting from the first.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export JORM_URL="${NODE:-http://localhost:8000}"
export JORM_TOKEN="${JORM_TOKEN:-dev-token}"
# On Windows, `python3` resolves to a Store stub that exits silently — a shim
# that pretends to be an interpreter is worse than no interpreter. Pick one that
# actually runs.
PY=python3
$PY -c 'pass' 2>/dev/null || PY=python
J="$PY $ROOT/cli/jorm.py"

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "  ok: $1"; }

command -v node >/dev/null || { echo "SKIP: no node — geas is JavaScript"; exit 0; }

echo "== the node's flash, from a terminal"
$J shell -c 'ls /guests' 2>/dev/null | grep -q blinky || fail "ls /guests"
pass "ls /guests"

echo "== a pipe"
n=$($J shell -c 'cat /guests/blinky/main.py | grep -c hal' 2>/dev/null | tr -d '\r')
[ "$n" -ge 2 ] || fail "cat | grep -c gave '$n'"
pass "cat /guests/blinky/main.py | grep -c hal → $n"

echo "== the jorm verbs, in the same shell"
$J shell -c 'guests' 2>/dev/null | grep -q blinky || fail "guests"
pass "guests"

echo "== exit codes reach your shell (this is what makes it scriptable)"
$J shell -c 'test -f /guests/blinky/main.py' 2>/dev/null || fail "test -f on a file that exists said no"
! $J shell -c 'test -f /guests/nope.py' 2>/dev/null || fail "test -f on a missing file said yes"
pass "test -f: 0 when present, 1 when absent"

echo "== colour is for a person, not for a pipe"
$J shell -c 'guests' 2>/dev/null | grep -q $'\033' && fail "escape codes leaked into a pipe"
pass "piped output is text"

echo
echo "SHELL acceptance: ALL PASS — one geas, two front-ends"
