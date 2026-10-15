#!/usr/bin/env bash
# Development convenience: run the bridge in the foreground (Linux/macOS dev).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3 -m venv .venv; ./.venv/bin/pip install -r requirements-dev.txt; fi
exec ./.venv/bin/python -m apexity_bridge run "$@"
