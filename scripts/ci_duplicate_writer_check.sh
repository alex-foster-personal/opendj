#!/usr/bin/env bash
# DEVOPS-10 row 14: isolated PyYAML for ops/fleet/state-files.yaml.
#
# Usage: scripts/ci_duplicate_writer_check.sh [--manifest PATH] [--json]
#
# Hosted periodic-checks.yml should call this instead of bare pip install.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
scripts/ci_venv.sh 3.11 --lock ops/fleet/pylock.duplicate-writer.toml
exec .venv/bin/python -m scripts.duplicate_writer_check "$@"
