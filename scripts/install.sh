#!/usr/bin/env bash
# ENGRAM installer for Linux / macOS (tested on Kali/Debian).  Usage:  bash scripts/install.sh [--no-model]
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.11+ not found (Debian/Kali: sudo apt install python3 python3-venv)"; exit 1; }
echo "Using $PY"

"$PY" -m venv .venv || { echo "venv failed — on Debian/Kali run: sudo apt install python3-venv"; exit 1; }
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[all]"
if [ "${1:-}" != "--no-model" ]; then
  .venv/bin/engram models download
fi
.venv/bin/engram doctor || true
echo
echo "Done. Start ENGRAM with:  .venv/bin/engram serve --open"
