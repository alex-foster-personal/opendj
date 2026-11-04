#!/usr/bin/env bash
# Secret scanning. Spec: docs/security/secret-scanning.md
#
# Usage: scripts/security/scan_secrets.sh pr|full
#   pr    gitleaks over the commits between the base and HEAD (what a PR adds).
#   full  trufflehog over the whole git history, reporting verified live secrets
#         (FAIL) and secrets whose verification errored (UNKNOWN, never clean).
#         Materializes missing blobs first so file:// clone works on a pool partial
#         clone (#4224).
#   doc-path-leak (pr only): no added line under specs/, app_docs/, docs/ or .planning/
#         names a real home directory (scripts/doc_path_leak.py). full: SKIP.
#
# Positive controls run in a throwaway git repo holding a synthetic GitHub-PAT-shaped
# token generated at runtime (random hex, never a real credential, never committed
# to this repo). trufflehog's control runs with verification off, because a
# verified hit would need a live secret: it proves detection, not verification.

# shellcheck source=scripts/security/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:?usage: scan_secrets.sh pr|full}"
sec_require_mode "$MODE"
GITLEAKS_CONFIG="$SECURITY_REPO_ROOT/.gitleaks.toml"
KEY_MATERIAL_PROBES=(x.pem x.key x.p12 x.p8 x.mobileprovision credentials.json service-account-x.json)
# trufflehog path suppressions, one per entry: regex|expires (YYYY-MM-DD)|reason.
# These candidates fail verification by design (placeholders), which would keep the
# weekly scan UNKNOWN forever. An expired entry turns the scan UNKNOWN until renewed.
TRUFFLEHOG_PATH_SUPPRESSIONS=(
  '^\.planning/bifrost2-handoff/sync-kit/[^/]+/skills/cloudflare/references/hyperdrive/|2026-12-13|vendored Cloudflare Hyperdrive docs: postgres:// examples with placeholder host and password'
  '^\.pnpm-store/|2026-12-13|pnpm store committed by mistake, history only: third-party package files'
  '^apps/webui/frontend/tests/unit/progress-repo-base\.test\.mjs$|2026-12-13|code-comment example URL (x-access-token placeholder, host "host")'
  '^\.xdg-cache/|2027-01-13|corepack cache committed by mistake, history only: pnpm.mjs docs example of a proxy URL with a placeholder user and password'
)
# trufflehog VALUE suppressions, same format: a regex over the candidate's Raw value
# (no `|` inside a regex: it is the field separator).
# For placeholders that recur across files and commit messages, where a path cannot
# name them all. Applied to UNVERIFIED candidates only (secscan.py trufflehog-summary):
# a verified hit is never filtered, whatever it looks like.
TRUFFLEHOG_VALUE_SUPPRESSIONS=(
  '^https?://opendj\.localhost:[0-9]+@attacker\.example$|2027-01-13|open-redirect test vector: userinfo trick, the "password" is a port number (opener scope, ADR, open-in-browser tests, commit messages)'
  '^https?://opendj\.localhost:[0-9]+@evil\.example\.com$|2027-01-13|open-redirect test vector in tests/webui/test_auth.py: the "password" is a port number'
  '^https://x-access-token:ghs_redacted@github\.com$|2027-01-13|redaction placeholder: git-remote-identity and enqueue-gate tests, .test_durations test ids'
  '^https://x-access-token:ghs_\.\.\.@github\.com$|2027-01-13|elided-token placeholder in commit messages (5e20b6de9, 040e7ccdd)'
)
OUT="$SECURITY_WORK_DIR/secrets"
rm -rf "$OUT" && mkdir -p "$OUT"

# ----- helpers --------------------------------------------------------------------------------
_unknown_exit() { sec_unknown "$1" "$MODE" "$2" || exit 2; }

