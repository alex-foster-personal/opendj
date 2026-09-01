#!/usr/bin/env bash
#
# Every precondition `just dmg` needs, checked in ONE pass.
#
# WHY THIS EXISTS. The recipe used to discover its preconditions one at a
# time: a dirty tree, then the signing configuration, then an unbuilt SPA,
# then the pnpm toolchain pin. Each was a separate failed attempt, and each
# attempt costs the operator a round trip to find out one more thing. None of
# these four checks needs any of the others to have passed first, so they all
# run and EVERY unmet precondition is reported before the run stops.
#
# READ-ONLY, ALWAYS. It builds nothing, installs nothing and writes nothing,
# so it is safe to run at any point, including from
# `scripts/ship_dmg.sh --dry-run`.
#
# COST: about 8 seconds on an idle Air, and roughly 6 of those are the single
# `pnpm --version`, which corepack answers by starting node twice to resolve
# the pinned version (and which may consult the network the first time it
# resolves a version it has never seen). That is the price of measuring the
# toolchain that will actually build the SPA rather than the one on PATH.
# Nothing else here takes more than a second.
#
# WHAT IT DELIBERATELY DOES NOT DO. It does not fix anything for you and it
# does not rebuild the SPA: a builder that quietly rebuilds its own inputs
# hides the fact that the operator packaged a tree they had not built.
#
# A CHECK THAT CANNOT MEASURE REPORTS A FAILURE, NEVER A PASS. An unreadable
# working tree, a missing package.json and an absent freshness checker are all
# reported as unmet preconditions naming what could not be measured. Silence
# from a broken probe reads exactly like a clean result, so it is never
# allowed to.
#
# Usage:
#   scripts/dmg_preflight.sh      direct, uses the ambient environment
#   just dmg-preflight            same checks, plus the justfile's .env load
#
# Exit codes:
#   0   every precondition met, `just dmg` can proceed
#   1   one or more preconditions unmet (all of them listed)
#   2   usage error
#
# ENVIRONMENT
#   MDT_REPO_ROOT                       tree to check (default: this repo)
#   MDT_MACOS_SIGNING_IDENTITY          Developer ID identity to sign with
#   MDT_MACOS_NOTARY_KEYCHAIN_PROFILE   notarytool keychain profile
#   MDT_SHIP_UNSIGNED                   1 = deliberately unsigned dev image
#
# -Claude
set -euo pipefail

case "${1:-}" in
    "") ;;
    -h|--help) sed -n '2,47p' "$0"; exit 0 ;;
    *) echo "[ERROR] unknown argument '${1}'. Run with --help." >&2; exit 2 ;;
esac

#----- configuration ------------------------------------------------------

# SELF_ROOT is where the INSTRUMENTS live (the SPA freshness checker this
# shares with the payload builder). ROOT is the tree under TEST. They are the
# same path in every real run and differ only under MDT_REPO_ROOT, which is
# how a test can point the real checks at a throwaway tree.
SELF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${MDT_REPO_ROOT:-$SELF_ROOT}"

FRONTEND_DIR="$ROOT/apps/webui/frontend"
FRONTEND_PACKAGE_JSON="$FRONTEND_DIR/package.json"
DIRTY_FILES_SHOWN=10

FAILURES=0
FAILED_NAMES=""

#----- reporting ----------------------------------------------------------

ok()      { printf '[OK]      %s\n' "$1"; }
section() { printf '\n=== %s ===\n' "$1"; }

# One unmet precondition. Named so the summary can list what to fix without
# the operator scrolling back, and counted so nothing can report a pass while
# a check has failed.
fail() { # <check name> <what is wrong> <how to fix it>
    printf '[ERROR]   %s\n' "$2"
    printf '          FIX: %s\n' "$3"
    FAILURES=$((FAILURES + 1))
    FAILED_NAMES="${FAILED_NAMES:+$FAILED_NAMES, }$1"
}

#----- A. host platform ---------------------------------------------------

check_platform() {
    section "A. host platform"
    local kernel machine
    kernel="$(uname -s)"
    machine="$(uname -m)"
    if [ "$kernel" != "Darwin" ]; then
        fail "host platform" "host is $kernel, not macOS" \
             "The dmg bundles a macOS .app and needs hdiutil and codesign. Build it on the Air or silver."
        return
    fi
    if [ "$machine" != "arm64" ]; then
        fail "host platform" "host is $machine, and the artifact is arm64 only" \
             "Build on Apple silicon. An Intel Mac cannot produce or run this image (v1 decision)."
        return
    fi
    ok "host is macOS on $machine"
}

#----- B. working tree ----------------------------------------------------

