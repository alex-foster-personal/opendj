#!/usr/bin/env bash
# Secret scanning. Spec: docs/security/secret-scanning.md
#
# Usage: scripts/security/scan_secrets.sh pr|full
#   pr    gitleaks over the commits between the base and HEAD (what a PR adds).
#   full  trufflehog over the whole git history, reporting verified live secrets
#         (FAIL) and secrets whose verification errored (UNKNOWN, never clean).
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

# Writes the regexes of unexpired path suppressions to $1; an expired one is UNKNOWN.
_trufflehog_exclude_file() {
  local out="$1" entry regex expires today
  today="$(date -u +%F)"
  : >"$out"
  for entry in "${TRUFFLEHOG_PATH_SUPPRESSIONS[@]}"; do
    regex="${entry%%|*}"
    expires="$(printf '%s' "$entry" | cut -d'|' -f2)"
    [[ "$expires" > "$today" ]] || _unknown_exit trufflehog "path suppression expired $expires: $regex"
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

  # --results=verified,unknown: verified = live secret (FAIL); unknown = verification
  # errored (network/auth), which is UNKNOWN rather than clean.
  _trufflehog_exclude_file "$OUT/exclude-paths.txt"
  rc=0
  "$SECURITY_BIN_DIR/trufflehog" git "file://$SECURITY_REPO_ROOT" --results=verified,unknown \
    --exclude-paths "$OUT/exclude-paths.txt" --json --no-update >"$OUT/history.jsonl" || rc=$?
  [[ $rc -eq 0 ]] || _unknown_exit "$scanner" "history scan errored (exit $rc)"
  sec_py trufflehog-summary "$OUT/history.jsonl" --count-file "$OUT/history.count" \
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

# ----- main --------------------------------------------------------------------------------------
_check_gitignore_key_material
if [[ "$MODE" == "pr" ]]; then
  scan_pr
elif [[ "$MODE" == "full" ]]; then
  scan_full
fi
