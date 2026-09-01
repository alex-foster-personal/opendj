#!/usr/bin/env bash
#
# Ship the Open DJ desktop app to the Mac App Store.
#
# PREFLIGHT FIRST, ALWAYS. The default mode is read-only and changes nothing.
# It exists because every expensive failure in this pipeline is knowable in
# advance: a missing certificate, a bundle identifier the provisioning profile
# does not cover, a CFBundleVersion already uploaded, an entitlement the app
# needs and does not declare. Finding those after a 20 minute universal build
# and a 40 minute App Review queue is the whole cost this script removes.
#
# Modes:
#   --preflight   (default) check every requirement, change nothing
#   --build       render entitlements, build, embed the profile, sign inside-out
#   --package     productbuild a signed .pkg from the signed .app
#   --upload      deliver the .pkg to App Store Connect
#   --all         build + package + upload, each gated on a clean preflight
#
# Configuration is environment only, never committed. RUN THIS UNDER DOPPLER:
#
#   doppler run --project general --config dev_personal -- scripts/ship_appstore.sh
#
# MDT_MAS_TEAM_ID already lives there. Every value is REQUIRED for its phase
# and unset is a hard error, never a guess:
#
#   MDT_MAS_TEAM_ID           10-character Apple team id (in Doppler)
#   MDT_MAS_APP_CERT          "Apple Distribution: NAME (TEAMID)"
#   MDT_MAS_INSTALLER_CERT    "3rd Party Mac Developer Installer: NAME (TEAMID)"
#   MDT_MAS_PROVISION_PROFILE path to the .provisionprofile for this bundle id
#   MDT_MAS_ASC_KEY_ID        App Store Connect API key id
#   MDT_MAS_ASC_ISSUER_ID     App Store Connect API issuer id
#
# NOT WIRED INTO CI, on purpose. App Store delivery is release-cadence work
# with credentials no runner should hold, and a red App Store job on every PR
# would train everyone to ignore it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# macho_files / macho_count. Shared with scripts/sign_macos_developer_id.sh so
# the two signing paths cannot disagree about which files are Mach-O.
# shellcheck source=scripts/lib/macho.sh
. "$REPO_ROOT/scripts/lib/macho.sh"

TAURI_DIR="apps/desktop/src-tauri"
CONF="$TAURI_DIR/tauri.conf.json"
BUILD_DIR="$TAURI_DIR/target/appstore"
APP_TEMPLATE="$TAURI_DIR/Entitlements.appstore.template.plist"
INHERIT_TEMPLATE="$TAURI_DIR/Entitlements.appstore.inherit.template.plist"

MODE="preflight"
case "${1:-}" in
    --preflight|"") MODE="preflight" ;;
    --build)        MODE="build" ;;
    --package)      MODE="package" ;;
    --upload)       MODE="upload" ;;
    --all)          MODE="all" ;;
    -h|--help)      sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "[ERROR] unknown argument '${1}'. Run with --help." >&2; exit 2 ;;
esac

BLOCKERS=0
GAPS=0

# What the sandbox scan (section E) must not count as a finding: build
# artifacts, docs, and test harnesses. A .pyc is the same source twice and a
# wdio config describes the test rig, not the shipped app. Kept as one
# pattern so the two scans below cannot drift apart on what counts as noise.
SCAN_NOISE='node_modules|__pycache__|\.pyc$|\.md$|tests?/|wdio\.conf'

#----- reporting ----------------------------------------------------------

ok()      { printf '[OK]      %s\n' "$1"; }
# A BLOCKER stops submission dead. A GAP will be rejected by App Review but
# does not stop the build, so the two are counted separately and the summary
# never merges them: "you cannot build" and "you can build but it will bounce"
# are different days of work.
blocker() { printf '[ERROR]   %s\n' "$1"; printf '          FIX: %s\n' "$2"; BLOCKERS=$((BLOCKERS + 1)); }
gap()     { printf '[WARN]    %s\n' "$1"; printf '          FIX: %s\n' "$2"; GAPS=$((GAPS + 1)); }
section() { printf '\n=== %s ===\n' "$1"; }

require_env() {
    local name="$1" phase="$2"
    if [ -z "${!name:-}" ]; then
        echo "[ERROR] $name is unset and $phase cannot proceed without it." >&2
        echo "        This script will not invent a signing identity, a team" >&2
        echo "        id or a profile path. See the header for all six." >&2
        exit 1
    fi
}

#----- A. host toolchain --------------------------------------------------

