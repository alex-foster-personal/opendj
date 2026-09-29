#!/usr/bin/env bash
# Reuse the persistent workspace's .venv when it runs the interpreter
# `uv venv --python <version>` would pick right now; recreate it otherwise.
#
# Usage: scripts/ci_venv.sh <python-version> [--sync] [--lock <pylock.toml>]
#
# With no provisioning flags, behavior is unchanged: create or reuse the venv
# and return. With --sync or --lock, dependency install runs in the same
# invocation as venv setup so reuse and exact fill stay atomic.
#
#   --sync                run `uv sync` after venv setup (exact already)
#   --lock <pylock.toml>  run `uv pip sync` from that hash-pinned PEP 751 lock
#
# Both installs are EXACT: they remove packages the lock does not name
# (another branch's dependency, the wheel the contracts job installs), so a
# reused venv holds what a fresh fill would. And neither resolves: a pylock
# names every wheel by URL and hash, so with a warm uv cache `--lock` makes
# zero requests to the package index (issue #4252; the `uv pip install
# --exact --upgrade -r` form it replaced revalidated ~150 index pages per job
# and turned trunk red whenever PyPI was slow). Locks are compiled and checked
# by scripts/ci_lock.py.
#
# Pinned by tests/scripts/test_ci_workspace_reuse.py and
# tests/scripts/test_ci_offline_provisioning.py.
set -euo pipefail

want=""
do_sync=false
lock=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sync)
      do_sync=true
      shift
      ;;
    --lock)
      lock="${2:?--lock requires a pylock.toml path}"
      shift 2
      ;;
    -*)
      echo "usage: scripts/ci_venv.sh <python-version> [--sync] [--lock <pylock.toml>]" >&2
      exit 2
      ;;
    *)
      if [[ -z "$want" ]]; then
        want="$1"
        shift
      else
        echo "usage: scripts/ci_venv.sh <python-version> [--sync] [--lock <pylock.toml>]" >&2
        exit 2
      fi
      ;;
  esac
done

[[ -n "$want" ]] || {
  echo "usage: scripts/ci_venv.sh <python-version> [--sync] [--lock <pylock.toml>]" >&2
  exit 2
}

if [ -n "$lock" ] && [ ! -f "$lock" ]; then
  echo "[venv] lock not found: $lock (compile it with: python -m scripts.ci_lock compile)" >&2
  exit 2
fi

venv=.venv

python_version() { "$1" -c 'import platform; print(platform.python_version())'; }

# `--system` so an existing .venv in the working directory is not the answer.
base="$(uv python find --system "$want")"
want_version="$(python_version "$base")"

reused=false
if [ ! -e "$venv" ]; then
  reason="missing"
elif ! have_version="$(python_version "$venv/bin/python" 2>&1)"; then
  reason="interpreter does not run: $have_version"
elif [ "$have_version" != "$want_version" ]; then
  reason="python $have_version, want $want_version"
else
  reused=true
  echo "[venv] reusing $venv (python $have_version)"
fi

if ! $reused; then
  echo "[venv] recreating $venv ($reason) from $base"
  # rm first, not `uv venv --clear`: uv refuses to clear a directory that is not
  # a virtualenv, so a half-written .venv (no pyvenv.cfg) would fail the job
  # instead of being replaced.
  rm -rf -- "$venv"
  uv venv --python "$base" "$venv"
fi

if $do_sync; then
  echo "[venv] syncing dependencies"
  uv sync --python "$venv/bin/python"
fi

if [ -n "$lock" ]; then
  # pylock support is a uv preview feature; opting in names that on purpose.
  lock_sync=(uv pip sync --preview-features pylock --python "$venv/bin/python" "$lock")
  echo "[venv] syncing exactly from $lock (offline, warm cache)"
  if ! "${lock_sync[@]}" --offline; then
    # A cold cache (new runner, bumped pin). The fetch below downloads the files
    # the lock names, verified by hash, and never resolves the lock itself. The
    # one exception is scripts/ci_lock.py SOURCE_BUILDS (no wheel): building
    # them resolves their isolated build dependencies from the index, once per
    # runner, after which the built wheel is cached and the offline sync above
    # covers them. Announced so a runner that keeps missing is visible;
    # UV_OFFLINE=1 makes a miss fail instead.
    echo "::warning title=CI venv cache miss::the uv cache lacks files $lock pins; fetching them by locked URL and hash (a SOURCE_BUILDS package also resolves its build deps; issue #4252)" >&2
    "${lock_sync[@]}"
  fi
fi
