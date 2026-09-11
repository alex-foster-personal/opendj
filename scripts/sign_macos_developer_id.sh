#!/usr/bin/env bash
# Developer ID signing and notarization for the DIRECT-DOWNLOAD dmg.
#
# SCOPE. This is the Developer ID Application path: a dmg a tester downloads
# and opens by double-click, cleared by Gatekeeper because it is notarized and
# stapled. It is NOT the Mac App Store path. scripts/ship_appstore.sh uses a
# different certificate (Mac App Distribution / 3rd Party Mac Developer
# Installer), a provisioning profile and sandbox entitlements, off the SAME
# Team ID. The two certificates are not interchangeable; do not merge them.
#
# WHY A SEPARATE SCRIPT RATHER THAN INLINE `just` LINES. Tauri's bundler signs
# the .app it produces, but the app carries a whole relocatable CPython under
# Contents/Resources/payload: dozens of extension modules and shared libraries
# plus the extensionless interpreter itself, all of them Mach-O and none of
# them findable by extension alone. The bundler does not walk into a resource
# directory, so those Mach-O files would reach the notary service unsigned and
# the submission would come back Invalid. Signing them has to happen while the
# payload is still a staging directory, BEFORE `cargo tauri build` seals it
# into the bundle -- a different point in the recipe from every other step.
# Splitting the stages into subcommands is what lets the justfile call each
# one where it belongs, and lets them be tested individually without a build.
#
# USAGE
#   sign_macos_developer_id.sh payload <staged-payload-dir>
#   sign_macos_developer_id.sh verify-dmg-app <dmg>
#   sign_macos_developer_id.sh notarize-app <app>
#   sign_macos_developer_id.sh dmg <dmg>
#   sign_macos_developer_id.sh notarize <dmg>
#
# ENVIRONMENT
#   MDT_MACOS_SIGNING_IDENTITY        Developer ID Application identity, as
#       `security find-identity -v -p codesigning` prints it. Required by
#       payload / verify-dmg-app / dmg.
#   MDT_MACOS_NOTARY_KEYCHAIN_PROFILE `xcrun notarytool store-credentials`
#       profile name. Required by notarize.
#
# Every subcommand fails loudly rather than degrading. There is no path
# through this script that produces an unsigned artifact and reports success.
#
# Acceptance (each line is a hard assertion; any failure exits non-zero):
#   [if] a subcommand runs with MDT_MACOS_SIGNING_IDENTITY unset and does not
#        exit non-zero naming that variable [then] broken
#   [if] `payload` leaves any Mach-O file in the staged payload unsigned
#        [then] the notary service returns Invalid -> broken
#   [if] `verify-dmg-app` passes an app that lacks the hardened runtime, a
#        secure timestamp, or a Developer ID Application authority [then] the
#        notary service returns Invalid -> broken
#   [if] `notarize` reports success on a submission whose status is not
#        Accepted [then] an un-notarized dmg ships as notarized -> broken
#   [if] `notarize` leaves the dmg unstapled [then] a tester offline at first
#        launch is refused by Gatekeeper -> broken

set -euo pipefail

# macho_files / macho_count. Shared with scripts/ship_appstore.sh so the two
# signing paths cannot disagree about which files are Mach-O.
# shellcheck source=scripts/lib/macho.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/macho.sh"

#----- helpers ------------------------------------------------------------

die() {
    echo "[ERROR] $*" >&2
    exit 1
}

ok() { echo "[OK] $*"; }

_require_identity() {
    [ -n "${MDT_MACOS_SIGNING_IDENTITY:-}" ] || die \
        "MDT_MACOS_SIGNING_IDENTITY is not set. This is the Developer ID Application identity, as printed by: security find-identity -v -p codesigning"
}

_require_notary_profile() {
    [ -n "${MDT_MACOS_NOTARY_KEYCHAIN_PROFILE:-}" ] || die \
        "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is not set. Create one with: xcrun notarytool store-credentials"
}

_require_tool() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found on PATH; this step needs the Xcode command line tools"
}

#----- payload ------------------------------------------------------------

# Sign every Mach-O inside the STAGED payload directory, before the Tauri
# bundler copies it into Contents/Resources and seals the outer bundle around
# it. These are all standalone Mach-O files rather than nested bundles, so
# they carry no enclosing signature for one another to invalidate and the
# order among them does not matter -- only that all of them are signed before
# the .app is.
#
# --options runtime is the hardened runtime, which notarization requires.
# --timestamp asks Apple's timestamp authority for a secure timestamp, which
# notarization also requires; it is a network round trip per file, so the
# files are signed a few at a time and the elapsed cost is printed rather
# than absorbed (OPS-06 budgets the whole dmg pipeline at 12 minutes).
cmd_payload() {
    local payload="${1:-}"
    [ -n "$payload" ] || die "usage: $0 payload <staged-payload-dir>"
    # Identity first: a missing certificate is the far likelier mistake, and
    # reporting a path problem instead would send the operator hunting in the
    # wrong place.
    _require_identity
    _require_tool codesign
    [ -d "$payload" ] || die "no staged payload directory at $payload"

    local count started elapsed
    count=$(macho_count "$payload")
    [ "$count" -gt 0 ] || die "found no Mach-O files under $payload; the payload should carry a CPython runtime, so an empty scan means the payload is not staged"

    echo "[INFO] signing $count Mach-O files in the payload with the hardened runtime"
    started=$(date +%s)
    macho_files "$payload" | xargs -0 -P 4 -n 1 \
        codesign --force --timestamp --options runtime \
        --sign "$MDT_MACOS_SIGNING_IDENTITY" ||
        die "codesign failed inside the payload; the outer bundle would have been sealed around an unsigned Mach-O"
    elapsed=$(($(date +%s) - started))
    ok "payload signed, $count files in ${elapsed}s"
}