check_toolchain() {
    section "A. host toolchain"

    if [ "$(uname -s)" != "Darwin" ]; then
        blocker "host is $(uname -s), not macOS" \
                "App Store delivery needs codesign, productbuild and altool, which exist only on macOS."
        return
    fi
    ok "host is macOS $(sw_vers -productVersion) on $(uname -m)"

    # altool lives INSIDE Xcode.app, not in the Command Line Tools. This is
    # the single most common surprise here: notarytool and productbuild are
    # present with CLT alone, so the pipeline looks ready right up to the
    # upload step and then cannot deliver.
    if xcrun --find altool >/dev/null 2>&1; then
        ok "altool found at $(xcrun --find altool)"
    else
        local dev_dir
        dev_dir="$(xcode-select -p 2>/dev/null || echo unset)"
        blocker "altool not found (active developer dir: $dev_dir)" \
                "Install the full Xcode from the Mac App Store, then run: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer. The Command Line Tools alone ship notarytool and productbuild but NOT altool, so uploads cannot run."

        # Checked only when Xcode is the thing missing, because it is the
        # step that needs the space. Reported as a separate line rather than
        # folded into the fix above: "install Xcode" and "you cannot install
        # Xcode yet" are different problems, and discovering the second one
        # halfway through a 10 GB download is how a disk gets filled.
        local free_gib
        free_gib="$(df -g / | tail -1 | awk '{print $4}')"
        if [ "${free_gib:-0}" -lt 25 ]; then
            blocker "only ${free_gib} GiB free on / but Xcode needs roughly 25 GiB to download and expand" \
                    "Free space before starting the download. Stale apps/desktop/src-tauri/target directories in the sibling worktrees under ~/code are usually the largest reclaimable items; they are pure build artifacts, but confirm no agent is mid-build before deleting one."
        else
            ok "${free_gib} GiB free on /, enough for the Xcode install"
        fi
    fi

    for tool in codesign productbuild; do
        if xcrun --find "$tool" >/dev/null 2>&1 || command -v "$tool" >/dev/null 2>&1; then
            ok "$tool available"
        else
            blocker "$tool not found" "Install the Xcode command line tools: xcode-select --install"
        fi
    done
}

#----- B. identities and credentials --------------------------------------

check_identities() {
    section "B. signing identities and credentials"

    local identities
    identities="$(security find-identity -v 2>/dev/null || true)"

    if printf '%s' "$identities" | grep -q "Apple Distribution"; then
        ok "Apple Distribution certificate present"
    else
        blocker "no 'Apple Distribution' certificate in the keychain" \
                "Xcode > Settings > Accounts > Manage Certificates > + > Apple Distribution. This signs the .app. A Developer ID certificate is NOT a substitute: Developer ID is for direct download, Apple Distribution is for the store."
    fi

    # Apple renamed this certificate; both names are valid in keychains today
    # depending on when it was issued, so accept either rather than sending
    # someone to re-issue a certificate they already hold.
    if printf '%s' "$identities" | grep -qE "3rd Party Mac Developer Installer|Mac Installer Distribution"; then
        ok "Mac Installer Distribution certificate present"
    else
        blocker "no installer certificate ('3rd Party Mac Developer Installer' or 'Mac Installer Distribution')" \
                "Create it at developer.apple.com/account/resources/certificates. It signs the .pkg, and it is a DIFFERENT certificate from the one that signs the .app. Both are required."
    fi

    if [ -z "${MDT_MAS_TEAM_ID:-}" ]; then
        gap "MDT_MAS_TEAM_ID unset" \
            "Set it to the 10-character team id shown at developer.apple.com/account (top right). The entitlements template cannot be rendered without it."
    elif [ "${#MDT_MAS_TEAM_ID}" -ne 10 ]; then
        blocker "MDT_MAS_TEAM_ID='$MDT_MAS_TEAM_ID' is ${#MDT_MAS_TEAM_ID} characters, expected 10" \
                "An Apple team id is exactly 10 alphanumeric characters. A wrong one signs a valid-looking binary that App Store Connect rejects on upload."
    else
        ok "team id set (${MDT_MAS_TEAM_ID})"
    fi

    if [ -z "${MDT_MAS_ASC_KEY_ID:-}" ] || [ -z "${MDT_MAS_ASC_ISSUER_ID:-}" ]; then
        gap "App Store Connect API key not configured" \
            "Create an API key with Developer access at appstoreconnect.apple.com/access/integrations/api, then set MDT_MAS_ASC_KEY_ID and MDT_MAS_ASC_ISSUER_ID and place AuthKey_<KEYID>.p8 in ~/.appstoreconnect/private_keys/. An API key beats an app-specific password: it does not expire on a password change and is scoped to one role."
    else
        local key_path="$HOME/.appstoreconnect/private_keys/AuthKey_${MDT_MAS_ASC_KEY_ID}.p8"
        if [ -f "$key_path" ]; then
            ok "App Store Connect API key present"
        else
            blocker "MDT_MAS_ASC_KEY_ID is set but $key_path is missing" \
                    "altool looks for the private key by that exact filename. Download it once from App Store Connect (Apple allows exactly one download) and place it there."
        fi
    fi
}

