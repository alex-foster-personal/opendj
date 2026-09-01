#!/usr/bin/env bash
# GOOD FIXTURE for rule `pipeline-status`, the OTHER safe form: under pipefail
# the shell propagates the first non-zero component, so $? after a pipeline is
# the pipeline's status and the read is correct.
set -euo pipefail

verify_build() {
    local log="$1"
    npm run build 2>&1 | tee "$log"
    EXIT_CODE=$?
    return "$EXIT_CODE"
}
