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
#   TAURI_SIGNING_PRIVATE_KEY           updater minisign key (never printed)
#   OPENDJ_GOOGLE_OAUTH_CLIENT_ID       Desktop-app client baked into payload
#   OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET   non-confidential installed-app secret
#   MDT_DOPPLER_BIN                     doppler executable to resolve the
#                                        OAuth client from (default: "doppler",
#                                        resolved via PATH as before). Lets a
#                                        caller point at an explicit path
#                                        instead of hiding doppler by editing
#                                        PATH, which is host-layout-dependent:
#                                        stripping doppler's PATH directory can
#                                        also strip an unrelated tool that
#                                        happens to share it.
#
# -Claude
set -euo pipefail

case "${1:-}" in
    "") ;;
    -h|--help) sed -n '2,48p' "$0"; exit 0 ;;
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

# Run a version probe WITHOUT letting a failing probe end the report.
#
# This is the trap the whole script exists to avoid, one level down. Under
# `set -e` plus `pipefail`, `x="$(broken_tool --version | head -1)"` ENDS THE
# SCRIPT, so the branch written to report that the tool is broken never runs
# and the operator gets a truncated report with no summary and no idea which
# check died. Every external probe goes through here: the caller reads the
# status and the output, and decides.
_probe_in() { # <dir> <command...>; prints first output line, returns its status
    local dir="$1"
    shift
    local out status=0
    out="$(cd "$dir" && "$@" 2>/dev/null | head -1)" || status=$?
    printf '%s' "$out"
    return "$status"
}
_probe() { _probe_in "$PWD" "$@"; }

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
        # `|| true`: this line runs INSIDE a failure report, and on a machine
        # with no `security` at all the pipeline's nonzero status would end
        # the script mid-sentence under `set -e`.
        security find-identity -v -p codesigning 2>&1 | sed 's/^/          /' || true
        return
    fi
    if [ -n "$identity" ] && [ -z "$notary" ]; then
        fail "signing configuration" \
             "MDT_MACOS_SIGNING_IDENTITY is set but MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is not. A Developer ID signature that is never notarized is still refused by Gatekeeper on a downloaded image, so it buys nothing on its own" \
             "Create a profile with: xcrun notarytool store-credentials"
        return
    fi
    # A configured NAME is not a usable IDENTITY. Measured on silver
    # Thu 10 Sep 2026: MDT_MACOS_SIGNING_IDENTITY was set from ~/.zshenv while
    # the machine held no Developer ID certificate in any keychain. Every check
    # above passed, and the build died at codesign 30s later, having already
    # staged the payload. This preflight exists precisely to convert that into
    # a one-second refusal, so the string check alone was a check that could
    # not fail for the condition it was there to catch.
    if [ -n "$identity" ] && [ "$ship_unsigned" != "1" ]; then
        if ! command -v security >/dev/null 2>&1; then
            # CANNOT MEASURE IS AN UNMET PRECONDITION, NOT A WARNING.
            #
            # This branch used to print UNMEASURED and fall through to the
            # `ok "signing as..."` below, so the preflight exited 0 and `just
            # dmg` proceeded with an unverified identity. A warning that the
            # exit code does not carry is a warning nothing acts on, and the
            # summary line then said the same thing it says for a machine that
            # really does hold the certificate. The whole point of this check
            # is that those two states must not look alike.
            fail "signing identity" \
                 "the signing identity CANNOT BE MEASURED on this host: no \`security\` binary on PATH, so there is no way to tell a present certificate from an absent one before codesign runs" \
                 "Run the build on a macOS host (\`security\` ships with the OS), or export MDT_SHIP_UNSIGNED=1 to build a deliberately unsigned dev image that testers must clear by hand."
            return
        else
            # `-v` restricts to identities that are VALID (chain intact), and
            # `-p codesigning` to those usable for signing, so a match means
            # the cert, its private key and its chain are all really here.
            # silver had the cert and reported 0 valid until Apple's G2
            # intermediate was installed; that state must fail, not pass.
            local available
            available="$(security find-identity -v -p codesigning 2>/dev/null || true)"
            if ! printf '%s' "$available" | grep -qF -- "$identity"; then
                fail "signing identity" \
                     "MDT_MACOS_SIGNING_IDENTITY is set to '$identity' but no VALID codesigning identity on this machine matches it, so codesign will fail after the payload is staged" \
                     "Install the Developer ID Application certificate AND its private key into a keychain on this host, then confirm with: security find-identity -v -p codesigning. A cert whose chain is incomplete reports 0 valid: if the identity is listed by \`security find-identity -p codesigning\` but not by the \`-v\` form, the Apple intermediate (Developer ID Certification Authority, OU=G2) is missing."
                printf '          valid codesigning identities on this machine:\n'
                printf '%s\n' "${available:-  (none)}" | sed 's/^/          /'
                return
            fi
        fi
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
    if ! tauri_version="$(_probe cargo tauri --version)" || [ -z "$tauri_version" ]; then
        fail "rust toolchain" "cargo-tauri did not answer 'cargo tauri --version' (cargo itself is present)" \
             "cargo install tauri-cli --version ^2"
        return
    fi
    ok "cargo and cargo-tauri present ($tauri_version)"
}