#----- C. provisioning profile --------------------------------------------

bundle_id() {
    # Read the identifier from the Tauri config rather than hardcoding it, so
    # a lane-suffixed identifier is caught here instead of at upload.
    /usr/bin/plutil -extract identifier raw -o - "$CONF" 2>/dev/null \
        || grep -o '"identifier"[[:space:]]*:[[:space:]]*"[^"]*"' "$CONF" | head -1 | cut -d'"' -f4
}

check_profile() {
    section "C. provisioning profile"

    local bid
    bid="$(bundle_id)"
    ok "bundle identifier: $bid"

    if [ -z "${MDT_MAS_PROVISION_PROFILE:-}" ]; then
        gap "MDT_MAS_PROVISION_PROFILE unset" \
            "Create a 'Mac App Store' distribution profile for $bid at developer.apple.com/account/resources/profiles, download it, and point this at the .provisionprofile file."
        return
    fi
    if [ ! -f "$MDT_MAS_PROVISION_PROFILE" ]; then
        blocker "MDT_MAS_PROVISION_PROFILE points at a file that does not exist: $MDT_MAS_PROVISION_PROFILE" \
                "Download the profile again; the path is stale."
        return
    fi

    # A .provisionprofile is CMS-wrapped, so read it through security rather
    # than grepping the binary.
    local decoded
    decoded="$(security cms -D -i "$MDT_MAS_PROVISION_PROFILE" 2>/dev/null || true)"
    if [ -z "$decoded" ]; then
        blocker "cannot decode $MDT_MAS_PROVISION_PROFILE" \
                "The file is not a valid provisioning profile. Re-download it."
        return
    fi

    # The profile embeds TEAMID.bundle.id. A profile for a DIFFERENT bundle id
    # signs cleanly and is rejected on upload, which is a slow way to learn it.
    if printf '%s' "$decoded" | grep -q "$bid"; then
        ok "profile covers $bid"
    else
        blocker "profile does not mention the bundle identifier $bid" \
                "The profile was created for a different App ID. Create one for $bid, or fix bundle.identifier in $CONF."
    fi

    local expiry
    expiry="$(printf '%s' "$decoded" | grep -A1 "<key>ExpirationDate</key>" | tail -1 | sed 's/.*<date>//;s/<\/date>.*//' || true)"
    if [ -n "$expiry" ]; then
        ok "profile expires $expiry"
    fi
}

#----- D. bundle metadata the store requires ------------------------------

check_bundle_metadata() {
    section "D. bundle metadata required by App Store Connect"

    local category
    category="$(/usr/bin/plutil -extract bundle.category raw -o - "$CONF" 2>/dev/null || echo "")"
    if [ -n "$category" ]; then
        ok "bundle.category set ($category) -> LSApplicationCategoryType"
    else
        blocker "bundle.category is not set in $CONF" \
                "App Store Connect refuses a build with no LSApplicationCategoryType. Add \"category\": \"Music\" under \"bundle\" (Tauri maps it to the Info.plist key). 'Music' is the honest fit for this app."
    fi

    local minver
    minver="$(/usr/bin/plutil -extract bundle.macOS.minimumSystemVersion raw -o - "$CONF" 2>/dev/null || echo "")"
    if [ -z "$minver" ]; then
        gap "bundle.macOS.minimumSystemVersion is not set" \
            "Set it explicitly; the default may not match the architectures you ship."
    else
        local major="${minver%%.*}"
        # arm64 did not exist before macOS 11. Claiming 10.15 on an arm64-only
        # binary advertises Intel Macs that cannot run the app at all, and the
        # store will happily sell it to them.
        if [ "$major" -lt 11 ]; then
            blocker "minimumSystemVersion is $minver but this product ships arm64 only" \
                    "Either raise it to 11.0 (arm64 needs Big Sur), or build a universal binary with --target universal-apple-darwin and keep $minver. Shipping arm64-only at $minver offers the app to Intel Macs that cannot launch it."
        else
            ok "minimumSystemVersion $minver is consistent with arm64"
        fi
    fi

    for tmpl in "$APP_TEMPLATE" "$INHERIT_TEMPLATE"; do
        if [ -f "$tmpl" ]; then
            ok "entitlements template present: $(basename "$tmpl")"
        else
            blocker "missing entitlements template $tmpl" \
                    "Restore it from git; the build phase renders both."
        fi
    done
}

