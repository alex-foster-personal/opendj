#!/usr/bin/env bash
# Download the CI workflow's shared production frontend artifact for this commit,
# or build it locally when CI's own "production frontend build" job has not
# reached a state that will land the artifact within budget.
#
# Measured Mon 14 Sep 2026 (docs/decisions/ADR-0028): under a deep queue, 15
# of 17 failed E2E `gate` jobs burned their whole 900s poll budget because
# CI's build job for the SHA had not even started on CI's own pool -- 385
# runner-minutes of vacuous red. This script now reads that job's live state
# via `gh api` and decides:
#   - artifact already published            -> download it (unchanged)
#   - job queued / no CI run yet / budget expired -> build locally at once
#   - job in_progress or completed success   -> keep polling for the artifact
#   - job completed failure/cancelled/timed_out -> refuse, never rebuild over
#     a real CI failure
#   - CI's state cannot be read at all       -> refuse, never guess
#
# Pinned by tests/scripts/test_ci_frontend_build_artifact.py.
set -euo pipefail

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
SHA="${1:?full git sha required}"
DEST="${2:-apps/webui/frontend/build}"
ARTIFACT_NAME="production-frontend-${SHA}"
CI_WORKFLOW_NAME="CI"
CI_JOB_NAME="production frontend build"
MAX_WAIT_S="${MDT_CI_FRONTEND_ARTIFACT_WAIT_S:-900}"
POLL_S="${MDT_CI_FRONTEND_ARTIFACT_POLL_S:-20}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Test seam only: production callers never set this, so it always resolves
# to the real script. Mirrors the existing MDT_CI_FRONTEND_ARTIFACT_*_S
# override convention below.
LOCAL_BUILD_SCRIPT="${MDT_CI_FRONTEND_BUILD_LOCAL_SCRIPT:-${ROOT}/scripts/ci_frontend_build_local.sh}"

if [ -z "${GH_TOKEN:-}" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
  export GH_TOKEN="$GITHUB_TOKEN"
fi

if [ -f "${DEST}/.ci-production-frontend-sha" ]; then
  "${ROOT}/scripts/ci_frontend_build_assert.sh" "$SHA" "$DEST"
  exit 0
fi

# Prints the artifact id on stdout if published and not expired; prints
# nothing when gh cannot answer or nothing is published yet. Absence here is
# not a verdict by itself -- the caller decides what to do next from CI's own
# job state, not from this silence.
_poll_artifact_id() {
  # gh exits 4 when unauthenticated (the fast tier job has no token).
  # That is "not published yet", not a crash: leaking gh's code here skipped
  # the refuse-to-build message (head ed73bf4f7, assert 4 == 1).
  local ids
  ids="$(gh api -H "Accept: application/vnd.github+json" \
      "repos/${REPO}/actions/artifacts?name=${ARTIFACT_NAME}&per_page=5" \
      --jq ".artifacts[] | select(.expired==false and .name==\"${ARTIFACT_NAME}\") | .id" \
      2>/dev/null)" || ids=""
  printf '%s\n' "$ids" | awk 'NF { print; exit }'
}

_download_and_assert() {
  local artifact_id="$1" tmp_zip
  tmp_zip="$(mktemp -t mdt-frontend-artifact.XXXXXX.zip)"
  trap 'rm -f "$tmp_zip"' RETURN
  gh api -H "Accept: application/vnd.github+json" \
    "repos/${REPO}/actions/artifacts/${artifact_id}/zip" >"$tmp_zip"
  mkdir -p "$DEST"
  find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  unzip -qo "$tmp_zip" -d "$DEST"
  "${ROOT}/scripts/ci_frontend_build_assert.sh" "$SHA" "$DEST"
}

# Sets CI_JOB_STATE to one of:
#   waiting     - no CI run for this SHA yet, or the run exists but the build
#                 job has not been created/started yet (queued)
#   running     - the build job is in_progress
#   success     - the build job completed successfully (artifact is uploading)
#   failed      - the build job completed without success; CI_JOB_CONCLUSION
#                 carries the conclusion
#   unreadable  - a `gh api` call failed; never guessed, always a hard stop
CI_JOB_STATE=""
CI_JOB_CONCLUSION=""
_probe_ci_job_state() {
  local run_id job_line status conclusion
  if ! run_id="$(gh api -H "Accept: application/vnd.github+json" \
      "repos/${REPO}/actions/runs?head_sha=${SHA}&per_page=20" \
      --jq ".workflow_runs[] | select(.name==\"${CI_WORKFLOW_NAME}\") | .id" \
      2>/dev/null | awk 'NF { print; exit }')"; then
    CI_JOB_STATE="unreadable"
    return
  fi
  if [ -z "$run_id" ]; then
    CI_JOB_STATE="waiting"
    return
  fi
  if ! job_line="$(gh api -H "Accept: application/vnd.github+json" \
      "repos/${REPO}/actions/runs/${run_id}/jobs" \
      --jq ".jobs[] | select(.name==\"${CI_JOB_NAME}\") | [.status, .conclusion] | @tsv" \
      2>/dev/null)"; then
    CI_JOB_STATE="unreadable"
    return
  fi
  if [ -z "$job_line" ]; then
    CI_JOB_STATE="waiting"
    return
  fi
  IFS=$'\t' read -r status conclusion <<<"$job_line"
  case "$status" in
    queued)
      CI_JOB_STATE="waiting"
      ;;
    in_progress)
      CI_JOB_STATE="running"
      ;;
    completed)
      if [ "$conclusion" = "success" ]; then
        CI_JOB_STATE="success"
      else
        CI_JOB_STATE="failed"
        CI_JOB_CONCLUSION="$conclusion"
      fi
      ;;
    *)
      CI_JOB_STATE="unreadable"
      ;;
  esac
}

_build_locally() {
  local reason="$1"
  echo "::warning::${reason}; building the production frontend locally instead of waiting on CI"
  "$LOCAL_BUILD_SCRIPT" "$SHA" "$DEST"
  "${ROOT}/scripts/ci_frontend_build_assert.sh" "$SHA" "$DEST"
}

deadline=$(( $(date +%s) + MAX_WAIT_S ))
while true; do
  artifact_id="$(_poll_artifact_id)"
  if [ -n "$artifact_id" ]; then
    _download_and_assert "$artifact_id"
    exit 0
  fi

  _probe_ci_job_state

  case "$CI_JOB_STATE" in
    waiting)
      _build_locally "CI's '${CI_JOB_NAME}' job for ${SHA} is queued or has not started"
      exit 0
      ;;
    failed)
      echo "::error::CI's '${CI_JOB_NAME}' job for ${SHA} completed with conclusion '${CI_JOB_CONCLUSION}'; refusing to rebuild over a real CI failure"
      exit 1
      ;;
    unreadable)
      echo "::error::CI state for ${SHA} is unreadable (a gh api query failed); refusing to build locally"
      exit 1
      ;;
    running|success)
      : # the job is on track; keep polling for the shared artifact below
      ;;
    *)
      echo "::error::unexpected CI job state '${CI_JOB_STATE}' for ${SHA}"
      exit 1
      ;;
  esac

  if [ "$(date +%s)" -ge "$deadline" ]; then
    _build_locally "${MAX_WAIT_S}s budget expired waiting on CI's '${CI_JOB_NAME}' job for ${SHA} (last observed state: ${CI_JOB_STATE})"
    exit 0
  fi
  sleep "$POLL_S"
done
