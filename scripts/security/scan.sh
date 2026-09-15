#!/usr/bin/env bash
# Run one or more security areas and print a summary table. Same entrypoint locally
# (`just security-scan`) and in .github/workflows/security.yml.
#
# Usage: scripts/security/scan.sh <areas> <mode>
#   areas  all | comma list of deps,secrets,sast,workflows
#   mode   pr (only what is new vs the base commit) | full (everything at HEAD)
#
# Writes $SECURITY_WORK_DIR/{summary.tsv,summary.md,report.md}. Exit: 0 all PASS/SKIP,
# 1 any FAIL, 2 any UNKNOWN (including an area script that crashed without a verdict).

# shellcheck source=scripts/security/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

AREAS_ARG="${1:?usage: scan.sh all|deps,secrets,sast,workflows pr|full}"
MODE="${2:?usage: scan.sh <areas> pr|full}"
sec_require_mode "$MODE"
ALL_AREAS=(deps secrets sast workflows)

if [[ "$AREAS_ARG" == "all" ]]; then
  areas=("${ALL_AREAS[@]}")
else
  IFS=',' read -r -a areas <<<"$AREAS_ARG"
fi
for area in "${areas[@]}"; do
  [[ " ${ALL_AREAS[*]} " == *" $area "* ]] || { printf '[ERROR] unknown area: %s\n' "$area" >&2; exit 64; }
done

: >"$SECURITY_SUMMARY_FILE"
: >"$SECURITY_WORK_DIR/report.md"
overall=0
for area in "${areas[@]}"; do
  rows_before="$(wc -l <"$SECURITY_SUMMARY_FILE")"
  sec_log "=== $area ($MODE) ==="
  rc=0
  bash "$SECURITY_SCRIPTS/scan_$area.sh" "$MODE" || rc=$?
  if [[ "$(wc -l <"$SECURITY_SUMMARY_FILE")" -eq "$rows_before" || ($rc -ne 0 && $rc -ne 1 && $rc -ne 2) ]]; then
    sec_row "$area" "$MODE" "?" "?" UNKNOWN "scan_$area.sh exited $rc without a verdict"
    rc=2
  fi
  if [[ $rc -gt $overall ]]; then overall=$rc; fi
done

# ----- summary table ------------------------------------------------------------------------------
{
  printf '| scanner | mode | control | findings | status | detail |\n'
  printf '|---|---|---|---|---|---|\n'
  awk -F'\t' '{printf "| %s | %s | %s | %s | %s | %s |\n", $1, $2, $3, $4, $5, $6}' "$SECURITY_SUMMARY_FILE"
} >"$SECURITY_WORK_DIR/summary.md"
printf '\n'
awk -F'\t' 'BEGIN {
    line = "+----------------------+------+------------+----------+---------+"
    print line
    printf "| %-20s | %-4s | %-10s | %8s | %-7s | %s\n", "scanner", "mode", "control", "findings", "status", "detail"
    print line
  }
  { printf "| %-20s | %-4s | %-10s | %8s | %-7s | %s\n", $1, $2, $3, $4, $5, $6 }
  END { print line }' "$SECURITY_SUMMARY_FILE"
printf 'details: %s\n' "$SECURITY_WORK_DIR/report.md"

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  { printf '## security scan (%s)\n\n' "$MODE"; cat "$SECURITY_WORK_DIR/summary.md" "$SECURITY_WORK_DIR/report.md"; } >>"$GITHUB_STEP_SUMMARY"
fi
exit "$overall"