#----- E. sandbox reachability (the one that actually decides this) -------

check_sandbox_reachability() {
    section "E. App Sandbox reachability"

    echo "The store makes com.apple.security.app-sandbox mandatory. These are"
    echo "the paths this app reaches for today that a sandboxed process cannot,"
    echo "found by scanning the source rather than from a list that can go stale."
    echo

    local found=0

    # ~/Library/Pioneer is not user-selected and not in the container. No
    # entitlement reaches it; only an open panel the user drives can.
    local rb_hits
    rb_hits="$(grep -rln "Library/Pioneer" apps/ 2>/dev/null | grep -vE "$SCAN_NOISE" || true)"
    if [ -n "$rb_hits" ]; then
        found=1
        echo "[WARN]    reads the live rekordbox database under ~/Library/Pioneer"
        printf '          %s\n' $rb_hits
        echo "          Unreachable when sandboxed. Needs an NSOpenPanel grant plus a"
        echo "          security-scoped bookmark, or the feature is off in the store build."
    fi

    # /Volumes enumeration is worse than a blocked read: a sandboxed process
    # cannot even list the directory, so USB discovery returns empty rather
    # than failing, which reads as "no drives attached".
    local vol_hits
    vol_hits="$(grep -rln '"/Volumes"\|Path("/Volumes")' apps/ 2>/dev/null | grep -vE "$SCAN_NOISE" || true)"
    if [ -n "$vol_hits" ]; then
        found=1
        echo "[WARN]    enumerates /Volumes for USB export discovery"
        printf '          %s\n' $vol_hits
        echo "          A sandboxed process cannot LIST /Volumes. This fails silently as"
        echo "          'no drives attached' rather than raising, so it needs an explicit"
        echo "          refusal in the store build, not a fallback."
    fi

    if [ "$found" -eq 1 ]; then
        gap "the app reaches paths the sandbox forbids (detail above)" \
            "Decide per feature: gate it behind a user-selected folder grant with an app-scope bookmark, or make it refuse explicitly in the store build. Do NOT let it degrade to an empty result, which the house fail-fast rule forbids and which reads to a user as a broken app."
    else
        ok "no known-unreachable path patterns found in source"
    fi

    # A bundled interpreter is allowed. Installing code at runtime is not:
    # guideline 2.5.2 requires the app to be self-contained in its container.
    if grep -rn "pip install\|uv pip\|uv add" apps/desktop apps/engine_core 2>/dev/null | grep -v node_modules | grep -vE "\.md:|#" | head -1 | grep -q .; then
        blocker "the shipped app appears to install packages at runtime" \
                "Guideline 2.5.2 forbids it outright. Everything must be staged into the payload at build time."
    else
        ok "no runtime package installation in the shipped paths"
    fi
}

#----- F. payload signability ---------------------------------------------

check_payload() {
    section "F. bundled engine payload"

    local payload="$TAURI_DIR/payload"
    if [ ! -d "$payload" ]; then
        gap "payload not staged at $payload" \
            "Run 'just dmg' once (it builds the payload), or scripts/build_engine_payload.py directly. Preflight cannot count what must be signed until it exists."
        return
    fi

    # Every Mach-O in the bundle needs its own signature, applied inside-out.
    # The count is the point: it tells you whether signing is a step or an
    # ordeal, and it is the reason --deep looks tempting and stays banned.
    #
    # Counted with the SAME helper --build signs with, so this number cannot
    # disagree with the work it predicts. It used to count
    # -name '*.so' -o -name '*.dylib', which reported 0 for a payload holding
    # nothing but the extensionless interpreter: a zero that reads as "clean"
    # and means "not measured".
    local macho_total
    macho_total="$(macho_count "$payload")"
    ok "payload staged, $macho_total Mach-O files to sign inside-out"
    if [ "$macho_total" -gt 0 ]; then
        echo "          Signed individually by --build, deepest first, WITHOUT --deep."
        echo "          --deep would overwrite each one with the parent's entitlements,"
        echo "          handing a full sandbox profile to a Python interpreter."
    else
        gap "no Mach-O files found under $payload" \
            "The payload carries a whole CPython runtime, so an empty scan means it is not staged (or file(1) is missing), not that there is nothing to sign."
    fi
}