#----- D2. Apple xattr ----------------------------------------------------

check_apple_xattr() {
    section "D2. Apple xattr (bundler runs xattr -cr before codesign)"
    local probe
    probe=$(mktemp -d /tmp/opendj-xattr-probe.XXXXXX)
    touch "$probe/f"
    if ! PATH="/usr/bin:$PATH" xattr -cr "$probe" 2>/dev/null; then
        rm -rf "$probe"
        fail "apple xattr" "/usr/bin/xattr -cr failed; the bundler cannot strip extended attributes before signing" \
             "This is Apple's xattr; check macOS tooling (xcode-select --install)."
        return
    fi
    rm -rf "$probe"
    if ! xattr -cr /dev/null 2>/dev/null && [ "$(command -v xattr)" != "/usr/bin/xattr" ]; then
        ok "a non-Apple xattr ($(command -v xattr)) shadows /usr/bin/xattr; the dmg recipe puts /usr/bin first for the bundle step"
    else
        ok "xattr supports -cr"
    fi
}

#----- E. python runner ---------------------------------------------------

check_uv() {
    section "E. python runner"
    if ! command -v uv >/dev/null 2>&1; then
        fail "uv" "uv not found on PATH" \
             "Install uv: https://docs.astral.sh/uv/. The recipe stages the engine payload and reads every build stamp through 'uv run'."
        return
    fi
    # NOT `ok "uv present ($(uv --version))"`. A command substitution inside an
    # argument throws its status away, so a uv that is present and broken
    # printed `[OK] uv present ()`: a PASS reported by a probe that measured
    # nothing, which is the one thing this script promises never to do.
    local version
    if ! version="$(_probe uv --version)" || [ -z "$version" ]; then
        fail "uv" "uv is on PATH but 'uv --version' did not answer, so the payload stage and every build stamp would fail later instead of here" \
             "Reinstall uv: https://docs.astral.sh/uv/. Check 'uv --version' answers before re-running."
        return
    fi
    ok "uv present ($version)"
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
    local status=0
    installed="$(_probe_in "$FRONTEND_DIR" pnpm --version)" || status=$?
    installed="${installed//[[:space:]]/}"
    if [ "$status" -ne 0 ] || [ -z "$installed" ]; then
        fail "pnpm toolchain pin" "'pnpm --version' in $FRONTEND_DIR exited $status and printed '$installed', so the toolchain cannot be checked against the pnpm@$pinned pin. corepack failing to resolve a pinned version it has never fetched looks exactly like this" \
             "corepack enable && corepack use pnpm@$pinned, and resolve the pin once while online if corepack cannot reach the registry."
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

#----- H. tauri updater signing key ---------------------------------------

# The gap this closes: item C checks APPLE code signing, which is a different
# key for a different purpose, so a fully green preflight still died about
# eight minutes into `cargo tauri build` with "A public key has been found,
# but no private key. Make sure to set TAURI_SIGNING_PRIVATE_KEY". That is the
# most expensive way this build can fail, because the failure lands after the
# entire cargo release compile has been paid for.
#
# The requirement is not conditional on intent: tauri.conf.json sets
# createUpdaterArtifacts and compiles in an updater pubkey, so EVERY build
# through this recipe signs updater artifacts and every build needs the key.
#
# THE VALUE IS NEVER PRINTED, not even truncated. This report goes into logs,
# CI output and pasted terminal scrollback.
check_updater_signing_key() {
    section "H. tauri updater signing key"
    local conf="$ROOT/apps/desktop/src-tauri/tauri.conf.json"
    if [ ! -f "$conf" ]; then
        fail "updater signing key" "no $conf, so it cannot be read whether this build signs updater artifacts" \
             "Run the preflight against a full checkout."
        return
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        fail "updater signing key" "python3 is not on PATH, so tauri.conf.json cannot be parsed. The check is not being skipped, it could not be measured" \
             "python3 ships with macOS; restore it (xcode-select --install) and re-run."
        return
    fi
    # Prints "<createUpdaterArtifacts true|false> <has-pubkey yes|no>". A parse
    # failure prints nothing and exits nonzero, which the caller reports.
    local state status=0
    state="$(python3 -c '
import json, sys
with open(sys.argv[1]) as fh:
    conf = json.load(fh)
wants = conf.get("bundle", {}).get("createUpdaterArtifacts") is True
pubkey = conf.get("plugins", {}).get("updater", {}).get("pubkey") or ""
print("true" if wants else "false", "yes" if pubkey.strip() else "no")
' "$conf" 2>/dev/null)" || status=$?
    if [ "$status" -ne 0 ] || [ -z "$state" ]; then
        fail "updater signing key" "could not parse $conf (python3 exited $status), so it is unknown whether this build needs the updater key" \
             "Check the file is valid JSON: python3 -m json.tool on it."
        return
    fi
    local wants_artifacts has_pubkey
    wants_artifacts="$(printf '%s' "$state" | awk '{print $1}')"
    has_pubkey="$(printf '%s' "$state" | awk '{print $2}')"

    if [ "$wants_artifacts" != "true" ]; then
        ok "createUpdaterArtifacts is off, so no updater signing key is needed"
        return
    fi
    if [ "$has_pubkey" != "yes" ]; then
        fail "updater signing key" \
             "tauri.conf.json sets createUpdaterArtifacts but carries no plugins.updater.pubkey, so the bundler would emit update artifacts nothing can verify" \
             "Add the public key to plugins.updater.pubkey (see docs/auto-update.md), or set createUpdaterArtifacts to false."
        return
    fi
    if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
        fail "updater signing key" \
             "createUpdaterArtifacts is on and an updater pubkey is compiled in, but TAURI_SIGNING_PRIVATE_KEY is empty or unset. cargo tauri build fails on this AFTER the full release compile, about eight minutes in" \
             "export TAURI_SIGNING_PRIVATE_KEY=\"\$(doppler secrets get TAURI_UPDATER_PRIVATE_KEY --project general --config dev_personal --plain)\" and export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=\"\" (the key has no passphrase). Full detail: docs/auto-update.md."
        return
    fi
    # NON-EMPTY IS NOT THE SAME AS USABLE. A malformed key, a wrong password,
    # or a key belonging to a DIFFERENT pubkey all satisfy an emptiness test
    # and then either kill the build after the full compile or, worse, sign
    # updates the compiled-in public key cannot verify. That second failure
    # ships silently and breaks the updater in every installed copy.
    #
    # So prove it rather than assume it: sign a throwaway file with the real
    # signer, then compare the key id in the signature against the key id in
    # the configured pubkey. Both are 8 bytes at a fixed offset, and neither
    # is secret.
    if ! command -v cargo >/dev/null 2>&1 || ! cargo tauri --version >/dev/null 2>&1; then
        fail "updater signing key" \
             "TAURI_SIGNING_PRIVATE_KEY is set, but cargo-tauri is not available to prove it signs and pairs with the compiled-in pubkey, so the pairing could NOT be measured" \
             "Install tauri-cli (see section D). A key that is merely non-empty can still fail the build after the full compile, or sign updates nothing can verify."
        return
    fi
    local probe_dir probe sign_out sign_rc=0 scrubbed
    probe_dir="$(mktemp -d /tmp/opendj-updater-key.XXXXXX)"
    probe="$probe_dir/probe.txt"
    printf 'preflight\n' > "$probe"
    # The signer reads TAURI_SIGNING_PRIVATE_KEY from the environment itself,
    # so the secret never lands on disk here and never appears in argv.
    sign_out="$(TAURI_SIGNING_PRIVATE_KEY_PASSWORD="${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}" \
        cargo tauri signer sign "$probe" 2>&1)" || sign_rc=$?
    if [ "$sign_rc" -ne 0 ] || [ ! -f "$probe.sig" ]; then
        # Scrubbed defensively, in case a future signer echoes what it got.
        #
        # Redacted with a bash substitution rather than a sed program built
        # from the key. A real generated key is multiline (comment line, then
        # payload), so interpolating it into `s|...|...|g` lets its first
        # newline terminate the expression: sed dies with `unterminated s
        # command`, and under `set -euo pipefail` that takes the whole
        # preflight down HERE, before the cleanup, the fail() and the summary.
        # The operator asked why their key does not work and would get a sed
        # parse error, a leaked probe dir and no report. The wrong-password
        # case, which is the most likely one, is exactly when it fires.
        scrubbed="${sign_out//"$TAURI_SIGNING_PRIVATE_KEY"/"<REDACTED>"}"
        scrubbed="$(printf '%s' "$scrubbed" | tail -3 | tr '\n' ' ')"
        rm -rf "$probe_dir"
        fail "updater signing key" \
             "TAURI_SIGNING_PRIVATE_KEY is set but the signer could not USE it (exit $sign_rc). A malformed key or a wrong TAURI_SIGNING_PRIVATE_KEY_PASSWORD fails here instead of after the full compile. Signer said: $scrubbed" \
             "Re-export from Doppler: TAURI_SIGNING_PRIVATE_KEY=\"\$(doppler secrets get TAURI_UPDATER_PRIVATE_KEY --project general --config dev_personal --plain)\" with TAURI_SIGNING_PRIVATE_KEY_PASSWORD=\"\" (the key has no passphrase). See docs/auto-update.md."
        return
    fi
    # CRYPTOGRAPHIC VERIFICATION, not a name check.
    #
    # Comparing key ids alone compares an 8-byte tag chosen at generation and
    # copied into both files. It proves the two artifacts CLAIM the same
    # identity; it does not prove the 32-byte Ed25519 body can verify what the
    # private key signs. A pubkey with the right header and id but a corrupted
    # or substituted body passes a name check and then rejects every update in
    # the field, which is the exact failure this item exists to prevent.
    #
    # So verify the probe signature against the configured public key, and
    # reject payloads that are not the right length. Ed25519 comes from the
    # `cryptography` package via uv rather than being hand-rolled: forty lines
    # of modular arithmetic in the code path that decides whether updates are
    # trustworthy is not a trade worth making.
    if ! command -v uv >/dev/null 2>&1; then
        rm -rf "$probe_dir"
        fail "updater signing key" \
             "the key signed, but uv is not on PATH to run the Ed25519 verification, so the pairing could NOT be measured" \
             "Install uv (see section E). Matching key ids alone are not proof that the pubkey can verify this key's signatures."
        return
    fi
    # LOCAL ENVIRONMENT FIRST, network second. `cryptography` is declared in
    # the dev extra, so a synced checkout verifies with no fetch at all. The
    # ad-hoc `--with` stays as a fallback for an unsynced tree, but it must
    # not be the primary path: it needs the network on a cold cache, and a
    # release host with every build tool present but PyPI unreachable would
    # then have its dmg gate blocked by the verifier rather than by anything
    # about the build.
    local runner=(uv run --extra dev --no-sync python)
    if ! "${runner[@]}" -c 'import cryptography' >/dev/null 2>&1; then
        runner=(uv run --with cryptography --no-project python)
    fi
    local verdict
    verdict="$("${runner[@]}" -c '
import base64, hashlib, json, sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def body(raw: bytes) -> bytes:
    """The base64 payload line of a minisign pubkey or signature.

    The INNER decode validates. Without validate=True, base64 silently
    DISCARDS bytes outside its alphabet, so a stray character in the
    configured pubkey decodes to the correct 42 bytes here while the strict
    minisign parser in the app rejects the string outright: a green gate and
    an updater that fails for everyone. The OUTER decode stays lenient on
    purpose, because a key may legitimately be supplied either base64-wrapped
    or raw, and that fallback is what tells the two apart.
    """
    try:
        raw = base64.b64decode(raw, validate=True)
    except Exception:
        pass
    for line in raw.decode("utf-8", "replace").splitlines():
        if line and not line.startswith(("untrusted comment:", "trusted comment:")):
            try:
                return base64.b64decode(line, validate=True)
            except Exception:
                print("BADB64")
                raise SystemExit(0)
    raise SystemExit("UNPARSEABLE")


with open(sys.argv[1]) as fh:
    pub = body(json.load(fh)["plugins"]["updater"]["pubkey"].encode())
sig = body(open(sys.argv[2], "rb").read())
data = open(sys.argv[3], "rb").read()

# minisign: algorithm(2) + key_id(8) + ed25519_pk(32) = 42 for a pubkey,
# algorithm(2) + key_id(8) + signature(64) = 74 for a signature.
if len(pub) != 42 or len(sig) != 74:
    print("BADLEN", len(pub), len(sig))
    raise SystemExit(0)

# VALIDATE the algorithm tags, never merely read them. A pubkey whose 32-byte
# body is intact but whose tag is corrupted cannot be parsed by the real
# minisign implementation compiled into the app, so verifying the body alone
# would report OK for a key every installed client rejects. Likewise an
# unknown signature tag must not be quietly treated as legacy "Ed".
if pub[:2] != b"Ed" or sig[:2] not in (b"ED", b"Ed"):
    print("BADALG", pub[:2].hex(), sig[:2].hex())
    raise SystemExit(0)

pub_id, sig_id = pub[2:10].hex().upper(), sig[2:10].hex().upper()
# "ED" signs the blake2b-512 prehash, legacy "Ed" signs the bytes.
msg = hashlib.blake2b(data, digest_size=64).digest() if sig[:2] == b"ED" else data
try:
    Ed25519PublicKey.from_public_bytes(pub[10:42]).verify(sig[10:74], msg)
except InvalidSignature:
    print("NOVERIFY", pub_id, sig_id)
    raise SystemExit(0)
# BOTH checks, not either. Raw Ed25519 ignores the key id entirely, so a
# pubkey with a mutated id but an intact body verifies here, while the
# minisign-verify inside the app refuses the signature for the id mismatch
# alone. (No apostrophes in this block: it lives inside a single-quoted
# shell string, and one would terminate it.) The first
# version of this check compared ids and skipped verification; verifying and
# skipping the ids is the same mistake pointing the other way.
if pub_id != sig_id:
    print("IDMISMATCH", pub_id, sig_id)
    raise SystemExit(0)
print("OK", pub_id)
' "$conf" "$probe.sig" "$probe" 2>/dev/null)" || verdict=""
    rm -rf "$probe_dir"
    local outcome pub_id sig_id
    outcome="$(printf '%s' "$verdict" | awk '{print $1}')"
    case "$outcome" in
      OK)
        pub_id="$(printf '%s' "$verdict" | awk '{print $2}')"
        # Key ids are public by construction. The key itself is never printed,
        # at any length.
        ok "TAURI_SIGNING_PRIVATE_KEY signs, and its signature VERIFIES against the compiled-in pubkey (key id $pub_id)"
        ;;
      NOVERIFY)
        pub_id="$(printf '%s' "$verdict" | awk '{print $2}')"
        sig_id="$(printf '%s' "$verdict" | awk '{print $3}')"
        if [ "$pub_id" = "$sig_id" ]; then
            fail "updater signing key" \
                 "the key signs, and its key id matches the pubkey ($pub_id), but the signature does NOT verify against that pubkey. The public key body is corrupted or substituted, so every installed copy would reject these updates while the ids looked right" \
                 "Restore plugins.updater.pubkey in tauri.conf.json from the real keypair (see docs/auto-update.md). Matching key ids are not proof of pairing."
        else
            fail "updater signing key" \
                 "TAURI_SIGNING_PRIVATE_KEY works, but it is the WRONG KEY: it signs as key id $sig_id while the pubkey compiled into the app is $pub_id. Updates signed with it are rejected by every installed copy, and nothing about the build would tell you" \
                 "Export the key that pairs with plugins.updater.pubkey: TAURI_SIGNING_PRIVATE_KEY=\"\$(doppler secrets get TAURI_UPDATER_PRIVATE_KEY --project general --config dev_personal --plain)\". See docs/auto-update.md."
        fi
        ;;
      BADB64)
        fail "updater signing key" \
             "the pubkey or the signature payload is not strict base64; it carries bytes outside the base64 alphabet. Python would silently DISCARD them and verify the remainder, but the strict minisign parser compiled into the app rejects the string outright, so this would pass here and break the updater for everyone" \
             "Restore plugins.updater.pubkey in tauri.conf.json from the real keypair, with no stray characters (see docs/auto-update.md)."
        ;;
      IDMISMATCH)
        pub_id="$(printf '%s' "$verdict" | awk '{print $2}')"
        sig_id="$(printf '%s' "$verdict" | awk '{print $3}')"
        fail "updater signing key" \
             "the signature verifies against the pubkey's Ed25519 body, but their minisign key ids disagree (pubkey $pub_id, signature $sig_id). The app's minisign-verify refuses a signature whose key id differs from the public key's, so every installed copy would reject these updates" \
             "Restore plugins.updater.pubkey in tauri.conf.json from the real keypair (see docs/auto-update.md)."
        ;;
      BADALG)
        fail "updater signing key" \
             "the minisign algorithm tag is not one this format allows (pubkey/signature tags: $verdict; expected pubkey 4564 and signature 4544 or 4564). The real minisign parser compiled into the app rejects unsupported tags, so a key that verifies here byte-wise would still be unreadable in the field" \
             "Restore plugins.updater.pubkey in tauri.conf.json from the real keypair (see docs/auto-update.md)."
        ;;
      BADLEN)
        fail "updater signing key" \
             "the pubkey or the signature is not a well-formed minisign payload (lengths: $verdict; expected pubkey 42 bytes and signature 74)" \
             "Restore plugins.updater.pubkey in tauri.conf.json from the real keypair (see docs/auto-update.md)."
        ;;
      *)
        fail "updater signing key" \
             "the key signed, but the signature could not be VERIFIED against the pubkey because the check itself did not complete, so the pairing is unmeasured" \
             "Check plugins.updater.pubkey is a valid minisign public key, and that 'uv run --with cryptography' works on this machine (see docs/auto-update.md)."
        ;;
    esac
}

