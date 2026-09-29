#!/usr/bin/env bash
# Static analysis (Semgrep CE). Spec: docs/security/static-analysis.md
#
# Usage: scripts/security/scan_sast.sh pr|full
#   pr    diff-aware: only findings NEW versus the base commit (--baseline-commit).
#         SKIP (with reason) when the diff has no file semgrep scans, decided from
#         git diff scope plus semgrep target resolution, not from semgrep's 0/0 output.
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
#
# The control contract is the TRACKED file list, never a filesystem walk: a stray
# untracked file (pytest's __pycache__ beside the .py fixtures, Wed 16 Sep 2026) must not
# raise the floor, and a tracked file missing from the working tree must fail loudly
# rather than quietly lower it.
control_root="$(mktemp -d "${TMPDIR:-/tmp}/sast-control.XXXXXX")"
trap 'rm -rf "$control_root"' EXIT
printf '# empty on purpose: disables the default ignore list for the control copy\n' \
  >"$control_root/.semgrepignore"
git -C "$SECURITY_REPO_ROOT" ls-files -z -- "$CONTROL_DIR" >"$OUT/control.files" ||
  _unknown_exit "git ls-files failed for $CONTROL_DIR, so the control file list is unknown"
control_targets=()
while IFS= read -r -d '' _control_file; do
  [[ -f "$SECURITY_REPO_ROOT/$_control_file" ]] ||
    _unknown_exit "tracked control file missing from the working tree: $_control_file"
  mkdir -p "$control_root/$(dirname "$_control_file")"
  cp "$SECURITY_REPO_ROOT/$_control_file" "$control_root/$_control_file"
  control_targets+=("$_control_file")
done <"$OUT/control.files"
control_files="${#control_targets[@]}"
[[ "$control_files" -gt 0 ]] || _unknown_exit "git tracks no files under $CONTROL_DIR"
_semgrep "$control_root" "$OUT/control.json" "${control_targets[@]}" ||
  _unknown_exit "control scan errored"
control_args=()
for rule in "${REQUIRED_CONTROL_RULES[@]}"; do control_args+=(--require-rule "$rule"); done
for target in "${control_targets[@]}"; do control_args+=(--expect-file "$target"); done
# secscan prints its own UNKNOWN reason; the summary row carries that reason, not a guess.
if ! sec_py semgrep-summary "$OUT/control.json" "${control_args[@]}" --min-rules "$MIN_RULES" \
  --min-files "$control_files" --fail-on-error --count-file "$OUT/control.count" \
  >"$OUT/control.txt" 2>"$OUT/control.err"; then
  cat "$OUT/control.err" >&2
  reason="$(sed -n 's/^UNKNOWN: //p' "$OUT/control.err" | tail -n 1)"
  _unknown_exit "control unmeasured: ${reason:-secscan semgrep-summary failed without an UNKNOWN line (stderr above)}"
fi
control_hits="$(cat "$OUT/control.count")"

# ----- scan ---------------------------------------------------------------------------------------
if [[ "$MODE" == "pr" ]]; then
  base="$(sec_base_sha)"
  head="$(sec_head_sha)"
  sec_py semgrep-diff-scope \
    --root "$SECURITY_REPO_ROOT" --base "$base" --head "$head" \
    --semgrep "$SECURITY_BIN_DIR/semgrep" \
    --config p/python --config p/typescript --config p/rust \
    --config "$SECURITY_REPO_ROOT/tools/semgrep" \
    --exclude tests/fixtures/security \
    --count-file "$OUT/scope.count" >"$OUT/scope.txt" ||
    _unknown_exit "could not resolve diff scope"
  scannable="$(cat "$OUT/scope.count")"
  if [[ "$scannable" -eq 0 ]]; then
    sec_row "$SCANNER" "$MODE" "fired($control_hits)" 0 SKIP "no scannable file changed vs ${base:0:9}"
    exit 0
  fi
  # Diff-aware mode scans only files changed since base, so zero files is a valid answer.
  _semgrep "$SECURITY_REPO_ROOT" "$OUT/scan.json" --baseline-commit "$base" \
    --exclude "tests/fixtures/security" || _unknown_exit "diff-aware scan errored"
  title="semgrep: findings new vs ${base:0:9}"
  detail="diff-aware vs ${base:0:9}"
  expected_args=(--expected-scannable "$scannable")
elif [[ "$MODE" == "full" ]]; then
  _semgrep "$SECURITY_REPO_ROOT" "$OUT/scan.json" --exclude "tests/fixtures/security" ||
    _unknown_exit "full scan errored"
  title="semgrep: whole tree"
  detail="whole tree (existing debt included)"
  expected_args=()
fi
# The rule floor is proved by the control above; a diff-aware run loads only the
# rules for the languages it scans (154 Python rules for a Python-only diff).
sec_py semgrep-summary "$OUT/scan.json" --min-rules 1 --min-files 1 ${expected_args[@]+"${expected_args[@]}"} \
  --count-file "$OUT/scan.count" \
  --report-md "$SECURITY_WORK_DIR/report.md" --title "$title" ||
  _unknown_exit "scan output unparseable or rules missing"

findings="$(cat "$OUT/scan.count")"
if [[ "$findings" -eq 0 ]]; then
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" 0 PASS "$detail"
else
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" "$findings" FAIL "$detail"
  exit 1
fi
