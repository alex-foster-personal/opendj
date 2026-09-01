#!/usr/bin/env bash
# GOOD FIXTURE for rule `pipeline-status`, WITHOUT pipefail: the status of the
# pipeline is read from PIPESTATUS, and a plain command's $? is not a pipeline
# read at all.
set -eu

verify_build() {
    local log="$1"
    npm run build 2>&1 | tee "$log"
    EXIT_CODE="${PIPESTATUS[0]}"
    return "$EXIT_CODE"
}

verify_build_no_pipe() {
    npm run build
    EXIT_CODE=$?
    return "$EXIT_CODE"
}

# The pipeline is closed by the function's own brace, so the next statement's
# $? reads whatever ran after it, not the pipeline.
count_entries() { ls | wc -l; }
report_status() { echo "$?"; }
