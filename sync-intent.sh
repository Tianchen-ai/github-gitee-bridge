#!/usr/bin/env bash
# Run from any directory. Credentials are read by Python, never sourced by a shell.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"
if [[ ! -f .env.intent ]]; then
    printf '%s\n' 'Missing .env.intent. Copy .env.intent.example and fill in the two tokens.' >&2
    exit 1
fi
command -v git >/dev/null || { printf '%s\n' 'Git is required.' >&2; exit 1; }
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import requests, flask, waitress; from bridge.config import Config' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements-bridge.txt
fi
if [[ $# -eq 0 ]]; then
    set -- once
fi
exec .venv/bin/python -m bridge --config examples/intent.toml --env-file .env.intent "$@"
