#!/usr/bin/env bash
# Install the pinned security scanners into .tmp/security/bin (or $SECURITY_WORK_DIR/bin).
#
# Every release binary is pinned by version AND sha256. The sha256 values were taken
# from the GitHub release API `digest` field and cross-checked against each project's
# own published checksum file on Mon 14 Sep 2026 (zizmor publishes no checksum file,
# so its pins come from the API digest alone). A mismatch aborts loudly: a tag or
# asset that was re-pushed upstream (the Trivy pattern, Thu 19 Mar 2026) cannot
# slip through.
#
# Semgrep has no standalone binary, so it installs from PyPI into a private venv
# with `uv pip install --require-hashes` against scripts/security/semgrep-requirements.txt.
#
# Usage: scripts/security/install_scanners.sh [tool ...]   (no args = all tools)
# Tools: uv osv-scanner gitleaks trufflehog zizmor actionlint semgrep
#
# Bumping a version: change the VERSION and both SHA256 lines together, then run
# `just security-scan` so every positive control proves the new binary still sees.

# shellcheck source=scripts/security/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ALL_TOOLS=(uv osv-scanner gitleaks trufflehog zizmor actionlint semgrep)

# ----- pins -------------------------------------------------------------------
UV_VERSION="0.12.13"
UV_SHA256_linux_amd64="745765a3b6e360ad76743599ae5c42e9278c7edf8bbff9fc76d05bf2623a04dd"
UV_SHA256_darwin_arm64="7e6ddb9316acc00f2296c82ff4d99977870ee34b2f0ddcae9444d714db9364ed"

OSV_VERSION="2.6.0"
OSV_SHA256_linux_amd64="ca69b3d3cd08f889a49dc0a383122f71cc528b83803671df5fd874d97485b108"
OSV_SHA256_darwin_arm64="98c460dcd37de25819babd757d04542045b6243113e209edcd4d89fedb0256b4"

GITLEAKS_VERSION="8.30.1"
GITLEAKS_SHA256_linux_amd64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
GITLEAKS_SHA256_darwin_arm64="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"

TRUFFLEHOG_VERSION="3.97.4"
TRUFFLEHOG_SHA256_linux_amd64="dc24007c2f233bd61c05beabeb44aa27ea9b43288166279209abe0458c5ce76b"
TRUFFLEHOG_SHA256_darwin_arm64="57e2a41c1e196cf96cae49ca2151f5e9207be2f5c41349b5ea49cb5dcfc606b7"

ZIZMOR_VERSION="1.30.1"
ZIZMOR_SHA256_linux_amd64="e65324f4430c2717591937edcec90ccbefaf14c174f8ec9415e03ca875b46e1a"
ZIZMOR_SHA256_darwin_arm64="e28d22b087f9ebb8d99da6e740d348c930f559961c7c3f12badda54f882195a2"

ACTIONLINT_VERSION="1.7.12"
ACTIONLINT_SHA256_linux_amd64="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
ACTIONLINT_SHA256_darwin_arm64="aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f"

SEMGREP_REQUIREMENTS="$SECURITY_SCRIPTS/semgrep-requirements.txt"

# ----- helpers ------------------------------------------------------------------
_platform() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  if [[ "$os" == "Linux" && "$arch" == "x86_64" ]]; then
    echo linux_amd64
  elif [[ "$os" == "Darwin" && "$arch" == "arm64" ]]; then
    echo darwin_arm64
  else
    printf '[ERROR] unsupported platform %s/%s: add pins for it\n' "$os" "$arch" >&2
    exit 1
  fi
}

_sha256_of() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    printf '[ERROR] neither sha256sum nor shasum found\n' >&2
    exit 1
  fi
}

# _fetch_verified <url> <expected-sha256> <dest-file>
_fetch_verified() {
  local url="$1" expected="$2" dest="$3" actual
  curl --fail --silent --show-error --location --retry 3 --output "$dest" "$url"
  actual="$(_sha256_of "$dest")"
  if [[ "$actual" != "$expected" ]]; then
    printf '[ERROR] sha256 MISMATCH for %s\n  expected %s\n  actual   %s\n' "$url" "$expected" "$actual" >&2
    rm -f "$dest"
    exit 1
  fi
}

# Skip a download when the installed binary was produced from the same pinned hash.
_already_installed() {
  local tool="$1" sha="$2"
  [[ -x "$SECURITY_BIN_DIR/$tool" && -f "$SECURITY_BIN_DIR/.$tool.sha256" &&
    "$(cat "$SECURITY_BIN_DIR/.$tool.sha256")" == "$sha" ]]
}

