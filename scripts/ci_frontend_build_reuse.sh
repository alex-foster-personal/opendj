#!/usr/bin/env bash
# Re-point a production frontend build restored by frontend tree hash at THIS commit.
#
# ci.yml's `frontend-build` restores a build made for another commit whose build inputs
# (apps/webui/frontend's tree and apps/desktop/electron/package.json's version) are identical.
# The only commit-specific bytes are the git sha svelte.config.js writes as kit.version.name
# (`_app/version.json`, the start chunk) and the `.ci-production-frontend-sha` stamp. Both are
# rewritten to the new sha (same 40-hex length), so `_verify_frontend_build_version` and
# scripts/ci_frontend_build_assert.sh see this commit. A `-dirty` build is never reused.
#
# Regression lines:
#   - [if] a reused build's _app/version.json still names the old sha [then] fail, [else stop].
#   - [if] a build with no stamp or a dirty version is reused [then] fail, [else stop].
#
# Usage: scripts/ci_frontend_build_reuse.sh <new full sha> [build dir]
# -Claude
set -euo pipefail

NEW="${1:?full git sha required}"
BUILD_DIR="${2:-apps/webui/frontend/build}"
STAMP="${BUILD_DIR}/.ci-production-frontend-sha"
VERSION_JSON="${BUILD_DIR}/_app/version.json"

[ -f "$STAMP" ] || { echo "::error::restored build has no stamp ${STAMP}"; exit 1; }
OLD="$(tr -d '[:space:]' <"$STAMP")"
[[ "$OLD" =~ ^[0-9a-f]{40}$ && "$NEW" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::bad sha old=${OLD} new=${NEW}"; exit 1; }
grep -q -- "${OLD}-dirty" "$VERSION_JSON" && { echo "::error::restored build is dirty; not reusable"; exit 1; }

if [ "$OLD" != "$NEW" ]; then
    grep -rlF -- "$OLD" "$BUILD_DIR" | while IFS= read -r file; do
        perl -pi -e "s/${OLD}/${NEW}/g" "$file"
    done
fi
grep -qF -- "\"${NEW}\"" "$VERSION_JSON" || { echo "::error::${VERSION_JSON} does not name ${NEW} after reuse"; exit 1; }
if grep -rqF -- "$OLD" "$BUILD_DIR" && [ "$OLD" != "$NEW" ]; then
    echo "::error::the old sha ${OLD} survives in ${BUILD_DIR}"
    exit 1
fi
echo "[frontend-build] reused the build of ${OLD} for ${NEW} (same frontend tree)"
