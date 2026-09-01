#!/usr/bin/env bash
# BAD FIXTURE for scripts/shell_construct_lint.py rule `gh-api-arg`.
# Never sourced, never run.
#
# `gh api` accepts --jq but SILENTLY IGNORES --arg (unlike `gh run list`, which
# errors on it). So $want is unbound inside the query, the select never matches
# what the caller asked about, and the command returns a confident wrong answer
# with exit 0.
set -euo pipefail

pr_head_sha_if_wanted() {
    gh api "repos/{owner}/{repo}/pulls/$1" \
        --jq '.head.sha | select(. != $want)' \
        --arg want "$2"
}
