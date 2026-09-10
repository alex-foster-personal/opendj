#!/usr/bin/env bash
# Reuse the persistent workspace's .venv when it runs the interpreter
# `uv venv --python <version>` would pick right now; recreate it otherwise.
#
# Usage: scripts/ci_venv.sh <python-version> [--sync] [--requirements <file> ...]
#
# With no provisioning flags, behavior is unchanged: create or reuse the venv
# and return. With --sync or --requirements, dependency install runs in the
# same invocation as venv setup so reuse and exact fill stay atomic.
#
#   --sync                 run `uv sync` after venv setup (exact already)
#   --requirements <file>  run `uv pip install --exact --upgrade` for each file
#
# Reuse is only half the contract when the caller provisions separately. The
# install MUST be exact:
#   uv pip install --exact --upgrade --python .venv/bin/python -r <requirements>
# `--exact` removes packages that are not in this job's requirements (another
# branch's dependency, the wheel the contracts job installs), and `--upgrade`
# re-resolves to what a fresh install would pick instead of keeping whatever an
# earlier job left behind. Without both, a reused venv silently tests a
# different environment than a fresh one would. `uv sync` is exact already.
#
# Pinned by tests/scripts/test_ci_workspace_reuse.py.
set -euo pipefail

want=""
do_sync=false
requirements_files=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sync)
      do_sync=true
      shift
      ;;
    --requirements)
      requirements_files+=("${2:?--requirements requires a file path}")
      shift 2
      ;;
    -*)
      echo "usage: scripts/ci_venv.sh <python-version> [--sync] [--requirements <file> ...]" >&2
      exit 2
      ;;
    *)
      if [[ -z "$want" ]]; then
        want="$1"
        shift
      else
        echo "usage: scripts/ci_venv.sh <python-version> [--sync] [--requirements <file> ...]" >&2
        exit 2
      fi
      ;;
  esac
done

[[ -n "$want" ]] || {
  echo "usage: scripts/ci_venv.sh <python-version> [--sync] [--requirements <file> ...]" >&2
  exit 2
}

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

if ! $do_sync && [ "${#requirements_files[@]}" -eq 0 ]; then
  exit 0
fi

if $do_sync; then
  echo "[venv] syncing dependencies"
  uv sync --python "$venv/bin/python"
fi

if [ "${#requirements_files[@]}" -gt 0 ]; then
  for req in "${requirements_files[@]}"; do
    echo "[venv] installing exact requirements from $req"
    uv pip install --exact --upgrade --python "$venv/bin/python" -r "$req"
  done
fi