#----- J. Icon Composer compiled asset (actool or verified fallback) -------

# The gap this closes: since #2567 `just dmg` compiles AppIcon.icon with
# actool after cargo tauri build. actool ships only with full Xcode, not
# Command Line Tools alone, so silver and the other build hosts used to pay
# for the full release compile and then die here. This check runs the SAME
# decision helper as the recipe, in read-only mode, so a host without actool
# can still proceed when the committed Assets.car matches the source digest.
check_icon_composer_asset() {
    section "J. Icon Composer compiled asset (actool or verified fallback)"
    local icon_source="$ROOT/apps/desktop/src-tauri/icons/AppIcon.icon"
    if [ ! -d "$icon_source" ]; then
        fail "Icon Composer asset" \
             "no Icon Composer source at $icon_source" \
             "Run the preflight against a full checkout."
        return
    fi
    if ! command -v uv >/dev/null 2>&1; then
        fail "Icon Composer asset" \
             "cannot run the Icon Composer readiness check: uv is not on PATH" \
             "Install uv (see section E). The check is not being skipped, it could not be measured."
        return
    fi
    local status=0
    local result host
    host="$(uname -n 2>/dev/null || hostname 2>/dev/null || echo unknown)"
    result="$(cd "$SELF_ROOT" && env -u UV_LOCKED uv run --no-project python -m scripts.icon_composer_asset \
        --check \
        --source "$icon_source" \
        --committed-car "$icon_source/Assets.car" \
        --sidecar "$icon_source/Assets.car.source.sha256" 2>&1)" || status=$?
    if [ "$status" -ne 0 ]; then
        fail "Icon Composer asset" \
             "$result" \
             "Install full Xcode 26+ and run 'xcode-select -s /Applications/Xcode.app', or regenerate apps/desktop/src-tauri/icons/AppIcon.icon/Assets.car and Assets.car.source.sha256 on a host with actool when the Icon Composer source changes."
        return
    fi
    case "$result" in
        compile\ actool=*)
            ok "Icon Composer: actool available on $host; compile required at build time ($result)"
            ;;
        reuse-committed\ path=*)
            ok "Icon Composer: no actool on $host; verified committed fallback ($result)"
            ;;
        *)
            fail "Icon Composer asset" \
                 "Icon Composer readiness check returned an unrecognised result on $host: $result" \
                 "Re-run: uv run --no-project python -m scripts.icon_composer_asset --check --source $icon_source"
            ;;
    esac
}

