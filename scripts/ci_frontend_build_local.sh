#!/usr/bin/env bash
# Build the production frontend locally: the exact install/build/stamp
# commands ci.yml's "production frontend build" job runs
# (.github/workflows/ci.yml, job frontend-build), extracted so the one other
# real caller -- scripts/ci_frontend_build_acquire_from_ci.sh's local-build
# path -- does not duplicate them by hand.
#
# ci.yml's own frontend-build job keeps its three inline steps rather than
# calling this script: collapsing them would remove the literal `pnpm build`
# text that tests/scripts/test_ci_frontend_build_artifact.py pins to exactly
# one occurrence in that job, and would lose per-step timing in the Actions
# UI for the job most contended for shared-runner minutes. See
# docs/decisions/ADR-0028-e2e-frontend-artifact-local-build.md.
#
# Callers must already have Node on PATH: ci.yml's frontend-build job and
# e2e.yml's gate/extended jobs both set it up via actions/setup-node before
# any of this runs. Corepack + the pinned pnpm activation happen here so
# this script is otherwise self-contained (and idempotent to rerun even when
# a caller already activated pnpm itself).
#
# Pinned by tests/scripts/test_ci_frontend_build_artifact.py.
set -euo pipefail

SHA="${1:?full git sha required}"
DEST="${2:-apps/webui/frontend/build}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT}/apps/webui/frontend"

# Pinned identically to apps/webui/frontend/package.json's "packageManager"
# and every other corepack step in .github/workflows/*.yml; update together.
corepack enable
corepack prepare pnpm@11.9.0 --activate

(cd "$FRONTEND_DIR" && pnpm install --frozen-lockfile)
(cd "$FRONTEND_DIR" && pnpm build)

"${ROOT}/scripts/ci_frontend_build_stamp.sh" "$SHA" "$DEST"
