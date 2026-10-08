#!/usr/bin/env bash
# Make every NEW linked worktree sparse: blog and (private path) images stay in
# the index as skip-worktree entries instead of on disk (OPS-45,
# (private path)). Policy lives in
# scripts/sparse_worktree.py; this file only finds a Python interpreter and
# dispatches to the checked-out worktree's own copy, so a branch without that
# module is left untouched.
#
# git runs post-checkout after `git worktree add` with the null id as the
# previous HEAD, and after every branch switch with a real one. Only the first
# case in a linked worktree changes anything; the primary checkout never does.
# Every other checkout returns here before any interpreter is needed.
#
# Install by COPY (not symlink) so the hook does not depend on the primary
# clone's branch:
#   just wt-sparse-hook-install
# Revert: rm "$(git rev-parse --git-common-dir)/hooks/post-checkout"
# Opt out for one worktree:   MDT_FULL_WORKTREE=1 git worktree add ...
# Full checkout in an existing worktree:   git sparse-checkout disable
#
# Portable interpreter seam (#5212): MDT_SPARSE_HOOK_PYTHON names one
# interpreter and is used alone when set; otherwise python3, python, then the
# Windows launcher `py -3` are tried, each by its resolved path and each
# checked to really be Python 3.9 or newer (this rejects the Windows Store
# python3 stub and a Python 2 `python`). Builtins only, no other programs.
#
# Fails CLOSED. No usable interpreter exits 3 naming
# SPARSE_HOOK_PYTHON_UNAVAILABLE, so `git worktree add` reports that the new
# worktree was not made sparse instead of a silent full tree. A failed SPARSE
# setup rolls back to a full tree and exits 0 (an efficiency rail, not a
# safety rail). A failure that leaves the worktree in a state nobody asked for
# exits nonzero, so `git worktree add` reports it; the worktree exists.
null_sha1=0000000000000000000000000000000000000000
null_sha256=0000000000000000000000000000000000000000000000000000000000000000
[ "$3" = "1" ] || exit 0
[ "$1" = "$null_sha1" ] || [ "$1" = "$null_sha256" ] || exit 0

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "[ERROR] sparse-worktree: SPARSE_HOOK_NO_WORKTREE: git rev-parse --show-toplevel failed in a fresh worktree; it was not made sparse" >&2
  exit 3
}
[ -f "$root/scripts/sparse_worktree.py" ] || exit 0

python_ok() {
  "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

py_cmd=()
tried=""
if [ -n "${MDT_SPARSE_HOOK_PYTHON:-}" ]; then
  tried=" MDT_SPARSE_HOOK_PYTHON=$MDT_SPARSE_HOOK_PYTHON"
  resolved=$(command -v "$MDT_SPARSE_HOOK_PYTHON" 2>/dev/null) && python_ok "$resolved" && py_cmd=("$resolved")
else
  for name in python3 python py; do
    if [ "$name" = "py" ]; then tried="$tried py -3"; else tried="$tried $name"; fi
    resolved=$(command -v "$name" 2>/dev/null) || continue
    if [ "$name" = "py" ]; then
      python_ok "$resolved" -3 && py_cmd=("$resolved" -3)
    else
      python_ok "$resolved" && py_cmd=("$resolved")
    fi
    [ "${#py_cmd[@]}" -gt 0 ] && break
  done
fi
if [ "${#py_cmd[@]}" -eq 0 ]; then
  echo "[ERROR] sparse-worktree: SPARSE_HOOK_PYTHON_UNAVAILABLE: no Python 3.9+ interpreter (tried:$tried); this new worktree was NOT made sparse. Install Python 3 or set MDT_SPARSE_HOOK_PYTHON, then run: python -m scripts.sparse_worktree sparse" >&2
  exit 3
fi

cd "$root" || {
  echo "[ERROR] sparse-worktree: SPARSE_HOOK_NO_WORKTREE: cannot enter $root; it was not made sparse" >&2
  exit 3
}
"${py_cmd[@]}" -m scripts.sparse_worktree post-checkout "$1" "$2" "$3"
rc=$?
[ "$rc" -ne 0 ] && echo "[ERROR] sparse-worktree hook failed (rc=$rc); check this worktree with: just wt-sparse-status" >&2
exit "$rc"
