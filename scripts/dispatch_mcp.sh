#!/usr/bin/env bash
# Launch the `dispatch` MCP server (apps/fleet_mcp) against THIS checkout.
#
# Why a shim rather than `uv run python -m apps.fleet_mcp` straight in
# .mcp.json: run from a directory with no pyproject.toml of its own -- a
# worktree checked out beneath the primary clone, or any subdirectory -- and
# uv walks UP to the nearest ANCESTOR project and binds ITS .venv (CLAUDE.md,
# "`uv run` does NOT guarantee this worktree's environment"). Resolving the
# repo root from this script's own path and passing --project pins the
# environment to the checkout the script lives in, whatever the caller's cwd.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec uv run --no-sync --project "$repo_root" python -m apps.fleet_mcp "$@"
