#!/usr/bin/env bash
# Fail closed when a production frontend build is missing or for the wrong commit.
#
# Consumers must call this after restoring an artifact. There is no local-build
# fallback: a missing or mismatched stamp is a hard error.
#
# Pinned by tests/scripts/test_ci_frontend_build_artifact.py.
set -euo pipefail

SHA="${1:?full git sha required}"
BUILD_DIR="${2:-apps/webui/frontend/build}"
STAMP="${BUILD_DIR}/.ci-production-frontend-sha"
INDEX="${BUILD_DIR}/index.html"

if [ ! -f "$INDEX" ]; then
  echo "::error::production frontend build is missing ${INDEX}"
  exit 1
fi

if [ ! -f "$STAMP" ]; then
  echo "::error::production frontend artifact stamp is missing ${STAMP}"
  exit 1
fi

got="$(tr -d '[:space:]' < "$STAMP")"
if [ "$got" != "$SHA" ]; then
  echo "::error::production frontend artifact stamp ${got} does not match expected ${SHA}"
  exit 1
fi