# _install_release <tool> <url> <sha256> <member-in-tarball|"-" for a bare binary>
_install_release() {
  local tool="$1" url="$2" sha="$3" member="$4" tmp
  if _already_installed "$tool" "$sha"; then
    sec_log "$tool already installed (pinned sha256 matches)"
    return 0
  fi
  tmp="$(mktemp -d)"
  _fetch_verified "$url" "$sha" "$tmp/download"
  if [[ "$member" == "-" ]]; then
    install -m 0755 "$tmp/download" "$SECURITY_BIN_DIR/$tool"
  else
    tar -xzf "$tmp/download" -C "$tmp" "$member"
    install -m 0755 "$tmp/$member" "$SECURITY_BIN_DIR/$tool"
  fi
  echo "$sha" >"$SECURITY_BIN_DIR/.$tool.sha256"
  rm -rf "$tmp"
  sec_log "$tool installed and sha256-verified ($url)"
}

# _pin <PREFIX>: echo the sha256 variable for this platform.
_pin() { local var="${1}_SHA256_${PLATFORM}"; echo "${!var}"; }

# ----- installers ---------------------------------------------------------------------------
install_uv() {
  local triple
  if [[ "$PLATFORM" == "linux_amd64" ]]; then triple="x86_64-unknown-linux-gnu"; elif [[ "$PLATFORM" == "darwin_arm64" ]]; then triple="aarch64-apple-darwin"; fi
  _install_release uv "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$triple.tar.gz" \
    "$(_pin UV)" "uv-$triple/uv"
}

install_osv_scanner() {
  _install_release osv-scanner \
    "https://github.com/google/osv-scanner/releases/download/v$OSV_VERSION/osv-scanner_$PLATFORM" \
    "$(_pin OSV)" "-"
}

install_gitleaks() {
  local asset="$PLATFORM"
  if [[ "$PLATFORM" == "linux_amd64" ]]; then asset="linux_x64"; elif [[ "$PLATFORM" == "darwin_arm64" ]]; then asset="darwin_arm64"; fi
  _install_release gitleaks \
    "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_$asset.tar.gz" \
    "$(_pin GITLEAKS)" "gitleaks"
}

install_trufflehog() {
  _install_release trufflehog \
    "https://github.com/trufflesecurity/trufflehog/releases/download/v$TRUFFLEHOG_VERSION/trufflehog_${TRUFFLEHOG_VERSION}_$PLATFORM.tar.gz" \
    "$(_pin TRUFFLEHOG)" "trufflehog"
}

install_zizmor() {
  local triple
  if [[ "$PLATFORM" == "linux_amd64" ]]; then triple="x86_64-unknown-linux-gnu"; elif [[ "$PLATFORM" == "darwin_arm64" ]]; then triple="aarch64-apple-darwin"; fi
  _install_release zizmor \
    "https://github.com/zizmorcore/zizmor/releases/download/v$ZIZMOR_VERSION/zizmor-$triple.tar.gz" \
    "$(_pin ZIZMOR)" "zizmor"
}

install_actionlint() {
  _install_release actionlint \
    "https://github.com/rhysd/actionlint/releases/download/v$ACTIONLINT_VERSION/actionlint_${ACTIONLINT_VERSION}_$PLATFORM.tar.gz" \
    "$(_pin ACTIONLINT)" "actionlint"
}

install_semgrep() {
  local venv="$SECURITY_WORK_DIR/semgrep-venv" req_sha
  req_sha="$(_sha256_of "$SEMGREP_REQUIREMENTS")"
  if _already_installed semgrep "$req_sha"; then
    sec_log "semgrep already installed (requirements sha256 matches)"
    return 0
  fi
  install_uv
  rm -rf "$venv"
  "$SECURITY_BIN_DIR/uv" venv --quiet --python 3.12 "$venv"
  # --require-hashes: every wheel, transitive ones included, must match a pinned sha256.
  VIRTUAL_ENV="$venv" "$SECURITY_BIN_DIR/uv" pip install --quiet --require-hashes \
    --no-deps -r "$SEMGREP_REQUIREMENTS"
  ln -sf "$venv/bin/semgrep" "$SECURITY_BIN_DIR/semgrep"
  echo "$req_sha" >"$SECURITY_BIN_DIR/.semgrep.sha256"
  sec_log "semgrep installed from hash-pinned wheels ($("$SECURITY_BIN_DIR/semgrep" --version))"
}

# ----- main ---------------------------------------------------------------------------
PLATFORM="$(_platform)"
mkdir -p "$SECURITY_BIN_DIR"
tools=("$@")
[[ ${#tools[@]} -eq 0 ]] && tools=("${ALL_TOOLS[@]}")
for tool in "${tools[@]}"; do
  case "$tool" in
    uv) install_uv ;;
    osv-scanner) install_osv_scanner ;;
    gitleaks) install_gitleaks ;;
    trufflehog) install_trufflehog ;;
    zizmor) install_zizmor ;;
    actionlint) install_actionlint ;;
    semgrep) install_semgrep ;;
    *) printf '[ERROR] unknown tool: %s (known: %s)\n' "$tool" "${ALL_TOOLS[*]}" >&2; exit 64 ;;
  esac
done
