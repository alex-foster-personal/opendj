#!/usr/bin/env bash
# Publish a signed, notarized Open DJ release to the public updater host.
#
# The host is intentionally separate from the private product repository:
# unauthenticated Tauri clients need to fetch latest.json and its asset. A
# release tag is immutable. Retrying a completed release validates its public
# manifest and returns without rebuilding or replacing any published bytes.
#
# Requirements:
# - macOS with the Developer ID identity and notarytool profile configured.
# - TAURI_SIGNING_PRIVATE_KEY supplied through Doppler by the invoking shell.
# - gh authenticated with write access to maintainer/issue-assets.
#
# Acceptance:
# - [if] MDT_SHIP_UNSIGNED is set [then] release exits before building -> broken.
# - [if] the app or dmg lacks a stapled notary ticket [then] release exits -> broken.
# - [if] an existing version lacks a matching signed manifest [then] release
#   refuses it rather than replacing published bytes -> broken.
# - [if] tauri.conf.json version is not strictly greater than the latest
#   published release [then] release exits before building -> broken.

set -euo pipefail

PUBLIC_REPO="maintainer/issue-assets"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/apps/desktop/src-tauri/tauri.conf.json"
MACOS_DIR="$ROOT/apps/desktop/src-tauri/target/release/bundle/macos"
DMG_DIR="$ROOT/apps/desktop/src-tauri/target/release/bundle/dmg"

die() {
    echo "[ERROR] $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

require_command python3

CHANNEL="nightly"
while [ $# -gt 0 ]; do
    case "$1" in
        --)
            shift
            ;;
        --channel)
            [ $# -ge 2 ] || die "--channel needs a value (stable or nightly)"
            CHANNEL="$2"
            shift 2
            ;;
        --channel=*)
            CHANNEL="${1#--channel=}"
            shift
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done
case "$CHANNEL" in
    stable|nightly) ;;
    *) die "unknown --channel $CHANNEL (expected stable or nightly)" ;;
esac

cd "$ROOT"
git_sha="$(git -C "$ROOT" rev-parse HEAD)"

# OPS-18: the stable gate names missing evidence BEFORE Darwin/signing spend,
# so `just release --channel stable` on Linux (and on a Mac with no file)
# reports the hole instead of "requires macOS".
if [ "$CHANNEL" = "stable" ]; then
    python3 -m scripts.stable_evidence gate --sha "$git_sha" \
        || die "stable channel refused for $git_sha"
    OPENDJ_EVIDENCE_WRITTEN_AT_UTC="$(python3 -m scripts.stable_evidence written-at --sha "$git_sha")"
    export OPENDJ_EVIDENCE_WRITTEN_AT_UTC
fi
export OPENDJ_RELEASE_CHANNEL="$CHANNEL"
export CHANNEL

[ "$(uname -s)" = "Darwin" ] || die "just release requires macOS for Developer ID and notarization verification"
require_command gh
require_command xcrun
require_command spctl

[ -z "${MDT_SHIP_UNSIGNED:-}" ] || die "MDT_SHIP_UNSIGNED must not be set for just release"
[ -n "${MDT_MACOS_SIGNING_IDENTITY:-}" ] || die "MDT_MACOS_SIGNING_IDENTITY is required for just release"
[ -n "${MDT_MACOS_NOTARY_KEYCHAIN_PROFILE:-}" ] || die "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is required for just release"
[ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ] || die "TAURI_SIGNING_PRIVATE_KEY is required for just release"

version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$CONF")"
tag="$(python3 -m scripts.release_identity --config "$CONF")"

git -C "$ROOT" diff --quiet || die "release requires a clean tracked working tree"
git -C "$ROOT" diff --cached --quiet || die "release requires a clean index"

cd "$ROOT"
python3 -m scripts.release_semver check --config "$CONF" --repo "$PUBLIC_REPO"

if gh release view "$tag" --repo "$PUBLIC_REPO" >/dev/null 2>&1; then
    existing_dir="$(mktemp -d "${TMPDIR:-/tmp}/opendj-existing-release.XXXXXX")"
    cleanup() { rm -rf "$existing_dir"; }
    trap cleanup EXIT
    gh release download "$tag" --repo "$PUBLIC_REPO" --dir "$existing_dir" --pattern latest.json
    [ -f "$existing_dir/latest.json" ] || die "release $tag exists without latest.json"
    VERSION="$version" GIT_SHA="$git_sha" MANIFEST="$existing_dir/latest.json" python3 - <<'PY'
import json
import os
from pathlib import Path

from scripts.release_identity import release_asset_url, release_tag

manifest = json.loads(Path(os.environ["MANIFEST"]).read_text(encoding="utf-8"))
entry = manifest.get("platforms", {}).get("darwin-aarch64", {})
if manifest.get("version") != os.environ["VERSION"]:
    raise SystemExit("existing latest.json has a different version")
