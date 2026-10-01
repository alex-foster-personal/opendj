#!/usr/bin/env bash
# Make every NEW linked worktree sparse: blog and docs/landscape images stay in
# the index as skip-worktree entries instead of on disk (OPS-45,
# docs/decisions/ADR-NEW-sparse-linked-worktrees.md). Policy lives in
# scripts/sparse_worktree.py; this file only dispatches to the checked-out
# worktree's own copy, so a branch without that module is left untouched.
#
# git runs post-checkout after `git worktree add` with the null id as the
# previous HEAD, and after every branch switch with a real one. Only the first
# case in a linked worktree changes anything; the primary checkout never does.
#
# Install by COPY (not symlink) so the hook does not depend on the primary
# clone's branch:
#   just wt-sparse-hook-install
# Revert: rm "$(git rev-parse --git-common-dir)/hooks/post-checkout"
# Opt out for one worktree:   MDT_FULL_WORKTREE=1 git worktree add ...
# Full checkout in an existing worktree:   git sparse-checkout disable
#
# A failed SPARSE setup rolls back to a full tree and exits 0 (an efficiency
# rail, not a safety rail). A failure that leaves the worktree in a state nobody
# asked for exits nonzero, so `git worktree add` reports it; the worktree exists.
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -f "$root/scripts/sparse_worktree.py" ] || exit 0
command -v python3 >/dev/null 2>&1 || { echo "[WARN] sparse-worktree: no python3 on PATH; worktree left full" >&2; exit 0; }
cd "$root" || exit 0
python3 -m scripts.sparse_worktree post-checkout "$1" "$2" "$3"
rc=$?
[ "$rc" -ne 0 ] && echo "[ERROR] sparse-worktree hook failed (rc=$rc); check this worktree with: just wt-sparse-status" >&2
exit "$rc"
