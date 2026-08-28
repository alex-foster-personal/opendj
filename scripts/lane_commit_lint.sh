#!/bin/bash
# Conventional-commit + fleet-trailer lint for the agentB rebuild lane.
# Checks every commit on this branch beyond the parity base.
set -euo pipefail
BASE=6287d7c1
fail=0
for sha in $(git rev-list "$BASE"..HEAD); do
  subj=$(git log -1 --format=%s "$sha")
  body=$(git log -1 --format=%B "$sha")
  echo "$subj" | grep -Eq '^(feat|fix|docs|chore|refactor|test|perf|ci|build|style)(\(|:|!)' || { echo "[ERROR] non-conventional subject: $sha $subj"; fail=1; }
  echo "$body" | grep -Eq -- '-(Claude|Codex)' || { echo "[ERROR] missing fleet trailer: $sha $subj"; fail=1; }
done
if [ "$fail" -eq 0 ]; then echo "[OK] commit lint clean"; fi
exit $fail
