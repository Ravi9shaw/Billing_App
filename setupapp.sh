#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
case "${OSTYPE:-}" in msys*|cygwin*) MSYS_NO_PATHCONV=1 cmd.exe /c setupapp.bat; exit $?;; esac
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m billing --setup
