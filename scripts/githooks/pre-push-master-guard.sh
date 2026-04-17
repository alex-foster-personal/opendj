#!/usr/bin/env bash
# Refuse a push to master unless MDT_ALLOW_MASTER_PUSH=1.
#
# Install with:
#   ln -sf ../../scripts/githooks/pre-push-master-guard.sh .git/hooks/pre-push
#
# This is the client-side stand-in for server-side branch protection.
# GitHub Free on a private repo cannot enforce protection, so we enforce
# the branching convention locally instead. See docs/branching.md.
#
# Override (rare, emergency only):
#   MDT_ALLOW_MASTER_PUSH=1 git push origin master

set -eu

remote="${1:-}"
url="${2:-}"

zero="0000000000000000000000000000000000000000"

while read -r local_ref local_sha remote_ref remote_sha; do
  case "$remote_ref" in
    refs/heads/master|refs/heads/main)
      if [ "${MDT_ALLOW_MASTER_PUSH:-0}" != "1" ]; then
        echo "pre-push-master-guard: refusing to push to $remote_ref" >&2
        echo "                       set MDT_ALLOW_MASTER_PUSH=1 to override." >&2
        echo "                       see docs/branching.md for the convention." >&2
        exit 1
      fi
      ;;
  esac
done

exit 0
