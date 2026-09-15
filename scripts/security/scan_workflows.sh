#!/usr/bin/env bash
# GitHub Actions supply-chain checks. Spec: docs/security/ci-supply-chain.md
#
# Usage: scripts/security/scan_workflows.sh pr|full
#   pr    invariants always; zizmor + actionlint only when the PR touched .github/.
#   full  invariants, zizmor and actionlint over every workflow.
#
# Rows:
#   workflow-invariants  every workflow has a top-level permissions: block, and every
#                        security.yml job runs on GitHub-hosted ubuntu-latest (rule 4).
#   zizmor               --min-severity=medium, config .github/zizmor.yml (each
#                        suppression carries a reason). Offline: no GitHub API audits.
#   actionlint           syntax + expression checks; shellcheck/pyflakes off (style
#                        noise, not security signal; see the PR that added this).

# shellcheck source=scripts/security/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:?usage: scan_workflows.sh pr|full}"
sec_require_mode "$MODE"
WORKFLOWS_DIR=".github/workflows"
ZIZMOR_CONFIG=".github/zizmor.yml"
SECURITY_WORKFLOW=".github/workflows/security.yml"
CONTROL="tests/fixtures/security/zizmor-control/template-injection.yml"
OUT="$SECURITY_WORK_DIR/workflows"
rm -rf "$OUT" && mkdir -p "$OUT"
cd "$SECURITY_REPO_ROOT"

# ----- invariants (a grep is an instrument too: each gets a control that must fire) ----------------
check_invariants() {
  local scanner="workflow-invariants" probe="$OUT/no-permissions.yml" missing runs_on offending
  printf 'on: push\njobs: {}\n' >"$probe"
  [[ -n "$(grep -L '^permissions:' "$probe")" ]] ||
    { sec_unknown "$scanner" "$MODE" "permissions grep control did not fire"; return 2; }
  missing="$(grep -L '^permissions:' "$WORKFLOWS_DIR"/*.yml || true)"
  runs_on="$(grep -cE '^\s+runs-on:' "$SECURITY_WORKFLOW" || true)"
  [[ "$runs_on" -gt 0 ]] ||
    { sec_unknown "$scanner" "$MODE" "no runs-on lines found in $SECURITY_WORKFLOW"; return 2; }
  offending="$(grep -E '^\s+runs-on:' "$SECURITY_WORKFLOW" | grep -vE 'runs-on: ubuntu-latest$' || true)"
  if [[ -z "$missing" && -z "$offending" ]]; then
    sec_row "$scanner" "$MODE" "fired(1)" 0 PASS "all workflows declare permissions; $runs_on security job(s) hosted"
    return 0
  fi
  {
    printf '\n### workflow invariants\n\n'
    for f in $missing; do printf -- '- `%s` lacks a top-level permissions: block\n' "$f"; done
    [[ -n "$offending" ]] && printf -- '- `%s` has a non-hosted runs-on: `%s`\n' "$SECURITY_WORKFLOW" "$offending"
  } | tee -a "$SECURITY_WORK_DIR/report.md"
  sec_row "$scanner" "$MODE" "fired(1)" "$(($(echo "$missing" | grep -c . || true) + $(echo "$offending" | grep -c . || true)))" FAIL "see report"
  return 1
}

# _zizmor <json-out> <paths...>: 0 = clean, >= 10 = findings by max severity, else not measured.
_zizmor() {
  local json="$1" rc=0
  shift
  "$SECURITY_BIN_DIR/zizmor" --offline --min-severity=medium --format=json --config "$ZIZMOR_CONFIG" \
    "$@" >"$json" || rc=$?
  if [[ $rc -ne 0 && $rc -lt 10 ]]; then
    echo "zizmor exited $rc" >&2
    return 2
  fi
}

