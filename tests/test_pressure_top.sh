#!/usr/bin/env bash
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
uv run --extra dev pytest tests/scripts/test_pressure_gate.py -q
