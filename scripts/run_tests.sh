#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || { python3 -m venv .venv; ./.venv/bin/pip install -r requirements-dev.txt; }
./.venv/bin/python -m pytest tests/ "$@"
