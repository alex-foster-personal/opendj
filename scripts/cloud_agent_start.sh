#!/usr/bin/env bash
# Boot the web UI for a Cursor cloud agent. install (scripts/cloud_agent_setup.sh)
# has already created the venv and frontend deps. This script only starts
# processes, and it is safe to run again.
#
# Linux refuses an unset MDT_LIBRARY_MODE (unset is local only on darwin).
# A cloud agent has no remote crate, so local is the dev default. It is passed
# to the launched processes, never written into the worktree .env, which holds
# only the two port keys. run_local hands it to each tmux window with `-e`,
# because a long-lived tmux server does not copy the client environment into
# a new session.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

export MDT_LIBRARY_MODE="${MDT_LIBRARY_MODE:-local}"

exec just run