# A throwaway repo with one commit containing a synthetic token. Prints its path.
_control_repo() {
  local repo="$OUT/control-repo" token
  token="ghp_$(openssl rand -hex 18)"
  git init --quiet "$repo"
  printf 'GITHUB_TOKEN=%s\n' "$token" >"$repo/deploy.env"
  git -C "$repo" add deploy.env
  git -C "$repo" -c user.name=control -c user.email=control@example.invalid \
    commit --quiet -m "control: synthetic token"
  echo "$repo"
}

# _trufflehog_exclude_file <out> <entry...>: writes the regexes of the unexpired
# entries to <out>; an expired one is UNKNOWN. (No nameref: macOS ships bash 3.2.)
_trufflehog_exclude_file() {
  local out="$1" entry regex expires today
  shift
  today="$(date -u +%F)"
  : >"$out"
  for entry in "$@"; do
    regex="${entry%%|*}"
    expires="$(printf '%s' "$entry" | cut -d'|' -f2)"
    [[ "$expires" > "$today" ]] || _unknown_exit trufflehog "suppression expired $expires: $regex"
    printf '%s\n' "$regex" >>"$out"
  done
}

# .gitignore must keep key material out of git (secret-scanning.md acceptance).
_check_gitignore_key_material() {
  local probe
  for probe in "${KEY_MATERIAL_PROBES[@]}"; do
    git -C "$SECURITY_REPO_ROOT" check-ignore --quiet --no-index "$probe" ||
      _unknown_exit gitignore "does not ignore key material pattern: $probe"
  done
}

# ----- pr: gitleaks over the PR commit range ----------------------------------------------------
scan_pr() {
  local scanner="gitleaks" repo rc=0 base head commits
  sec_require_bin gitleaks || _unknown_exit "$scanner" "binary missing"
  repo="$(_control_repo)"
  "$SECURITY_BIN_DIR/gitleaks" git --no-banner --redact --config "$GITLEAKS_CONFIG" \
    --report-format json --report-path "$OUT/control.json" --exit-code 1 "$repo" || rc=$?
  [[ $rc -eq 1 ]] || _unknown_exit "$scanner" "control did not fire (exit $rc, expected 1)"
  sec_py gitleaks-summary "$OUT/control.json" --count-file "$OUT/control.count" >/dev/null ||
    _unknown_exit "$scanner" "control report unparseable"
  local control_hits
  control_hits="$(cat "$OUT/control.count")"
  [[ "$control_hits" -gt 0 ]] || _unknown_exit "$scanner" "control did not fire (0 findings)"

  base="$(sec_base_sha)"
  head="$(sec_head_sha)"
  commits="$(git -C "$SECURITY_REPO_ROOT" rev-list --count "$base..$head")"
  if [[ "$commits" -eq 0 ]]; then
    sec_row "$scanner" "$MODE" "fired($control_hits)" 0 SKIP "no commits in ${base:0:9}..${head:0:9}"
    return 0
  fi
  rc=0
  "$SECURITY_BIN_DIR/gitleaks" git --no-banner --redact --config "$GITLEAKS_CONFIG" \
    --log-opts="$base..$head" --report-format json --report-path "$OUT/range.json" \
    --exit-code 1 "$SECURITY_REPO_ROOT" || rc=$?
  [[ $rc -eq 0 || $rc -eq 1 ]] || _unknown_exit "$scanner" "range scan errored (exit $rc)"
  sec_py gitleaks-summary "$OUT/range.json" --count-file "$OUT/range.count" \
    --report-md "$SECURITY_WORK_DIR/report.md" --title "gitleaks: ${base:0:9}..${head:0:9}" ||
    _unknown_exit "$scanner" "range report unparseable"
  local findings
  findings="$(cat "$OUT/range.count")"
  if [[ "$findings" -eq 0 ]]; then
    sec_row "$scanner" "$MODE" "fired($control_hits)" 0 PASS "$commits commit(s) ${base:0:9}..${head:0:9}"
  else
    sec_row "$scanner" "$MODE" "fired($control_hits)" "$findings" FAIL "$commits commit(s) ${base:0:9}..${head:0:9}"
    return 1
  fi
}