check_working_tree() {
    section "B. working tree"
    local dirty status
    # A failure to READ the tree is reported as a failure, never as clean:
    # `git status` on a non-repository prints nothing to stdout and exits
    # nonzero, which is indistinguishable from a clean tree if you only look
    # at the output.
    if ! dirty="$(git -C "$ROOT" status --porcelain 2>&1)"; then
        status=$?
        fail "working tree" "cannot read the working tree at $ROOT (git exited $status): $dirty" \
             "Run the preflight from inside the repository, or point MDT_REPO_ROOT at it."
        return
    fi
    if [ -n "$dirty" ]; then
        local count
        count="$(printf '%s\n' "$dirty" | wc -l | tr -d ' ')"
        fail "working tree" "working tree is dirty ($count path(s)); release builds are cut from clean trees only" \
             "Commit the work, or build from a clean worktree. The build stamp records the commit, so a dirty tree ships an artifact no commit describes."
        printf '%s\n' "$dirty" | head -"$DIRTY_FILES_SHOWN" | sed 's/^/          /'
        if [ "$count" -gt "$DIRTY_FILES_SHOWN" ]; then
            printf '          ... and %s more\n' "$((count - DIRTY_FILES_SHOWN))"
        fi
        return
    fi
    ok "working tree is clean"
}

#----- C. signing configuration -------------------------------------------

check_signing_config() {
    section "C. signing configuration"
    local identity notary ship_unsigned
    identity="${MDT_MACOS_SIGNING_IDENTITY:-}"
    notary="${MDT_MACOS_NOTARY_KEYCHAIN_PROFILE:-}"
    ship_unsigned="${MDT_SHIP_UNSIGNED:-}"

    # ONE diagnosis per configuration, in a fixed order. The states overlap
    # (an empty identity satisfies both the notary guard and the default
    # refusal), and reporting two messages for one root cause would read as
    # two problems. INSTALL-04 pins the notary message as the one a
    # half-configured build reports, so that branch stays FIRST.
    if [ -n "$notary" ] && [ -z "$identity" ]; then
        fail "signing configuration" \
             "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is set but MDT_MACOS_SIGNING_IDENTITY is not; notarization needs a Developer ID signature" \
             "Set MDT_MACOS_SIGNING_IDENTITY to the Developer ID identity that profile notarizes for, or unset the profile."
        return
    fi
    if [ "$ship_unsigned" = "1" ] && [ -n "$identity" ]; then
        fail "signing configuration" \
             "MDT_SHIP_UNSIGNED=1 and MDT_MACOS_SIGNING_IDENTITY are both set" \
             "Unset one: the identity to build deliberately unsigned, or MDT_SHIP_UNSIGNED to sign. Guessing which was meant is how a mislabeled artifact ships."
        return
    fi
    if [ -z "$identity" ] && [ "$ship_unsigned" != "1" ]; then
        fail "signing configuration" \
             "no Developer ID signing identity. THE DEFAULT IS REFUSAL, so an unsigned image is never fallen into" \
             "Set MDT_MACOS_SIGNING_IDENTITY and MDT_MACOS_NOTARY_KEYCHAIN_PROFILE to sign and notarize (see the dmg recipe header and docs/operator-setup.md), or export MDT_SHIP_UNSIGNED=1 to build a deliberately unsigned dev image that testers must clear by hand."
        printf '          identities on this machine:\n'
        security find-identity -v -p codesigning 2>&1 | sed 's/^/          /'
        return
    fi
    if [ -n "$identity" ] && [ -z "$notary" ]; then
        fail "signing configuration" \
             "MDT_MACOS_SIGNING_IDENTITY is set but MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is not. A Developer ID signature that is never notarized is still refused by Gatekeeper on a downloaded image, so it buys nothing on its own" \
             "Create a profile with: xcrun notarytool store-credentials"
        return
    fi
    if [ -n "$identity" ]; then
        ok "signing as: $identity (notary profile: $notary)"
    else
        ok "MDT_SHIP_UNSIGNED=1: a deliberately unsigned image, quarantine cleared on install"
    fi
}

#----- D. rust toolchain --------------------------------------------------

check_rust_toolchain() {
    section "D. rust toolchain"
    if ! command -v cargo >/dev/null 2>&1; then
        fail "rust toolchain" "cargo not found on PATH" \
             "Install Rust: https://rustup.rs then re-open the shell."
        return
    fi
    local tauri_version
    if ! tauri_version="$(cargo tauri --version 2>/dev/null | head -1)" || [ -z "$tauri_version" ]; then
        fail "rust toolchain" "cargo-tauri not found (cargo is present)" \
             "cargo install tauri-cli --version ^2"
        return
    fi
    ok "cargo and cargo-tauri present ($tauri_version)"
}

#----- E. python runner ---------------------------------------------------

check_uv() {
    section "E. python runner"
    if ! command -v uv >/dev/null 2>&1; then
        fail "uv" "uv not found on PATH" \
             "Install uv: https://docs.astral.sh/uv/. The recipe stages the engine payload and reads every build stamp through 'uv run'."
        return
    fi
    ok "uv present ($(uv --version 2>/dev/null | head -1))"
}

