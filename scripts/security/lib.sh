# Shared helpers for scripts/security/*.sh. Sourced, never executed directly.
#
# Status vocabulary (one row per scanner in the summary table):
#   PASS     scanner ran, control fired, zero findings in scope
#   FAIL     scanner ran, control fired, findings in scope
#   UNKNOWN  scanner could not measure (missing binary, error exit, control silent,
#            nothing parsed). Never rendered as clean. Exits 2.
#   SKIP     not applicable in this mode (e.g. PR touched no lockfile). Says why.
#
# Exit codes of every scan_*.sh: 0 = PASS/SKIP, 1 = FAIL, 2 = UNKNOWN.

set -euo pipefail

# ----- config (env overrides are explicit, never silent) ----------------------
SECURITY_REPO_ROOT="$(git rev-parse --show-toplevel)"
SECURITY_WORK_DIR="${SECURITY_WORK_DIR:-$SECURITY_REPO_ROOT/.tmp/security}"
SECURITY_BIN_DIR="$SECURITY_WORK_DIR/bin"
SECURITY_SUMMARY_FILE="${SECURITY_SUMMARY_FILE:-$SECURITY_WORK_DIR/summary.tsv}"
SECURITY_FIXTURES="$SECURITY_REPO_ROOT/tests/fixtures/security"
SECURITY_SCRIPTS="$SECURITY_REPO_ROOT/scripts/security"
SECURITY_BASE_REF="${SECURITY_BASE_REF:-origin/main}"
export PATH="$SECURITY_BIN_DIR:$PATH"
mkdir -p "$SECURITY_WORK_DIR"

# ----- logging ----------------------------------------------------------------
sec_log() { printf '[security] %s\n' "$*" >&2; }

# ----- summary rows -----------------------------------------------------------
# sec_row <scanner> <mode> <control> <findings> <status> <detail>
sec_row() {
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$6" >>"$SECURITY_SUMMARY_FILE"
}

# sec_unknown <scanner> <mode> <message>: record + print UNKNOWN, return 2.
sec_unknown() {
  printf 'UNKNOWN: %s %s\n' "$1" "$3" >&2
  sec_row "$1" "$2" "?" "?" "UNKNOWN" "$3"
  return 2
}

sec_require_bin() {
  local tool="$1"
  if [[ ! -x "$SECURITY_BIN_DIR/$tool" ]]; then
    printf 'UNKNOWN: %s is not installed in %s; run scripts/security/install_scanners.sh %s\n' \
      "$tool" "$SECURITY_BIN_DIR" "$tool" >&2
    return 2
  fi
}

sec_require_mode() {
  case "$1" in
    pr | full) ;;
    *) printf '[ERROR] mode must be pr or full, got: %s\n' "$1" >&2; exit 64 ;;
  esac
}

# ----- git range ----------------------------------------------------------------
# PR mode compares HEAD against a base. CI passes SECURITY_BASE_SHA explicitly
# (the PR's base commit); locally it is the merge-base with SECURITY_BASE_REF.
sec_head_sha() { git -C "$SECURITY_REPO_ROOT" rev-parse "${SECURITY_HEAD_SHA:-HEAD}"; }

sec_base_sha() {
  local head
  head="$(sec_head_sha)"
  if [[ -n "${SECURITY_BASE_SHA:-}" ]]; then
    git -C "$SECURITY_REPO_ROOT" merge-base "$SECURITY_BASE_SHA" "$head"
  else
    git -C "$SECURITY_REPO_ROOT" merge-base "$SECURITY_BASE_REF" "$head"
  fi
}

# Files changed between the base and HEAD (committed changes only).
sec_changed_files() { git -C "$SECURITY_REPO_ROOT" diff --name-only "$(sec_base_sha)" "$(sec_head_sha)"; }

# ----- python helper (stdlib only, needs 3.11+ for tomllib) ---------------------
sec_py() {
  "$SECURITY_BIN_DIR/uv" run --no-project --quiet --python 3.12 \
    python "$SECURITY_SCRIPTS/secscan.py" "$@"
}
