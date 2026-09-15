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
CONFIGS=(--config p/python --config p/typescript --config p/rust --config "$SECURITY_REPO_ROOT/tools/semgrep")
CONTROL_DIR="tests/fixtures/security/sast-control"
REQUIRED_CONTROL_RULES=(
  odj-image-open-without-formats # tools/semgrep (custom)
  odj-subprocess-shell-true      # tools/semgrep (custom)
  subprocess-shell-true          # p/python
  wildcard-postmessage-configuration # p/typescript
  unsafe-usage                   # p/rust
)
MIN_RULES=200
OUT="$SECURITY_WORK_DIR/sast"
rm -rf "$OUT" && mkdir -p "$OUT"

_unknown_exit() { sec_unknown "$SCANNER" "$MODE" "$1" || exit 2; }

# _semgrep <dir> <json-out> <args...>: exit 0 (clean) and 1 (findings, via --error) are measurements.
_semgrep() {
  local dir="$1" json="$2" rc=0
  shift 2
  (cd "$dir" && "$SECURITY_BIN_DIR/semgrep" scan "${CONFIGS[@]}" --json --time \
    --metrics=off --disable-version-check --error "$@" >"$json") || rc=$?
  if [[ $rc -ne 0 && $rc -ne 1 ]]; then
    echo "semgrep exited $rc" >&2
    return 2
  fi
}

sec_require_bin semgrep || _unknown_exit "binary missing"
sec_require_bin uv || _unknown_exit "uv missing (needed for secscan.py)"

# ----- positive control -------------------------------------------------------------------------
# Inside a git repo Semgrep scans only tracked files and skips tests/ by default, so the
# control runs on a copy outside the repo whose own empty .semgrepignore turns the
# default ignore list off. The copy keeps the repo-relative path the custom rules include.
control_root="$(mktemp -d "${TMPDIR:-/tmp}/sast-control.XXXXXX")"
trap 'rm -rf "$control_root"' EXIT
mkdir -p "$control_root/$(dirname "$CONTROL_DIR")"
cp -R "$SECURITY_REPO_ROOT/$CONTROL_DIR" "$control_root/$CONTROL_DIR"
printf '# empty on purpose: disables the default ignore list for the control copy\n' \
  >"$control_root/.semgrepignore"
control_files="$(find "$control_root/$CONTROL_DIR" -type f | wc -l | tr -d ' ')"
_semgrep "$control_root" "$OUT/control.json" . || _unknown_exit "control scan errored"
require_args=()
for rule in "${REQUIRED_CONTROL_RULES[@]}"; do require_args+=(--require-rule "$rule"); done
sec_py semgrep-summary "$OUT/control.json" "${require_args[@]}" --min-rules "$MIN_RULES" \
  --min-files "$control_files" --fail-on-error --count-file "$OUT/control.count" >"$OUT/control.txt" ||
  _unknown_exit "control did not fire or rules failed to load (see above)"
control_hits="$(cat "$OUT/control.count")"

# ----- scan ---------------------------------------------------------------------------------------
if [[ "$MODE" == "pr" ]]; then
  base="$(sec_base_sha)"
  # Diff-aware mode scans only files changed since base, so zero files is a valid answer.
  _semgrep "$SECURITY_REPO_ROOT" "$OUT/scan.json" --baseline-commit "$base" \
    --exclude "tests/fixtures/security" || _unknown_exit "diff-aware scan errored"
  title="semgrep: findings new vs ${base:0:9}"
  detail="diff-aware vs ${base:0:9}"
  # A diff with no file semgrep scans (docs only, or ignored paths) loads no rules
  # at all; secscan reports that as SKIP (exit 3) instead of a missing-rules UNKNOWN.
  skip_args=(--skip-if-nothing-scanned)
elif [[ "$MODE" == "full" ]]; then
  _semgrep "$SECURITY_REPO_ROOT" "$OUT/scan.json" --exclude "tests/fixtures/security" ||
    _unknown_exit "full scan errored"
  title="semgrep: whole tree"
  detail="whole tree (existing debt included)"
  skip_args=()
fi
# The rule floor is proved by the control above; a diff-aware run loads only the
# rules for the languages it scans (154 Python rules for a Python-only diff).
summary_rc=0
sec_py semgrep-summary "$OUT/scan.json" --min-rules 1 --min-files 1 ${skip_args[@]+"${skip_args[@]}"} \
  --count-file "$OUT/scan.count" \
  --report-md "$SECURITY_WORK_DIR/report.md" --title "$title" || summary_rc=$?
if [[ $summary_rc -eq 3 ]]; then
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" 0 SKIP "no scannable file changed vs ${base:0:9}"
  exit 0
elif [[ $summary_rc -ne 0 ]]; then
  _unknown_exit "scan output unparseable or rules missing"
fi

findings="$(cat "$OUT/scan.count")"
if [[ "$findings" -eq 0 ]]; then
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" 0 PASS "$detail"
else
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" "$findings" FAIL "$detail"
  exit 1
fi