#----- verify-dmg-app -----------------------------------------------------

# Prove the .app inside the built image really carries what the notary service
# demands, BEFORE spending a submission on it. `cargo tauri build` deletes the
# staged .app once the image exists, so the image is the only surviving copy
# and this mounts it read-only to look.
#
# The three properties checked here are exactly the three that come back from
# the notary service as an Invalid status with a log file rather than as a
# build error, which is a slow and confusing way to learn them.
cmd_verify_dmg_app() {
    local dmg="${1:-}"
    [ -n "$dmg" ] || die "usage: $0 verify-dmg-app <dmg>"
    _require_identity
    _require_tool codesign
    [ -f "$dmg" ] || die "no dmg at $dmg"

    local mount app sig
    mount=$(mktemp -d /tmp/opendj-signing-verify.XXXXXX)
    # shellcheck disable=SC2064
    trap "hdiutil detach '$mount' >/dev/null 2>&1 || true; rmdir '$mount' 2>/dev/null || true" EXIT
    hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$mount" >/dev/null
    # shellcheck disable=SC2012  # mount contents are our own bundler's output
    app=$(ls -d "$mount"/*.app 2>/dev/null | head -1)
    [ -n "$app" ] || die "the image holds no .app"

    codesign --verify --deep --strict --verbose=2 "$app" ||
        die "the signature on $(basename "$app") does not verify"

    sig=$(codesign -d --verbose=4 "$app" 2>&1)

    printf '%s\n' "$sig" | grep -q '^Authority=Developer ID Application' ||
        die "$(basename "$app") is not signed by a Developer ID Application authority. codesign reported: $(printf '%s\n' "$sig" | grep '^Authority=' | head -1). A Mac App Distribution certificate cannot be notarized for direct download; those two certificates are not interchangeable."

    printf '%s\n' "$sig" | grep -qE 'flags=0x[0-9a-f]*\(.*runtime.*\)' ||
        die "$(basename "$app") was signed without the hardened runtime (codesign --options runtime). The notary service rejects that."

    printf '%s\n' "$sig" | grep -q '^Timestamp=' ||
        die "$(basename "$app") carries no secure timestamp (codesign --timestamp). The notary service rejects that."

    ok "$(basename "$app") is Developer ID signed, hardened, and timestamped"
}

#----- notarize-app -------------------------------------------------------

# A stapled app is required for the updater archive, not only the dmg. Apple
# accepts app bundles only as a zip submission, then staples the original app.
#
# The temp archive path lives in a GLOBAL rather than a `local`, and the trap
# below dereferences it when it fires. bash 3.2 (what `/usr/bin/env bash` is on
# macOS) does not scope a RETURN trap to the function that set it: it stays
# armed and fires AGAIN when the CALLER returns. A `local` is gone by then, so
# `set -u` aborted a run that had already notarized, stapled and passed spctl.
# Measured Tue 9 Sep 2026 on the Air: notarization printed
# "[OK] app notarized, stapled and accepted by spctl in 86s" and the run then
# died with "zip: unbound variable" attributed to main(), losing a 504s build.
#
# A global outlives the caller, so the second firing is an `rm -f` of a path
# that is already gone, which is a no-op. Expanding the path INTO the trap
# STRING would survive the same way, and was the first fix here, but a trap
# string is re-parsed as CODE when it fires, so that form embeds an
# environment-derived value (TMPDIR, below) into code. A global is
# dereferenced as DATA and is never re-parsed, so no quoting question arises.
# The sibling EXIT trap in cmd_verify_dmg_app does interpolate, and is safe for
# a different reason: its path comes from `mktemp -d /tmp/...` with a hardcoded
# prefix, so no part of it is environment-derived.
#
# A DIRECTORY, not a suffixed file. BSD mktemp randomizes only TRAILING X's,
# so the old `opendj-notary-app.XXXXXX.zip` template was one fixed literal
# name, and a failed run that left it behind made every later signed build on
# that host die with "mkstemp failed ... File exists" (silver, Thu 10 Sep
# 2026). The EXIT trap is what cleans up after `die`: die exits, and an exit
# never fires a RETURN trap, which is how that file was left behind at all.
NOTARY_APP_DIR=""

cmd_notarize_app() {
    local app="${1:-}"
    [ -n "$app" ] || die "usage: $0 notarize-app <app>"
    _require_identity
    _require_notary_profile
    _require_tool ditto
    _require_tool xcrun
    _require_tool spctl
    [ -d "$app" ] || die "no app bundle at $app"

    local out started elapsed
    NOTARY_APP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/opendj-notary-app.XXXXXX")
    trap 'rm -rf "$NOTARY_APP_DIR"' RETURN
    trap 'rm -rf "$NOTARY_APP_DIR"' EXIT
    ditto -c -k --keepParent "$app" "$NOTARY_APP_DIR/app.zip" || die "could not archive app for notarization"
    started=$(date +%s)
    out=$(xcrun notarytool submit "$NOTARY_APP_DIR/app.zip" \
        --keychain-profile "$MDT_MACOS_NOTARY_KEYCHAIN_PROFILE" --wait 2>&1) || {
        printf '%s\n' "$out"
        die "notarytool submit failed for app"
    }
    printf '%s\n' "$out"
    elapsed=$(($(date +%s) - started))
    printf '%s\n' "$out" | grep -q 'status: Accepted' || die \
        "the notary service did not accept the app submission"
    xcrun stapler staple "$app" || die "stapling app failed"
    xcrun stapler validate "$app" || die "the app staple does not validate"
    spctl -a -t exec -vv "$app" || die "spctl rejected the stapled app"
    ok "app notarized, stapled and accepted by spctl in ${elapsed}s"
}

#----- dmg ----------------------------------------------------------------

# Sign the image itself. Gatekeeper assesses the dmg a tester double-clicks,
# not only the app inside it, and an unsigned container around a signed app is
# a warning a tester should never have to see.
cmd_dmg() {
    local dmg="${1:-}"
    [ -n "$dmg" ] || die "usage: $0 dmg <dmg>"
    _require_identity
    _require_tool codesign
    [ -f "$dmg" ] || die "no dmg at $dmg"

    codesign --force --timestamp --sign "$MDT_MACOS_SIGNING_IDENTITY" "$dmg" ||
        die "codesign failed on the image"
    codesign --verify --strict --verbose=2 "$dmg" ||
        die "the image signature does not verify"
    ok "image signed"
}

#----- notarize -----------------------------------------------------------

# Submit, wait, staple, and prove the staple took.
#
# WHY THE STATUS LINE IS PARSED. `notarytool submit --wait` is reported to
# exit 0 whenever the submission COMPLETES, including when it completes with
# status Invalid. This is not a silent-success hole on its own: `stapler
# staple` fails when no ticket was issued, and under `set -e` that aborts.
# What the parse buys is the failure landing at the right step with a message
# naming the real problem, rather than surfacing later as a confusing stapler
# error about a missing ticket.
#
# UNTESTED ASSUMPTION, stated rather than hidden: no Developer ID certificate
# exists on this machine, so this function has never run. The exit-code
# behavior above is from Apple's documented contract, not from observation.
# Parsing defensively is the right call precisely because it cannot be
# verified here.
cmd_notarize() {
    local dmg="${1:-}"
    [ -n "$dmg" ] || die "usage: $0 notarize <dmg>"
    _require_identity
    _require_notary_profile
    _require_tool xcrun
    [ -f "$dmg" ] || die "no dmg at $dmg"

    local out started elapsed
    echo "[INFO] submitting to the notary service with keychain profile $MDT_MACOS_NOTARY_KEYCHAIN_PROFILE"
    started=$(date +%s)
    out=$(xcrun notarytool submit "$dmg" \
        --keychain-profile "$MDT_MACOS_NOTARY_KEYCHAIN_PROFILE" --wait 2>&1) || {
        printf '%s\n' "$out"
        die "notarytool submit failed"
    }
    printf '%s\n' "$out"
    elapsed=$(($(date +%s) - started))

    printf '%s\n' "$out" | grep -q 'status: Accepted' || die \
        "the notary service did not accept the submission (see the status above). Read the full log with: xcrun notarytool log <submission-id> --keychain-profile $MDT_MACOS_NOTARY_KEYCHAIN_PROFILE"

    xcrun stapler staple "$dmg" || die "stapling failed"
    xcrun stapler validate "$dmg" || die "the staple does not validate"

    # The ticket is what lets a tester who is offline at first launch through
    # Gatekeeper, so assess the image the way Gatekeeper will.
    spctl -a -vvv -t open --context context:primary-signature "$dmg" ||
        die "spctl rejected the stapled image; it would still warn a tester"

    ok "notarized, stapled and accepted by spctl in ${elapsed}s"
}

#----- dispatch -----------------------------------------------------------

main() {
    local action="${1:-}"
    shift || true
    case "$action" in
    payload) cmd_payload "$@" ;;
    verify-dmg-app) cmd_verify_dmg_app "$@" ;;
    notarize-app) cmd_notarize_app "$@" ;;
    dmg) cmd_dmg "$@" ;;
    notarize) cmd_notarize "$@" ;;
    "") die "usage: $0 {payload|verify-dmg-app|notarize-app|dmg|notarize} <path>" ;;
    *) die "unknown subcommand '$action'; expected one of payload, verify-dmg-app, notarize-app, dmg, notarize" ;;
    esac
}

main "$@"
