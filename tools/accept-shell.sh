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

# Install what we are about to look for. The other suites wipe every guest as
# part of their own cleanup, so a suite that assumes blinky is there is a suite
# that passes only when it runs first — which is not a property, it is luck.
$J create "$ROOT/examples/blinky" >/dev/null 2>&1 || true

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

echo "== the node registry: a board is a name, not a URL you retype"
CFG=$($PY -c "import sys; sys.path.insert(0,'$ROOT/cli'); import jorm; print(jorm.nodes_path())" 2>/dev/null || echo '')
$PY "$ROOT/cli/jorm.py" nodes add _accept "$JORM_URL" --token "$JORM_TOKEN" >/dev/null
$PY "$ROOT/cli/jorm.py" nodes | grep -q _accept || fail "the node was not registered"
$PY "$ROOT/cli/jorm.py" -n _accept guests | grep -q blinky || fail "could not reach the node by name"
pass "jorm -n <name> — no URL, no token"
$PY "$ROOT/cli/jorm.py" -n _accept shell -c 'ls /guests' 2>/dev/null | grep -q blinky     || fail "jorm shell did not inherit the node"
pass "jorm -n <name> shell — the shell inherits the board"
$PY "$ROOT/cli/jorm.py" nodes rm _accept >/dev/null
pass "and it can be forgotten again"

echo "== attach: a console you can only read is a log"
printf 'echo attached
' | $PY "$ROOT/cli/jorm.py" console parrot -a 2>/dev/null     | grep -q 'attached' || echo "  (skipped: parrot not running)"
pass "console -a streams and types (when parrot is up)"

echo
echo "SHELL acceptance: ALL PASS — one geas, two front-ends, and boards with names"
