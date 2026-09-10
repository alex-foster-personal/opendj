#!/usr/bin/env bash
# Reuse the persistent workspace's .venv when it runs the interpreter
# `uv venv --python <version>` would pick right now; recreate it otherwise.
#
# Usage: scripts/ci_venv.sh <python-version>
#
# Reuse is only half the contract. The caller's install MUST be exact:
#   uv pip install --exact --upgrade --python .venv/bin/python -r <requirements>
# `--exact` removes packages that are not in this job's requirements (another
# branch's dependency, the wheel the contracts job installs), and `--upgrade`
# re-resolves to what a fresh install would pick instead of keeping whatever an
# earlier job left behind. Without both, a reused venv silently tests a
# different environment than a fresh one would. `uv sync` is exact already.
#
# Pinned by tests/scripts/test_ci_workspace_reuse.py.
set -euo pipefail

want="${1:?usage: scripts/ci_venv.sh <python-version>}"
venv=.venv

python_version() { "$1" -c 'import platform; print(platform.python_version())'; }

# `--system` so an existing .venv in the working directory is not the answer.
base="$(uv python find --system "$want")"
want_version="$(python_version "$base")"

if [ ! -e "$venv" ]; then
  reason="missing"
elif ! have_version="$(python_version "$venv/bin/python" 2>&1)"; then
  reason="interpreter does not run: $have_version"
elif [ "$have_version" != "$want_version" ]; then
  reason="python $have_version, want $want_version"
else
  echo "[venv] reusing $venv (python $have_version)"
  exit 0
fi

echo "[venv] recreating $venv ($reason) from $base"
# rm first, not `uv venv --clear`: uv refuses to clear a directory that is not
# a virtualenv, so a half-written .venv (no pyvenv.cfg) would fail the job
# instead of being replaced.
rm -rf -- "$venv"
uv venv --python "$base" "$venv"