scan_zizmor() {
  local scanner="zizmor" control_hits findings
  sec_require_bin zizmor || { sec_unknown "$scanner" "$MODE" "binary missing"; return 2; }
  _zizmor "$OUT/zizmor-control.json" "$CONTROL" || { sec_unknown "$scanner" "$MODE" "control scan errored"; return 2; }
  sec_py zizmor-summary "$OUT/zizmor-control.json" --count-file "$OUT/zizmor-control.count" >"$OUT/zizmor-control.txt"
  control_hits="$(grep -c 'template-injection' "$OUT/zizmor-control.txt" || true)"
  [[ "$control_hits" -gt 0 ]] || { sec_unknown "$scanner" "$MODE" "control did not fire ($CONTROL)"; return 2; }
  _zizmor "$OUT/zizmor.json" "$WORKFLOWS_DIR" || { sec_unknown "$scanner" "$MODE" "scan errored"; return 2; }
  sec_py zizmor-summary "$OUT/zizmor.json" --count-file "$OUT/zizmor.count" \
    --report-md "$SECURITY_WORK_DIR/report.md" --title "zizmor (medium+)"
  findings="$(cat "$OUT/zizmor.count")"
  if [[ "$findings" -eq 0 ]]; then
    sec_row "$scanner" "$MODE" "fired($control_hits)" 0 PASS "$(ls "$WORKFLOWS_DIR"/*.yml | wc -l | tr -d ' ') workflows, medium+"
  else
    sec_row "$scanner" "$MODE" "fired($control_hits)" "$findings" FAIL "medium+ findings"
    return 1
  fi
}

# One line per error; exit 1 = errors found, 0 = clean, anything else = not measured.
_actionlint() {
  local out="$1" rc=0
  shift
  "$SECURITY_BIN_DIR/actionlint" -shellcheck= -pyflakes= \
    -format '{{range $e := .}}{{$e.Filepath}}:{{$e.Line}} {{$e.Kind}} {{$e.Message}}{{"\n"}}{{end}}' \
    "$@" >"$out" || rc=$?
  if [[ $rc -ne 0 && $rc -ne 1 ]]; then
    echo "actionlint exited $rc" >&2
    return 2
  fi
}

scan_actionlint() {
  local scanner="actionlint" control_hits findings
  sec_require_bin actionlint || { sec_unknown "$scanner" "$MODE" "binary missing"; return 2; }
  _actionlint "$OUT/actionlint-control.txt" "$CONTROL" || { sec_unknown "$scanner" "$MODE" "control errored"; return 2; }
  control_hits="$(grep -c 'potentially untrusted' "$OUT/actionlint-control.txt" || true)"
  [[ "$control_hits" -gt 0 ]] || { sec_unknown "$scanner" "$MODE" "control did not fire ($CONTROL)"; return 2; }
  _actionlint "$OUT/actionlint.txt" "$WORKFLOWS_DIR"/*.yml || { sec_unknown "$scanner" "$MODE" "scan errored"; return 2; }
  findings="$(grep -c . "$OUT/actionlint.txt" || true)"
  if [[ "$findings" -eq 0 ]]; then
    sec_row "$scanner" "$MODE" "fired($control_hits)" 0 PASS "syntax + expressions"
  else
    { printf '\n### actionlint (%s)\n\n' "$findings"; sed 's/^/- `/; s/$/`/' "$OUT/actionlint.txt"; } >>"$SECURITY_WORK_DIR/report.md"
    cat "$OUT/actionlint.txt"
    sec_row "$scanner" "$MODE" "fired($control_hits)" "$findings" FAIL "syntax + expressions"
    return 1
  fi
}

# ----- main --------------------------------------------------------------------------------------
status=0
_track() { local rc=0; "$@" || rc=$?; if [[ $rc -gt $status ]]; then status=$rc; fi; }
_track check_invariants
if [[ "$MODE" == "pr" ]] && ! sec_changed_files | grep -q '^\.github/'; then
  sec_row zizmor "$MODE" "not run" 0 SKIP "no .github/ change vs $(sec_base_sha | cut -c1-9)"
  sec_row actionlint "$MODE" "not run" 0 SKIP "no .github/ change vs $(sec_base_sha | cut -c1-9)"
else
  _track scan_zizmor
  _track scan_actionlint
fi
exit "$status"
