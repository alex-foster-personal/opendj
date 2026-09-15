#!/usr/bin/env bash
# Static analysis (Semgrep CE). Spec: docs/security/static-analysis.md
#
# Usage: scripts/security/scan_sast.sh pr|full
#   pr    diff-aware: only findings NEW versus the base commit (--baseline-commit).
#   full  whole tree. Reports existing debt; not wired to CI (would be red on day one).
#
# Rules: registry packs p/python p/typescript p/rust plus tools/semgrep/ (custom).
# The control directory must trip every custom rule and one rule from each pack,
# and the loaded rule count must clear MIN_RULES, so a pack that silently failed
# to download reads as UNKNOWN rather than clean.

# shellcheck source=scripts/security/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:?usage: scan_sast.sh pr|full}"
sec_require_mode "$MODE"
SCANNER="semgrep"
CONFIGS=(--config p/python --config p/typescript --config p/rust --config tools/semgrep)
CONTROL_DIR="tests/fixtures/security/sast-control"
REQUIRED_CONTROL_RULES=(
  odj-image-open-without-formats # tools/semgrep (custom)
  odj-subprocess-shell-true      # tools/semgrep (custom)
  subprocess-shell-true          # p/python
  eval-detected                  # p/typescript
  unsafe-usage                   # p/rust
)
MIN_RULES=300
OUT="$SECURITY_WORK_DIR/sast"
rm -rf "$OUT" && mkdir -p "$OUT"

_unknown_exit() { sec_unknown "$SCANNER" "$MODE" "$1" || exit 2; }

# _semgrep <json-out> <args...>: exit 0 (clean) and 1 (findings, via --error) are measurements.
_semgrep() {
  local json="$1" rc=0
  shift
  (cd "$SECURITY_REPO_ROOT" && "$SECURITY_BIN_DIR/semgrep" scan "${CONFIGS[@]}" --json --time \
    --metrics=off --disable-version-check --error "$@" >"$json") || rc=$?
  if [[ $rc -ne 0 && $rc -ne 1 ]]; then
    echo "semgrep exited $rc" >&2
    return 2
  fi
}

sec_require_bin semgrep || _unknown_exit "binary missing"
sec_require_bin uv || _unknown_exit "uv missing (needed for secscan.py)"

# ----- positive control -------------------------------------------------------------------------
_semgrep "$OUT/control.json" "$CONTROL_DIR" || _unknown_exit "control scan errored"
require_args=()
for rule in "${REQUIRED_CONTROL_RULES[@]}"; do require_args+=(--require-rule "$rule"); done
sec_py semgrep-summary "$OUT/control.json" "${require_args[@]}" --min-rules "$MIN_RULES" \
  --fail-on-error --count-file "$OUT/control.count" >"$OUT/control.txt" ||
  _unknown_exit "control did not fire or rules failed to load (see above)"
control_hits="$(cat "$OUT/control.count")"

# ----- scan ---------------------------------------------------------------------------------------
if [[ "$MODE" == "pr" ]]; then
  base="$(sec_base_sha)"
  _semgrep "$OUT/scan.json" --baseline-commit "$base" --exclude "tests/fixtures/security" ||
    _unknown_exit "diff-aware scan errored"
  title="semgrep: findings new vs ${base:0:9}"
  detail="diff-aware vs ${base:0:9}"
elif [[ "$MODE" == "full" ]]; then
  _semgrep "$OUT/scan.json" --exclude "tests/fixtures/security" || _unknown_exit "full scan errored"
  title="semgrep: whole tree"
  detail="whole tree (existing debt included)"
fi
sec_py semgrep-summary "$OUT/scan.json" --min-rules "$MIN_RULES" --count-file "$OUT/scan.count" \
  --report-md "$SECURITY_WORK_DIR/report.md" --title "$title" ||
  _unknown_exit "scan output unparseable or rules missing"

findings="$(cat "$OUT/scan.count")"
if [[ "$findings" -eq 0 ]]; then
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" 0 PASS "$detail"
else
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" "$findings" FAIL "$detail"
  exit 1
fi
