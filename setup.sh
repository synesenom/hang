#!/usr/bin/env bash
#
# ./setup.sh — bootstraps hang: Homebrew check, Python, Poetry and the project's
# Python environment (.venv). Then `./hang` installs the speech tools and
# models with progress bars. Safe to run again.
#
# You rarely need this directly: ./hang runs it the first time.
#
set -euo pipefail
cd "$(dirname "$0")"
LOG="${TMPDIR:-/tmp/}"; LOG="${LOG%/}/hang-setup.log"
: > "$LOG"

bold=$'\033[1m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; grey=$'\033[90m'; off=$'\033[0m'
ok()   { printf '\r  %s✓%s %s\033[K\n' "$green" "$off" "$1"; }
step() { printf '  %s…%s %s' "$yellow" "$off" "$1"; }
fail() { printf '\r  %s✗%s %s\033[K\n' "$red" "$off" "$1"; [ -s "$LOG" ] && tail -n 15 "$LOG" | sed 's/^/      /'; exit 1; }
run()  { "$@" >>"$LOG" 2>&1; }

printf '%sBootstrapping%s %s(log: %s)%s\n' "$bold" "$off" "$grey" "$LOG" "$off"

[ "$(uname)" = "Darwin" ] || fail "hang runs on macOS only"
command -v brew >/dev/null || fail "Homebrew is required: install it from https://brew.sh, then run ./hang again"
ok "Homebrew"

PY=""
for v in 3.12 3.13 3.11 3.10; do
  if command -v "python$v" >/dev/null; then PY="$(command -v "python$v")"; break; fi
done
if [ -z "$PY" ]; then
  step "Installing Python 3.12 with Homebrew"
  run brew install python@3.12 || fail "brew install python@3.12 failed"
  PY="$(brew --prefix)/bin/python3.12"
fi
ok "Python $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

if ! command -v poetry >/dev/null; then
  step "Installing Poetry with Homebrew"
  run brew install poetry || fail "brew install poetry failed"
fi
ok "Poetry $(poetry --version 2>/dev/null | sed 's/.*version //; s/)//')"

step "Installing Python packages (sherpa-onnx, numpy, click, rich) into .venv"
run poetry env use "$PY" || fail "poetry could not create the environment"
run poetry install --no-root || fail "poetry install failed"
ok "Python packages"
chmod +x hang

if [ -z "${HANG_BOOTSTRAP_ONLY:-}" ]; then
  exec ./hang
fi
