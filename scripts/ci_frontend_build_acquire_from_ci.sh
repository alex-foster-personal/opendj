#!/usr/bin/env bash
# Download the CI workflow's shared production frontend artifact for this commit.
#
# Polls the repository artifact index until production-frontend-<sha> exists or the
# wait budget expires. Never runs pnpm build locally.
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
  artifact_id="$(
    gh api -H "Accept: application/vnd.github+json" \
      "repos/${REPO}/actions/artifacts?name=${ARTIFACT_NAME}&per_page=5" \
      --jq ".artifacts[] | select(.expired==false and .name==\"${ARTIFACT_NAME}\") | .id" | head -n1
  )"
  if [ -n "$artifact_id" ]; then
    tmp_zip="$(mktemp -t mdt-frontend-artifact.XXXXXX.zip)"
    trap 'rm -f "$tmp_zip"' EXIT
    gh api -H "Accept: application/vnd.github+json" \
      "repos/${REPO}/actions/artifacts/${artifact_id}/zip" >"$tmp_zip"
    mkdir -p "$DEST"
    find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    unzip -qo "$tmp_zip" -d "$DEST"
    "${ROOT}/scripts/ci_frontend_build_assert.sh" "$SHA" "$DEST"
    exit 0
  fi
  sleep "$POLL_S"
done

echo "::error::production frontend artifact ${ARTIFACT_NAME} was not published by CI within ${MAX_WAIT_S}s; refusing to build locally"
exit 1