#----- I. Google OAuth client (packaged sign-in) --------------------------

# The gap this closes: every packaged install since Google sign-in shipped
# has had GET /api/v1/health google_oauth_configured: false, because nothing
# under apps/desktop or the payload builder set OPENDJ_GOOGLE_OAUTH_CLIENT_ID.
# The Desktop-app client must be in the build environment (or Doppler
# general/dev_personal) so bake_google_oauth can write it into the payload.
# THE VALUE IS NEVER PRINTED.
check_google_oauth_client() {
    section "I. Google OAuth client (packaged sign-in)"
    local client_id="${OPENDJ_GOOGLE_OAUTH_CLIENT_ID:-${GOOGLE_OAUTH_CLIENT_ID:-}}"
    local client_secret="${OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET:-${GOOGLE_OAUTH_CLIENT_SECRET:-}}"
    if [ -n "$client_id" ] && [ -n "$client_secret" ]; then
        ok "Google OAuth client id and secret are present in the environment (values not printed)"
        return
    fi
    # MDT_DOPPLER_BIN lets a caller (tests, an alternate install layout)
    # point at an explicit doppler executable instead of relying on PATH.
    # Defaults to the bare name, resolved via PATH exactly as before.
    local doppler_bin="${MDT_DOPPLER_BIN:-doppler}"
    if command -v "$doppler_bin" >/dev/null 2>&1; then
        local got_id="" got_secret=""
        got_id="$("$doppler_bin" secrets get OPENDJ_GOOGLE_OAUTH_CLIENT_ID --project general --config dev_personal --plain 2>/dev/null || true)"
        if [ -z "$got_id" ]; then
            got_id="$("$doppler_bin" secrets get GOOGLE_OAUTH_CLIENT_ID --project general --config dev_personal --plain 2>/dev/null || true)"
        fi
        got_secret="$("$doppler_bin" secrets get OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET --project general --config dev_personal --plain 2>/dev/null || true)"
        if [ -z "$got_secret" ]; then
            got_secret="$("$doppler_bin" secrets get GOOGLE_OAUTH_CLIENT_SECRET --project general --config dev_personal --plain 2>/dev/null || true)"
        fi
        if [ -n "$got_id" ] && [ -n "$got_secret" ]; then
            ok "Google OAuth client id and secret are available from Doppler (values not printed)"
            return
        fi
    fi
    fail "Google OAuth client" \
         "OPENDJ_GOOGLE_OAUTH_CLIENT_ID or OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET is empty or unset and Doppler did not yield a Desktop-app client. A packaged install cannot sign in without the client baked into the payload" \
         "export OPENDJ_GOOGLE_OAUTH_CLIENT_ID=\"\$(doppler secrets get OPENDJ_GOOGLE_OAUTH_CLIENT_ID --project general --config dev_personal --plain)\" and export OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET=\"\$(doppler secrets get OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET --project general --config dev_personal --plain)\" (Desktop-app client; Google documents the secret as not confidential for installed apps)."
}