#----- preflight ----------------------------------------------------------

preflight() {
    echo "Open DJ -> Mac App Store preflight"
    echo "repo: $REPO_ROOT"
    check_toolchain
    check_identities
    check_profile
    check_bundle_metadata
    check_sandbox_reachability
    check_payload

    section "summary"
    echo "blockers: $BLOCKERS   gaps: $GAPS"
    if [ "$BLOCKERS" -gt 0 ]; then
        echo
        echo "REPORT TO USER: App Store preflight FAILED with $BLOCKERS blocker(s) and $GAPS gap(s). A blocker stops the build; a gap builds but App Review rejects it. Fix the [ERROR] items above in order, then re-run."
        return 1
    fi
    if [ "$GAPS" -gt 0 ]; then
        echo
        echo "REPORT TO USER: App Store preflight PASSED with $GAPS gap(s). The build can proceed, but each [WARN] above is something App Review will bounce. Clear them before submitting."
        return 0
    fi
    echo
    echo "REPORT TO USER: App Store preflight PASSED clean. Ready for --build."
    return 0
}

#----- build --------------------------------------------------------------

render_entitlements() {
    local bid="$1"
    mkdir -p "$BUILD_DIR"
    # codesign does not expand variables, so render them here. sed over a
    # template keeps the team id out of git while leaving the shipped file
    # readable and reviewable.
    sed -e "s/\$TEAM_ID/$MDT_MAS_TEAM_ID/g" -e "s/\$BUNDLE_ID/$bid/g" \
        "$APP_TEMPLATE" > "$BUILD_DIR/Entitlements.plist"
    cp "$INHERIT_TEMPLATE" "$BUILD_DIR/Entitlements.inherit.plist"
    if grep -q 'TEAM_ID' "$BUILD_DIR/Entitlements.plist"; then
        echo "[ERROR] entitlements still contain an unexpanded placeholder after rendering." >&2
        exit 1
    fi
    ok "rendered entitlements into $BUILD_DIR"
}