existing_channel = manifest.get("channel")
if existing_channel is None:
    if os.environ["CHANNEL"] != "nightly":
        raise SystemExit("existing latest.json has no channel; only a nightly retry is allowed")
elif existing_channel != os.environ["CHANNEL"]:
    raise SystemExit("existing latest.json has a different channel")
if os.environ["GIT_SHA"] not in str(manifest.get("notes", "")):
    raise SystemExit("existing latest.json does not name this build SHA")
if not isinstance(entry.get("signature"), str) or not entry["signature"].strip():
    raise SystemExit("existing latest.json has no updater signature")
if f"/releases/download/{release_tag(os.environ['VERSION'])}/" not in str(entry.get("url", "")):
    raise SystemExit("existing latest.json does not point at its immutable release tag")
PY
    echo "[OK] release $tag already exists with a valid manifest"
    exit 0
fi

just dmg

app="$(find "$MACOS_DIR" -maxdepth 1 -type d -name '*.app' -print -quit)"
[ -n "$app" ] || die "just dmg produced no app bundle in $MACOS_DIR"
dmg="$(find "$DMG_DIR" -maxdepth 1 -type f -name '*.dmg' -print -quit)"
[ -n "$dmg" ] || die "just dmg produced no dmg in $DMG_DIR"
built_archive="$(find "$MACOS_DIR" -maxdepth 1 -type f -name '*.app.tar.gz' -print -quit)"
[ -n "$built_archive" ] || die "just dmg produced no updater archive in $MACOS_DIR"
[ -f "$built_archive.sig" ] || die "just dmg produced no updater signature for $built_archive"
# The updater plugin gunzips the download; a ZIP under the .tar.gz name is
# refused with "invalid gzip header" and the app stays put (v0.1.2 to v0.1.4).
archive_magic="$(head -c 2 "$built_archive" | od -An -tx1 | tr -d ' \n')"
[ "$archive_magic" = "1f8b" ] || die "updater archive $built_archive is not gzip (magic $archive_magic); refusing to publish what the updater cannot install"
# Tauri names the archive after the product ("Open DJ.app.tar.gz"). GitHub
# stores an uploaded asset under a space-free name ("Open.DJ.app.tar.gz"), so
# a manifest URL carrying the space 404s and the in-app updater never installs
# (v0.1.2, Tue 15 Sep 2026). Publish the same bytes under a name GitHub keeps.
# The signature is over the bytes, so the rename does not invalidate it.
archive="$MACOS_DIR/OpenDJ-$version-aarch64.app.tar.gz"
signature="$archive.sig"
cp "$built_archive" "$archive"
cp "$built_archive.sig" "$signature"

xcrun stapler validate "$app"
spctl -a -t exec -vv "$app"
xcrun stapler validate "$dmg"
# A disk image carries no launch context of its own: without an explicit
# primary-signature context, macOS 26.6.1 answers "rejected, source=Insufficient
# Context" for a notarized, stapled dmg (measured on the Air, Tue 15 Sep 2026).
spctl -a -t open --context context:primary-signature -vv "$dmg"

# The asset is uploaded under its FILENAME, and the updater endpoint fetches
# `latest.json`. So it is named exactly that, inside a unique directory: BSD
# mktemp randomizes only trailing X's, and the old `opendj-latest.XXXXXX.json`
# template would have published an asset literally named that.
manifest_dir="$(mktemp -d "${TMPDIR:-/tmp}/opendj-latest.XXXXXX")"
manifest="$manifest_dir/latest.json"
cleanup() { rm -rf "$manifest_dir"; }
trap cleanup EXIT
PUBLIC_REPO="$PUBLIC_REPO" VERSION="$version" GIT_SHA="$git_sha" ARCHIVE_NAME="$(basename "$archive")" SIGNATURE="$signature" MANIFEST="$manifest" python3 - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from scripts.release_identity import release_asset_url, release_tag

signature = Path(os.environ["SIGNATURE"]).read_text(encoding="utf-8").strip()
if not signature:
    raise SystemExit("updater signature is empty")
repo = os.environ["PUBLIC_REPO"]
asset = os.environ["ARCHIVE_NAME"]
manifest = {
    "version": os.environ["VERSION"],
    "notes": f"built from {os.environ['GIT_SHA']}",
    "pub_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "channel": os.environ["CHANNEL"],
    "platforms": {
        "darwin-aarch64": {
            "signature": signature,
            "url": release_asset_url(repo, os.environ["VERSION"], asset),
        }
    },
}
Path(os.environ["MANIFEST"]).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

assets=("$dmg" "$archive" "$signature" "$manifest")
gh release create "$tag" "${assets[@]}" --repo "$PUBLIC_REPO" --title "Open DJ $version" --notes "built from $git_sha"
echo "[OK] published $tag to $PUBLIC_REPO"