check_sentry_ship_dsn() {
    section "Sentry ship DSN (OBS-04)"
    # The payload bakes OPENDJ_SENTRY_DSN_BACKEND_SHIP into telemetry.json and
    # a packaged build defaults telemetry ON. The build host's own SENTRY_DSN
    # is the preview-dev key and is deliberately not a fallback: a dmg
    # reporting under it would mislabel every tester as a preview host.
    if [ -n "${OPENDJ_SENTRY_DSN_BACKEND_SHIP:-}" ]; then
        case "$OPENDJ_SENTRY_DSN_BACKEND_SHIP" in
            https://*@*/*) ok "OPENDJ_SENTRY_DSN_BACKEND_SHIP is set and DSN-shaped (value not printed)"; return ;;
        esac
        fail "Sentry ship DSN" \
             "OPENDJ_SENTRY_DSN_BACKEND_SHIP is set but is not DSN-shaped (expected https://<key>@<host>/<project>)" \
             "export OPENDJ_SENTRY_DSN_BACKEND_SHIP=\"\$(doppler secrets get OPENDJ_SENTRY_DSN_BACKEND_SHIP --project general --config dev_personal --plain)\" (the open-dj-be Default client key)."
        return
    fi
    local doppler_bin="${MDT_DOPPLER_BIN:-doppler}"
    if command -v "$doppler_bin" >/dev/null 2>&1; then
        local got_dsn=""
        got_dsn="$("$doppler_bin" secrets get OPENDJ_SENTRY_DSN_BACKEND_SHIP --project general --config dev_personal --plain 2>/dev/null || true)"
        if [ -n "$got_dsn" ]; then
            ok "Sentry ship DSN is available from Doppler (value not printed)"
            return
        fi
    fi
    fail "Sentry ship DSN" \
         "OPENDJ_SENTRY_DSN_BACKEND_SHIP is empty or unset and Doppler did not yield it. The payload build fails without it, because a dmg with no DSN reports nothing (the gap OBS-04 closes)" \
         "export OPENDJ_SENTRY_DSN_BACKEND_SHIP=\"\$(doppler secrets get OPENDJ_SENTRY_DSN_BACKEND_SHIP --project general --config dev_personal --plain)\" (the open-dj-be Default client key; see docs/telemetry.md)."
}