# ----- full: trufflehog over the whole history --------------------------------------------------
scan_full() {
  local scanner="trufflehog" repo rc=0
  sec_require_bin trufflehog || _unknown_exit "$scanner" "binary missing"
  repo="$(_control_repo)"
  "$SECURITY_BIN_DIR/trufflehog" git "file://$repo" --no-verification --json --no-update \
    >"$OUT/control.jsonl" || rc=$?
  [[ $rc -eq 0 ]] || _unknown_exit "$scanner" "control scan errored (exit $rc)"
  sec_py trufflehog-summary "$OUT/control.jsonl" --count-file "$OUT/control.count" >/dev/null ||
    _unknown_exit "$scanner" "control output unparseable"
  local control_hits
  control_hits="$(cat "$OUT/control.count")"
  [[ "$control_hits" -gt 0 ]] || _unknown_exit "$scanner" "control did not fire (0 detections)"

  sec_materialize_missing_objects "$SECURITY_REPO_ROOT" || \
    _unknown_exit "$scanner" "could not materialize git objects for file:// history scan (partial clone; set GH_TOKEN or GITHUB_TOKEN)"

  # --results=verified,unknown: verified = live secret (FAIL); unknown = verification
  # errored (network/auth), which is UNKNOWN rather than clean.
  _trufflehog_exclude_file "$OUT/exclude-paths.txt" "${TRUFFLEHOG_PATH_SUPPRESSIONS[@]}"
  _trufflehog_exclude_file "$OUT/suppress-values.txt" "${TRUFFLEHOG_VALUE_SUPPRESSIONS[@]}"
  rc=0
  "$SECURITY_BIN_DIR/trufflehog" git "file://$SECURITY_REPO_ROOT" --results=verified,unknown \
    --exclude-paths "$OUT/exclude-paths.txt" --json --no-update >"$OUT/history.jsonl" || rc=$?
  [[ $rc -eq 0 ]] || _unknown_exit "$scanner" "history scan errored (exit $rc)"
  sec_py trufflehog-summary "$OUT/history.jsonl" --suppress-raw "$OUT/suppress-values.txt" \
    --count-file "$OUT/history.count" \
    --report-md "$SECURITY_WORK_DIR/report.md" --title "trufflehog: full history (verified + unverifiable)" \
    >"$OUT/history.txt" || _unknown_exit "$scanner" "history output unparseable"
  cat "$OUT/history.txt"
  local findings unverifiable
  findings="$(cat "$OUT/history.count")"
  unverifiable="$(grep -c ' unverified ' "$OUT/history.txt" || true)"
  if [[ "$findings" -eq 0 ]]; then
    sec_row "$scanner" "$MODE" "fired($control_hits)" 0 PASS "full history, verified live secrets only"
  elif [[ "$unverifiable" -eq "$findings" ]]; then
    sec_unknown "$scanner" "$MODE" "$findings candidate(s) whose verification errored" || return 2
  else
    sec_row "$scanner" "$MODE" "fired($control_hits)" "$findings" FAIL "verified live secret(s) in history: rotate first"
    return 1
  fi
}

# ----- pr: real home paths on added doc lines ------------------------------------------------------
# A plan or doc naming the author's home directory leaks the login; Codex held 6 of 9 merge-head
# PRs for it on Wed 4 Nov 2026. The control is the fixture diff with a login minted per run, and
# it must yield exactly its three planted findings.
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

# ----- main --------------------------------------------------------------------------------------
_check_gitignore_key_material
if [[ "$MODE" == "pr" ]]; then
  doc_rc=0
  scan_doc_paths || doc_rc=$?
  pr_rc=0
  scan_pr || pr_rc=$?
  exit $((doc_rc > pr_rc ? doc_rc : pr_rc))
elif [[ "$MODE" == "full" ]]; then
  sec_row doc-path-leak "$MODE" "not run" 0 SKIP "diff-only check, runs in pr mode"
  scan_full
fi
