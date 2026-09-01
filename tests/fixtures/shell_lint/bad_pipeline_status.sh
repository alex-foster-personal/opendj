#!/usr/bin/env bash
# BAD FIXTURE for scripts/shell_construct_lint.py rule `pipeline-status`.
# Never sourced, never run: it exists so the lint can be proven able to fail.
#
# Shape of the real instance: a build-VERIFICATION script, the one script whose
# entire job was verifying a build, whose EXIT_CODE came back empty because $?
# after a pipeline reports `tee`, not the build.
set -eu

verify_build() {
    local log="$1"
    npm run build 2>&1 | tee "$log"
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -ne 0 ]; then
        echo "[ERROR] build failed"
        return "$EXIT_CODE"
    fi
    echo "[OK] build verified"
}
