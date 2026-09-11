#!/usr/bin/env bash
# Stamp a production frontend build directory with the commit it was built for.
#
# Pinned by tests/scripts/test_ci_frontend_build_artifact.py.
set -euo pipefail

SHA="${1:?full git sha required}"
BUILD_DIR="${2:-apps/webui/frontend/build}"

mkdir -p "$BUILD_DIR"
printf '%s\n' "$SHA" > "${BUILD_DIR}/.ci-production-frontend-sha"
