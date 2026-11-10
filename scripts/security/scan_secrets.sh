#!/usr/bin/env bash
# Secret scanning. Spec: docs/security/secret-scanning.md
#
# Usage: scripts/security/scan_secrets.sh pr|full
#   pr    gitleaks over the commits between the base and HEAD (what a PR adds).
#   full  trufflehog over the whole git history, reporting verified live secrets
#         (FAIL) and secrets whose verification errored (UNKNOWN, never clean).
#   doc-path-leak (pr only): no added line under specs/, app_docs/, docs/ or .planning/
#         names a real home directory (scripts/doc_path_leak.py). full: SKIP.

# shellcheck source=scripts/security/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:?usage: scan_secrets.sh pr|full}"
sec_require_mode "$MODE"
KEY_MATERIAL_PROBES=(x.pem x.key x.p12 x.p8 x.mobileprovision credentials.json service-account-x.json)
OUT="$SECURITY_WORK_DIR/secrets"
rm -rf "$OUT" && mkdir -p "$OUT"

# ----- helpers --------------------------------------------------------------------------------
_unknown_exit() { sec_unknown "$1" "$MODE" "$2" || exit 2; }

# .gitignore must keep key material out of git (secret-scanning.md acceptance).
_check_gitignore_key_material() {
  local probe
  for probe in "${KEY_MATERIAL_PROBES[@]}"; do
    git -C "$SECURITY_REPO_ROOT" check-ignore --quiet --no-index "$probe" ||
      _unknown_exit gitignore "does not ignore key material pattern: $probe"
  done
}

# ----- pr: real home paths on added doc lines --------------------------------------------------
# A plan or doc naming the author's home directory leaks the login. The control is the fixture
# diff with a login minted per run, and it must yield exactly its three planted findings.
_doc_path_leak() { (cd "$SECURITY_REPO_ROOT" && "$SECURITY_BIN_DIR/uv" run --no-project --quiet --python 3.12 python -m scripts.doc_path_leak "$@"); }

scan_doc_paths() {
  local scanner="doc-path-leak" work="$OUT/doc-path-leak" rc=0 fired findings base head
  sec_require_bin uv || { sec_unknown "$scanner" "$MODE" "uv missing"; return 2; }
  mkdir -p "$work"
  _doc_path_leak --control "$SECURITY_FIXTURES/doc-path-leak-control" >"$work/control.txt" ||
    { sec_unknown "$scanner" "$MODE" "control did not yield its planted findings: $(cat "$work/control.txt")"; return 2; }
  fired="$(grep -o 'fired([0-9]*)' "$work/control.txt")"
  base="$(sec_base_sha)" head="$(sec_head_sha)"
  _doc_path_leak --base "$base" --head "$head" --report-md "$SECURITY_WORK_DIR/report.md" >"$work/range.txt" || rc=$?
  cat "$work/range.txt"
  findings="$(grep -c ': real home path' "$work/range.txt" || true)"
  if [[ $rc -eq 0 ]]; then
    sec_row "$scanner" "$MODE" "$fired" 0 PASS "added doc lines ${base:0:9}..${head:0:9}"
  elif [[ $rc -eq 1 ]]; then
    sec_row "$scanner" "$MODE" "$fired" "$findings" FAIL "real home path on an added doc line"
    return 1
  else
    sec_unknown "$scanner" "$MODE" "doc diff not measured (exit $rc)"; return 2
  fi
}

scan_python() {
  local scanner="gitleaks"
  [[ "$MODE" == "full" ]] && scanner="trufflehog"
  sec_require_bin uv || { sec_unknown "$scanner" "$MODE" "uv missing"; return 2; }
  (cd "$SECURITY_REPO_ROOT" && "$SECURITY_BIN_DIR/uv" run --no-project --quiet --python 3.12 \
    python -m scripts.security.scan_secrets "$MODE")
}

# The Python runner receives the same paths and range that this wrapper and lib.sh use.
export SECURITY_REPO_ROOT SECURITY_WORK_DIR SECURITY_BIN_DIR SECURITY_SUMMARY_FILE SECURITY_BASE_REF
if [[ -n "${SECURITY_BASE_SHA:-}" ]]; then
  export SECURITY_BASE_SHA
fi
export SECURITY_HEAD_SHA="${SECURITY_HEAD_SHA:-}"

# ----- main -----------------------------------------------------------------------------------
_check_gitignore_key_material
if [[ "$MODE" == "pr" ]]; then
  doc_rc=0
  scan_doc_paths || doc_rc=$?
  pr_rc=0
  scan_python || pr_rc=$?
  exit $((doc_rc > pr_rc ? doc_rc : pr_rc))
elif [[ "$MODE" == "full" ]]; then
  sec_row doc-path-leak "$MODE" "not run" 0 SKIP "diff-only check, runs in pr mode"
  scan_python
fi