check_sentry_frontend_dsn() {
    section "Sentry frontend DSN (OBS-06 session replay)"
    # The engine derives the Session Replay loader URL from the open-dj-fe DSN
    # and the page loads it only after the tester accepts the terms (OBS-05).
    if [ -n "${OPENDJ_SENTRY_DSN_FRONTEND:-}" ]; then
        case "$OPENDJ_SENTRY_DSN_FRONTEND" in
            https://*@*/*) ok "OPENDJ_SENTRY_DSN_FRONTEND is set and DSN-shaped (value not printed)"; return ;;
        esac
        fail "Sentry frontend DSN" \
             "OPENDJ_SENTRY_DSN_FRONTEND is set but is not DSN-shaped (expected https://<key>@<host>/<project>)" \
             "export OPENDJ_SENTRY_DSN_FRONTEND=\"\$(doppler secrets get OPENDJ_SENTRY_DSN_FRONTEND --project general --config dev_personal --plain)\" (the open-dj-fe client key)."
        return
    fi
    local doppler_bin="${MDT_DOPPLER_BIN:-doppler}"
    if command -v "$doppler_bin" >/dev/null 2>&1; then
        local got_dsn=""
        got_dsn="$("$doppler_bin" secrets get OPENDJ_SENTRY_DSN_FRONTEND --project general --config dev_personal --plain 2>/dev/null || true)"
        if [ -n "$got_dsn" ]; then
            ok "Sentry frontend DSN is available from Doppler (value not printed)"
            return
        fi
    fi
    fail "Sentry frontend DSN" \
         "OPENDJ_SENTRY_DSN_FRONTEND is empty or unset and Doppler did not yield it. The payload build fails without it, because session replay for test users derives its loader from it (OBS-06)" \
         "export OPENDJ_SENTRY_DSN_FRONTEND=\"\$(doppler secrets get OPENDJ_SENTRY_DSN_FRONTEND --project general --config dev_personal --plain)\" (the open-dj-fe client key; see docs/telemetry.md)."
}

#----- main ---------------------------------------------------------------

echo "Open DJ dmg preflight (read-only)"
echo "repo: $ROOT"
check_platform
check_working_tree
check_signing_config
check_rust_toolchain
check_apple_xattr
check_uv
check_pnpm_pin
check_spa_built
check_updater_signing_key
check_google_oauth_client
check_sentry_ship_dsn
check_sentry_frontend_dsn
check_icon_composer_asset

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
