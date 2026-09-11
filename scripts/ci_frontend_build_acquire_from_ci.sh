#!/usr/bin/env bash
# Download the CI workflow's shared production frontend artifact for this commit.
#
# Polls until the CI workflow uploads production-frontend-<sha> or the wait budget
# expires. Never runs pnpm build locally.
#
# Pinned by tests/scripts/test_ci_frontend_build_artifact.py.
set -euo pipefail

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
SHA="${1:?full git sha required}"
DEST="${2:-apps/webui/frontend/build}"
ARTIFACT_NAME="production-frontend-${SHA}"
MAX_WAIT_S="${MDT_CI_FRONTEND_ARTIFACT_WAIT_S:-900}"
POLL_S="${MDT_CI_FRONTEND_ARTIFACT_POLL_S:-20}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "${DEST}/.ci-production-frontend-sha" ]; then
  "${ROOT}/scripts/ci_frontend_build_assert.sh" "$SHA" "$DEST"
  exit 0
fi

deadline=$(( $(date +%s) + MAX_WAIT_S ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  run_id="$(
    gh api -H "Accept: application/vnd.github+json" \
      "repos/${REPO}/actions/workflows/ci.yml/runs?head_sha=${SHA}&status=completed&per_page=10" \
      --jq '.workflow_runs[] | select(.conclusion=="success") | .id' | head -n1
  )"
  if [ -n "$run_id" ]; then
    art_id="$(
      gh api "repos/${REPO}/actions/runs/${run_id}/artifacts" \
        --jq ".artifacts[] | select(.name==\"${ARTIFACT_NAME}\") | .id" | head -n1
    )"
    if [ -n "$art_id" ]; then
      mkdir -p "$DEST"
      find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
      gh run download "$run_id" -n "$ARTIFACT_NAME" -D "$DEST"
      "${ROOT}/scripts/ci_frontend_build_assert.sh" "$SHA" "$DEST"
      exit 0
    fi
  fi
  sleep "$POLL_S"
done

echo "::error::production frontend artifact ${ARTIFACT_NAME} was not published by CI within ${MAX_WAIT_S}s; refusing to build locally"
exit 1