build() {
    require_env MDT_MAS_TEAM_ID "the build"
    require_env MDT_MAS_APP_CERT "the build"
    require_env MDT_MAS_PROVISION_PROFILE "the build"

    local bid app
    bid="$(bundle_id)"
    render_entitlements "$bid"

    section "building the app bundle"
    # --bundles app, not dmg: the store takes a .pkg built around the .app,
    # and a dmg is a container the pipeline would only have to unwrap.
    pnpm --dir apps/desktop tauri build --bundles app --target universal-apple-darwin

    app="$(ls -d "$TAURI_DIR"/target/universal-apple-darwin/release/bundle/macos/*.app 2>/dev/null | head -1)"
    [ -n "$app" ] || { echo "[ERROR] no .app produced by the build" >&2; exit 1; }
    ok "built $app"

    cp "$MDT_MAS_PROVISION_PROFILE" "$app/Contents/embedded.provisionprofile"
    ok "embedded the provisioning profile"

    section "signing inside-out"
    # The payload first, the bundle that contains it LAST. Signing outside-in
    # invalidates the outer signature the moment an inner one is written.
    # Within the payload the order does not matter: these are standalone
    # Mach-O files, not nested bundles, so none carries an enclosing signature
    # for another to invalidate.
    local payload signed_count
    payload="$app/Contents/Resources/payload"
    [ -d "$payload" ] || { echo "[ERROR] the built app carries no payload at $payload; the engine is missing, not merely unsigned." >&2; exit 1; }

    # Mach-O by CONTENT, never by extension. The predicate this replaced was
    # -name '*.so' -o -name '*.dylib', which skipped the single binary that
    # matters most: the bundled CPython at runtime/bin/python3.N. It carries
    # no extension, and it is the process the launcher execs, so it is the
    # binary the inherited sandbox actually has to land on.
    signed_count="$(macho_count "$payload")"
    [ "$signed_count" -gt 0 ] || { echo "[ERROR] found no Mach-O files under $payload; the payload carries a whole CPython runtime, so an empty scan means the scan is broken, not that the payload is clean." >&2; exit 1; }
    macho_files "$payload" | xargs -0 -n 1 \
        codesign --force --timestamp --options runtime \
            --sign "$MDT_MAS_APP_CERT" \
            --entitlements "$BUILD_DIR/Entitlements.inherit.plist"
    ok "signed $signed_count Mach-O files in the payload with the inherited sandbox"

    # Assert the interpreter by NAME, not by count. A count cannot tell 90
    # signed dylibs from 90 signed dylibs plus an unsigned interpreter, which
    # is exactly the shape of the bug this replaced. The pattern matches the
    # versioned real file (python3.11, python3.14); bin/python3 itself is a
    # symlink and macho_files skips symlinks on purpose.
    macho_files "$payload" | tr '\0' '\n' | grep -q '/runtime/bin/python3' || {
        echo "[ERROR] the signed set contains no runtime/bin/python3*; the launcher execs that interpreter, so an unsigned one is an App Store rejection." >&2
        exit 1
    }
    ok "the bundled interpreter is in the signed set"

    # bin/opendj-engine is deliberately NOT signed on its own. It is a POSIX
    # shell script (LAUNCHER_TEMPLATE in scripts/build_engine_payload.py opens
    # with #!/bin/sh), so it has no load commands to seal and codesign would
    # only hang a detached signature off an extended attribute. Two things
    # already cover it: the outer bundle's CodeResources seals it as a
    # resource, and the inherited-sandbox entitlement lands where it takes
    # effect, on the interpreter it execs. Its ABSENCE is still fatal, so that
    # is checked rather than silently skipped.
    [ -f "$payload/bin/opendj-engine" ] || {
        echo "[ERROR] the payload has no engine launcher at bin/opendj-engine; the desktop shell has nothing to exec." >&2
        exit 1
    }
    ok "engine launcher present (a shell script; the interpreter it execs is signed above)"

    # The outer bundle LAST, and never with --deep.
    codesign --force --timestamp --options runtime \
        --sign "$MDT_MAS_APP_CERT" \
        --entitlements "$BUILD_DIR/Entitlements.plist" \
        "$app"
    ok "signed the app bundle"

    codesign --verify --deep --strict --verbose=2 "$app"
    ok "signature verifies"
    echo "$app" > "$BUILD_DIR/app-path"
}

#----- package ------------------------------------------------------------

package() {
    require_env MDT_MAS_INSTALLER_CERT "packaging"
    local app pkg
    app="$(cat "$BUILD_DIR/app-path" 2>/dev/null || true)"
    [ -n "$app" ] && [ -d "$app" ] || { echo "[ERROR] no signed .app recorded; run --build first." >&2; exit 1; }
    pkg="$BUILD_DIR/$(basename "${app%.app}").pkg"

    section "building the installer package"
    productbuild --sign "$MDT_MAS_INSTALLER_CERT" \
        --component "$app" /Applications "$pkg"
    ok "built $pkg"
    echo "$pkg" > "$BUILD_DIR/pkg-path"
}

#----- upload -------------------------------------------------------------

upload() {
    require_env MDT_MAS_ASC_KEY_ID "the upload"
    require_env MDT_MAS_ASC_ISSUER_ID "the upload"
    local pkg
    pkg="$(cat "$BUILD_DIR/pkg-path" 2>/dev/null || true)"
    [ -n "$pkg" ] && [ -f "$pkg" ] || { echo "[ERROR] no .pkg recorded; run --package first." >&2; exit 1; }

    section "validating before upload"
    # Validation catches most rejections in seconds, before a full transfer.
    xcrun altool --validate-app --type macos --file "$pkg" \
        --apiKey "$MDT_MAS_ASC_KEY_ID" --apiIssuer "$MDT_MAS_ASC_ISSUER_ID"
    ok "validation passed"

    section "uploading"
    # --upload-package, NOT --upload-app: Apple deprecated the latter.
    xcrun altool --upload-package "$pkg" --type macos \
        --apiKey "$MDT_MAS_ASC_KEY_ID" --apiIssuer "$MDT_MAS_ASC_ISSUER_ID"
    ok "uploaded"
    echo
    echo "REPORT TO USER: uploaded $(basename "$pkg") to App Store Connect. Processing takes 5 to 30 minutes; the build appears under TestFlight and can then be attached to a version. Submitting for review is a browser step, not a CLI one."
}

#----- main ---------------------------------------------------------------

case "$MODE" in
    preflight) preflight ;;
    build)     preflight && build ;;
    package)   package ;;
    upload)    upload ;;
    all)       preflight && build && package && upload ;;
esac
