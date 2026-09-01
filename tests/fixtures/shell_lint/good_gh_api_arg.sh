#!/usr/bin/env bash
# GOOD FIXTURE for rule `gh-api-arg`: all three correct shapes.
set -euo pipefail

# Interpolate into the query, because gh api has no parameter binding.
pr_head_sha_interpolated() {
    local want="$2"
    gh api "repos/{owner}/{repo}/pulls/$1" --jq ".head.sha | select(. != \"${want}\")"
}

# Or pipe real output to a real jq, where --arg is the correct flag and binds.
pr_head_sha_piped() {
    gh api "repos/{owner}/{repo}/pulls/$1" \
        | jq -r --arg want "$2" '.head.sha | select(. != $want)'
}

# gh api's own flag for request fields is -f/--field, which it does honor.
open_issue() {
    gh api --method POST repos/o/r/issues -f title="$1"
}
