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

require_command gh
require_command python3
[ "$(uname -s)" = "Darwin" ] || die "just release requires macOS for Developer ID and notarization verification"
require_command xcrun
require_command spctl

[ -z "${MDT_SHIP_UNSIGNED:-}" ] || die "MDT_SHIP_UNSIGNED must not be set for just release"
[ -n "${MDT_MACOS_SIGNING_IDENTITY:-}" ] || die "MDT_MACOS_SIGNING_IDENTITY is required for just release"
[ -n "${MDT_MACOS_NOTARY_KEYCHAIN_PROFILE:-}" ] || die "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is required for just release"
[ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ] || die "TAURI_SIGNING_PRIVATE_KEY is required for just release"

version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$CONF")"
tag="v$version"
git_sha="$(git -C "$ROOT" rev-parse HEAD)"

git -C "$ROOT" diff --quiet || die "release requires a clean tracked working tree"
git -C "$ROOT" diff --cached --quiet || die "release requires a clean index"

cd "$ROOT"
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

manifest = json.loads(Path(os.environ["MANIFEST"]).read_text(encoding="utf-8"))
entry = manifest.get("platforms", {}).get("darwin-aarch64", {})
if manifest.get("version") != os.environ["VERSION"]:
    raise SystemExit("existing latest.json has a different version")
if os.environ["GIT_SHA"] not in str(manifest.get("notes", "")):
    raise SystemExit("existing latest.json does not name this build SHA")
if not isinstance(entry.get("signature"), str) or not entry["signature"].strip():
    raise SystemExit("existing latest.json has no updater signature")
if f"/releases/download/v{os.environ['VERSION']}/" not in str(entry.get("url", "")):
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
archive="$(find "$MACOS_DIR" -maxdepth 1 -type f -name '*.app.tar.gz' -print -quit)"
[ -n "$archive" ] || die "just dmg produced no updater archive in $MACOS_DIR"
signature="$archive.sig"
[ -f "$signature" ] || die "just dmg produced no updater signature for $archive"

xcrun stapler validate "$app"
spctl -a -t exec -vv "$app"
xcrun stapler validate "$dmg"
spctl -a -t open -vv "$dmg"

manifest="$(mktemp "${TMPDIR:-/tmp}/opendj-latest.XXXXXX.json")"
cleanup() { rm -f "$manifest"; }
trap cleanup EXIT
PUBLIC_REPO="$PUBLIC_REPO" VERSION="$version" GIT_SHA="$git_sha" ARCHIVE_NAME="$(basename "$archive")" SIGNATURE="$signature" MANIFEST="$manifest" python3 - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

signature = Path(os.environ["SIGNATURE"]).read_text(encoding="utf-8").strip()
if not signature:
    raise SystemExit("updater signature is empty")
repo = os.environ["PUBLIC_REPO"]
asset = os.environ["ARCHIVE_NAME"]
manifest = {
    "version": os.environ["VERSION"],
    "notes": f"built from {os.environ['GIT_SHA']}",
    "pub_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "platforms": {
        "darwin-aarch64": {
            "signature": signature,
            "url": f"https://github.com/{repo}/releases/download/v{os.environ['VERSION']}/{asset}",
        }
    },
}
Path(os.environ["MANIFEST"]).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

assets=("$dmg" "$archive" "$signature" "$manifest")
gh release create "$tag" "${assets[@]}" --repo "$PUBLIC_REPO" --title "Open DJ $version" --notes "built from $git_sha"
echo "[OK] published $tag to $PUBLIC_REPO"