#----- F. node toolchain pin ----------------------------------------------

# Checked even when the SPA is already fresh. The pin is what rebuilds the SPA
# after the next source edit, and finding it wrong at that moment is exactly
# the extra round trip this preflight exists to remove.
check_pnpm_pin() {
    section "F. node toolchain pin"
    if [ ! -f "$FRONTEND_PACKAGE_JSON" ]; then
        fail "pnpm toolchain pin" "no $FRONTEND_PACKAGE_JSON, so the pinned pnpm version cannot be read" \
             "Run the preflight against a full checkout; the pin lives in the frontend package.json."
        return
    fi
    local pinned installed
    pinned="$(sed -n 's/.*"packageManager"[[:space:]]*:[[:space:]]*"pnpm@\([^"]*\)".*/\1/p' "$FRONTEND_PACKAGE_JSON" | head -1)"
    if [ -z "$pinned" ]; then
        fail "pnpm toolchain pin" "$FRONTEND_PACKAGE_JSON declares no pnpm packageManager pin" \
             "Add \"packageManager\": \"pnpm@<version>\" so every machine builds the SPA with one toolchain."
        return
    fi
    if ! command -v pnpm >/dev/null 2>&1; then
        fail "pnpm toolchain pin" "pnpm not found on PATH (package.json pins pnpm@$pinned)" \
             "corepack enable && corepack use pnpm@$pinned"
        return
    fi
    # MEASURED FROM THE FRONTEND DIRECTORY, which is where the SPA build runs
    # (`cd apps/webui/frontend && pnpm run build`, as every justfile recipe
    # spells it). corepack resolves packageManager from the CURRENT directory,
    # so asking the repo root reports whatever pnpm is on PATH and calls a
    # correctly pinned machine unpinned. Measured from the root on this Mac:
    # 11.24.0. Measured from the frontend: 11.9.0, the pin. Same machine.
    installed="$(cd "$FRONTEND_DIR" && pnpm --version 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$installed" ]; then
        fail "pnpm toolchain pin" "pnpm is on PATH but 'pnpm --version' printed nothing in $FRONTEND_DIR, so the toolchain cannot be verified" \
             "Reinstall pnpm: corepack enable && corepack use pnpm@$pinned"
        return
    fi
    if [ "$installed" != "$pinned" ]; then
        fail "pnpm toolchain pin" "pnpm resolves to $installed in the frontend directory but package.json pins pnpm@$pinned" \
             "corepack enable && corepack use pnpm@$pinned (an unpinned pnpm resolves a different dependency tree, so the SPA it builds is not the SPA this artifact is supposed to carry). Build the SPA as 'cd apps/webui/frontend && pnpm run build': the --dir form runs corepack from the repo root, where the pin is invisible."
        return
    fi
    ok "pnpm $installed in the frontend directory matches the pin"
}

#----- G. built SPA -------------------------------------------------------

# The SAME instrument the payload builder runs, so a pass here cannot disagree
# with the build. A second implementation of "is the SPA fresh" would drift,
# and a preflight that passes where the build fails is worse than no preflight.
check_spa_built() {
    section "G. built SPA"
    local reason
    if ! command -v uv >/dev/null 2>&1; then
        fail "built SPA" "cannot run the SPA freshness check: uv is not on PATH" \
             "Install uv (see section E). The check is not being skipped, it could not be measured."
        return
    fi
    if reason="$(cd "$SELF_ROOT" && uv run --no-project python -c '
import sys
from pathlib import Path

from scripts.build_engine_payload import PayloadBuildError, assert_spa_is_fresh

try:
    assert_spa_is_fresh(Path(sys.argv[1]))
except PayloadBuildError as exc:
    print(exc)
    raise SystemExit(1)
' "$FRONTEND_DIR" 2>&1)"; then
        ok "SPA at $FRONTEND_DIR/build is present and newer than its sources"
        return
    fi
    fail "built SPA" "$reason" \
         "cd apps/webui/frontend && pnpm run build"
}

#----- main ---------------------------------------------------------------

echo "Open DJ dmg preflight (read-only)"
echo "repo: $ROOT"
check_platform
check_working_tree
check_signing_config
check_rust_toolchain
check_uv
check_pnpm_pin
check_spa_built

section "summary"
if [ "$FAILURES" -gt 0 ]; then
    echo "unmet preconditions: $FAILURES ($FAILED_NAMES)"
    echo
    echo "REPORT TO USER: dmg preflight FAILED with $FAILURES unmet precondition(s): $FAILED_NAMES. Nothing was built. Every [ERROR] above is independent of the others, so fix them together and re-run."
    exit 1
fi
echo "unmet preconditions: 0"
echo
echo "REPORT TO USER: dmg preflight PASSED. Every build precondition is met."
