#!/usr/bin/env bash
# Refuse a push that would update a branch whose open PR carries another fleet's
# reviewer:* lease (issue #272).
#
# Install with:
#   ln -sf ../../scripts/githooks/pre-push-reviewer-lease.sh .git/hooks/pre-push
#
# Chain with pre-push-master-guard.sh by calling both from one dispatcher hook,
# or install this hook alone when reviewer-lease enforcement is enough.
#
# Emergency bypass (rare): set MDT_REVIEWER_LEASE_EMERGENCY=1 before push.
# The hook posts explicit commit slices to the PR and allows the push only after
# the comment succeeds. See scripts/review_lease.py and FANOUT-CONVENTIONS.md.

set -euo pipefail

remote="${1:-}"
url="${2:-}"

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [ "${MDT_REVIEWER_LEASE_EMERGENCY:-0}" = "1" ]; then
  export MDT_REVIEWER_LEASE_EMERGENCY=1
fi

while read -r local_ref local_sha remote_ref remote_sha; do
  printf '%s %s %s %s\n' "$local_ref" "$local_sha" "$remote_ref" "$remote_sha"
done | uv run --no-sync python -m scripts.review_lease check-push

exit 0
