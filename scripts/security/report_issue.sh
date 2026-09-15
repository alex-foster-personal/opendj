#!/usr/bin/env bash
# Keep ONE rolling GitHub issue per cadence (docs/security/routine-scanning.md).
#
# Usage: scripts/security/report_issue.sh daily|weekly <scan-result> <summary.md> <report.md>
#   scan-result  the scan job's result (success | failure | cancelled | skipped)
#
# Clean = the job succeeded AND the summary has no FAIL or UNKNOWN row. Clean closes
# an open issue (with a comment); not clean creates, reopens or rewrites it. A missing
# summary is UNKNOWN (the scan job died before writing one), never clean.
# Needs GH_TOKEN with issues: write. Uses the gh CLI only (no marketplace action).

set -euo pipefail

CADENCE="${1:?usage: report_issue.sh daily|weekly <scan-result> <summary.md> <report.md>}"
SCAN_RESULT="${2:?scan result required}"
SUMMARY_MD="${3:?summary.md path required}"
REPORT_MD="${4:?report.md path required}"
LABEL="security"
MAX_BODY_CHARS=60000

if [[ "$CADENCE" == "daily" ]]; then
  TITLE="security: daily dependency scan"
elif [[ "$CADENCE" == "weekly" ]]; then
  TITLE="security: weekly review"
else
  printf '[ERROR] cadence must be daily or weekly, got: %s\n' "$CADENCE" >&2
  exit 64
fi
REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/$REPO/actions/runs/${GITHUB_RUN_ID:-local}"
NOW_UTC="$(date -u '+%a %d %b %Y %H:%M %Z')"

# ----- verdict ----------------------------------------------------------------------------------
if [[ ! -s "$SUMMARY_MD" ]]; then
  verdict="UNKNOWN"
  table="_No summary was produced: the scan job ended ($SCAN_RESULT) before writing one._"
  details=""
else
  table="$(cat "$SUMMARY_MD")"
  details="$(cat "$REPORT_MD" 2>/dev/null || true)"
  if grep -qE '\| (FAIL|UNKNOWN) \|' "$SUMMARY_MD" || [[ "$SCAN_RESULT" != "success" ]]; then
    verdict="FINDINGS"
    grep -q '| UNKNOWN |' "$SUMMARY_MD" && verdict="UNKNOWN"
  else
    verdict="CLEAN"
  fi
fi

body="$(printf '**%s** at %s (job result: `%s`). [Run](%s)\n\n%s\n\n%s\n\n---\nMaintained by `scripts/security/report_issue.sh`: one rolling issue per cadence; closes itself when clean.\n' \
  "$verdict" "$NOW_UTC" "$SCAN_RESULT" "$RUN_URL" "$table" "$details")"
if [[ ${#body} -gt $MAX_BODY_CHARS ]]; then
  body="${body:0:$MAX_BODY_CHARS}"$'\n\n_Truncated; full detail in the run summary._'
fi

# ----- label + issue ------------------------------------------------------------------------------
if ! gh label list --repo "$REPO" --search "$LABEL" --json name --jq '.[].name' | grep -qx "$LABEL"; then
  gh label create "$LABEL" --repo "$REPO" --color B60205 --description "Security scan findings and hardening"
fi
issue="$(gh issue list --repo "$REPO" --label "$LABEL" --state all --search "\"$TITLE\" in:title" \
  --json number,title,state --jq "map(select(.title == \"$TITLE\")) | sort_by(.state != \"OPEN\") | .[0] | \"\(.number) \(.state)\"" || true)"
number="${issue%% *}"
state="${issue##* }"
[[ "$number" == "null" || -z "$number" ]] && number="" && state=""

if [[ "$verdict" == "CLEAN" ]]; then
  if [[ "$state" == "OPEN" ]]; then
    gh issue edit "$number" --repo "$REPO" --body "$body"
    gh issue close "$number" --repo "$REPO" --comment "Clean at $NOW_UTC ($RUN_URL). Closing; the next non-clean scan reopens this issue."
    printf '[OK] %s clean: closed #%s\n' "$CADENCE" "$number"
  else
    printf '[OK] %s clean: no open issue to update\n' "$CADENCE"
  fi
else
  if [[ -z "$number" ]]; then
    url="$(gh issue create --repo "$REPO" --title "$TITLE" --label "$LABEL" --body "$body")"
    printf '[WARN] %s %s: opened %s\n' "$CADENCE" "$verdict" "$url"
  else
    [[ "$state" == "CLOSED" ]] && gh issue reopen "$number" --repo "$REPO"
    gh issue edit "$number" --repo "$REPO" --body "$body"
    printf '[WARN] %s %s: updated #%s\n' "$CADENCE" "$verdict" "$number"
  fi
fi
