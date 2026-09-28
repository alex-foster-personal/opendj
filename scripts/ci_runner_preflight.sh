#!/usr/bin/env bash
# ci_runner_preflight.sh -- fail fast, naming the runner and the missing
# executable, before a self-hosted CI job does any real work.
#
# Hosted runners ship a fixed, known-good toolset; a self-hosted box is
# whatever its provisioning left behind, and a workflow that assumes a tool
# is present finds out the hard way, mid-job, with a failure that names the
# SYMPTOM rather than the CAUSE. Issue #1041: CI Cost Guard died `python:
# command not found` (exit 127) on both agentbox runners after #1014 moved
# Linux CI to self-hosted -- Ubuntu 24.04 ships /usr/bin/python3 but not
# /usr/bin/python, and ci-cost-guard.yml / trunk-job-verdict.yml both call
# bare `python` with no actions/setup-python step to provide one.
#
# This is a NAME-AND-FAIL tripwire, not a fix: the fix is provisioning the
# runner (docs/ci-actions-cost-review-2026-08-16.md, "Self-hosted runner
# provisioning"). Do not add a fallback here -- e.g. falling back to python3
# when python is missing masks a missing runner tool instead of surfacing
# it, and defeats the point of this script.
#
# It then runs scripts/ci_stale_install_guard.py with each of `python` and
# `python3` on PATH (setup-python's interpreter when the job has one): a job
# whose interpreter can import music-dj-tools from OUTSIDE the checkout fails
# here, naming the path, instead of silently testing a stale shared-interpreter
# install (agentbox toolcache, Mon 28 Sep 2026; rule recorded in ADR PR #4214).
# Outside a job (no GITHUB_WORKSPACE) the checkout is this script's own repo.
#
# Exit codes:
#   0  -- every named executable resolves on PATH, and no interpreter on PATH
#         can import the project from outside the checkout
#   1  -- an executable is missing, or a stale project install shadows the
#         checkout; prints the runner name and each culprit
#   2  -- usage error (no executables named)
#
# Usage:
#   scripts/ci_runner_preflight.sh <executable> [<executable> ...]
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "[ci-runner-preflight] usage: ci_runner_preflight.sh <executable> [<executable> ...]" >&2
    exit 2
fi

# RUNNER_NAME is a GitHub Actions default env var on every runner (hosted and
# self-hosted); the hostname fallback keeps this script useful outside a job.
readonly RUNNER="${RUNNER_NAME:-$(hostname 2>/dev/null || echo unknown)}"
missing=()

for exe in "$@"; do
    if ! command -v "$exe" >/dev/null 2>&1; then
        missing+=("$exe")
    fi
done

if [ "${#missing[@]}" -gt 0 ]; then
    echo "[ci-runner-preflight] ERROR: runner '$RUNNER' is missing required executable(s): ${missing[*]}" >&2
    echo "[ci-runner-preflight] this runner needs (re)provisioning -- see 'Self-hosted runner provisioning' in docs/ci-actions-cost-review-2026-08-16.md" >&2
    exit 1
fi

echo "[ci-runner-preflight] ok: runner '$RUNNER' has $*"

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly WORKSPACE="${GITHUB_WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
checked=0
shadowed=0
for py in python python3; do
    command -v "$py" >/dev/null 2>&1 || continue
    checked=$((checked + 1))
    "$py" "$SCRIPT_DIR/ci_stale_install_guard.py" --workspace "$WORKSPACE" || shadowed=1
done

if [ "$checked" -eq 0 ]; then
    echo "[ci-runner-preflight] stale-install guard: no python or python3 on PATH, so this job cannot import the project; nothing to check"
fi
if [ "$shadowed" -ne 0 ]; then
    echo "[ci-runner-preflight] ERROR: runner '$RUNNER' has a music-dj-tools install outside the checkout $WORKSPACE (see above)" >&2
    exit 1
fi
