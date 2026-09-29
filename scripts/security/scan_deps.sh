#!/usr/bin/env bash
# Dependency vulnerability scan (osv-scanner). Spec: docs/security/dependency-scanning.md
#
# Usage: scripts/security/scan_deps.sh pr|full
#   pr    only vulnerabilities NEW versus the base commit, in manifests the PR changed.
#         SKIP (with reason) when no manifest changed: base and head then resolve the
#         same versions, so there is nothing new to find.
#   full  every manifest at HEAD; any finding not in osv-scanner.toml IgnoredVulns fails.
#
# Always, before scanning: the manifest inventory must match git (a new lockfile that
# nobody added to MANIFESTS fails as UNKNOWN), osv-scanner.toml must lint (reason +
# ignoreUntil <= 90 days on every entry), and the positive control must fire.

# shellcheck source=scripts/security/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:?usage: scan_deps.sh pr|full}"
sec_require_mode "$MODE"
SCANNER="osv-scanner"
MANIFESTS=(
  uv.lock
  requirements.txt
  requirements-docs.txt
  ops/quality/requirements.txt
  ops/quality/mypy-requirements.txt
  ops/fleet/requirements-duplicate-writer.txt
  scripts/security/semgrep-requirements.txt
  apps/webui/frontend/pnpm-lock.yaml
  apps/desktop/pnpm-lock.yaml
  apps/launcher/pnpm-lock.yaml
  apps/desktop/src-tauri/Cargo.lock
  apps/webui/server/native/waveform/Cargo.lock
  package-lock.json
)
CONFIG="$SECURITY_REPO_ROOT/osv-scanner.toml"
CONTROL="tests/fixtures/security/osv-control/requirements.txt"
MAX_IGNORE_DAYS=90
OUT="$SECURITY_WORK_DIR/deps"

# ----- helpers ------------------------------------------------------------------------------
# _osv_scan <json-out> <root-dir> <manifest...>: exit 0/1 are measurements, anything else is not.
_osv_scan() {
  local json="$1" root="$2" rc=0
  shift 2
  local args=()
  for manifest in "$@"; do args+=(-L "$root/$manifest"); done
  "$SECURITY_BIN_DIR/osv-scanner" scan source --config="$CONFIG" --format=json --all-packages \
    "${args[@]}" >"$json" || rc=$?
  if [[ $rc -ne 0 && $rc -ne 1 ]]; then
    echo "osv-scanner exited $rc (127 = scanner error, 128 = no packages found)" >&2
    return 2
  fi
}

_unknown_exit() { sec_unknown "$SCANNER" "$MODE" "$1" || exit 2; }

# ----- preflight ------------------------------------------------------------------------------
sec_require_bin osv-scanner || _unknown_exit "binary missing"
sec_require_bin uv || _unknown_exit "uv missing (needed for secscan.py)"
rm -rf "$OUT" && mkdir -p "$OUT"
sec_py inventory-check --root "$SECURITY_REPO_ROOT" --expect "${MANIFESTS[@]}" ||
  _unknown_exit "manifest inventory drifted from git (see errors above)"
sec_py osv-lint-config "$CONFIG" --max-days "$MAX_IGNORE_DAYS" ||
  _unknown_exit "osv-scanner.toml failed lint (see errors above)"

# ----- select manifests -------------------------------------------------------------------------
if [[ "$MODE" == "pr" ]]; then
  changed=()
  while IFS= read -r path; do changed+=("$path"); done < <(sec_changed_files)
  targets=()
  for manifest in "${MANIFESTS[@]}"; do
    for path in ${changed[@]+"${changed[@]}"}; do [[ "$path" == "$manifest" ]] && targets+=("$manifest"); done
  done
  if [[ ${#targets[@]} -eq 0 ]]; then
    sec_row "$SCANNER" "$MODE" "not run" 0 SKIP "no manifest changed vs $(sec_base_sha | cut -c1-9)"
    exit 0
  fi
elif [[ "$MODE" == "full" ]]; then
  targets=("${MANIFESTS[@]}")
fi

# ----- positive control: must find the known-vulnerable pin ------------------------------------
_osv_scan "$OUT/control.json" "$SECURITY_REPO_ROOT" "$CONTROL" || _unknown_exit "control scan errored"
sec_py osv-check "$OUT/control.json" --root "$SECURITY_REPO_ROOT" --expect "$CONTROL" \
  --count-file "$OUT/control.count" --title "osv-scanner control" >/dev/null ||
  _unknown_exit "control output unparseable"
control_hits="$(cat "$OUT/control.count")"
[[ "$control_hits" -gt 0 ]] || _unknown_exit "control did not fire ($CONTROL)"

# ----- scan -------------------------------------------------------------------------------------
_osv_scan "$OUT/head.json" "$SECURITY_REPO_ROOT" "${targets[@]}" || _unknown_exit "head scan errored"
sec_py osv-check "$OUT/head.json" --root "$SECURITY_REPO_ROOT" --expect "${targets[@]}" \
  --count-file "$OUT/head.count" --title "osv-scanner head" >"$OUT/head.txt" ||
  _unknown_exit "head scan measured nothing"

if [[ "$MODE" == "pr" ]]; then
  base="$(sec_base_sha)"
  mkdir -p "$OUT/base"
  # Manifests the PR adds do not exist on base; everything in them is new.
  on_base=()
  while IFS= read -r path; do on_base+=("$path"); done < <(
    git -C "$SECURITY_REPO_ROOT" ls-tree -r --name-only "$base" -- "${targets[@]}")
  if [[ ${#on_base[@]} -gt 0 ]]; then
    git -C "$SECURITY_REPO_ROOT" archive "$base" -- "${on_base[@]}" | tar -x -C "$OUT/base"
    _osv_scan "$OUT/base.json" "$OUT/base" "${on_base[@]}" || _unknown_exit "base scan errored"
  else
    echo '{"results": []}' >"$OUT/base.json"
  fi
  sec_py osv-diff --base "$OUT/base.json" --head "$OUT/head.json" --root "$SECURITY_REPO_ROOT" \
    --base-root "$OUT/base" --count-file "$OUT/findings.count" --report-md "$SECURITY_WORK_DIR/report.md" \
    --title "osv-scanner: vulnerabilities new vs ${base:0:9}" || _unknown_exit "diff failed"
  detail="${#targets[@]} changed manifest(s) vs ${base:0:9}"
elif [[ "$MODE" == "full" ]]; then
  cp "$OUT/head.count" "$OUT/findings.count"
  sed -n '2,$p' "$OUT/head.txt"
  { printf '\n### osv-scanner: unignored vulnerabilities at %s (%s)\n\n' "$(sec_head_sha | cut -c1-9)" "$(cat "$OUT/head.count")"
    sed -n '2,$p' "$OUT/head.txt" | sed 's/^/- `/; s/$/`/'; } >>"$SECURITY_WORK_DIR/report.md"
  detail="${#targets[@]} manifests"
fi

findings="$(cat "$OUT/findings.count")"
if [[ "$findings" -eq 0 ]]; then
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" 0 PASS "$detail"
else
  sec_row "$SCANNER" "$MODE" "fired($control_hits)" "$findings" FAIL "$detail"
  exit 1
fi
