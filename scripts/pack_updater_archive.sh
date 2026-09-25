#!/usr/bin/env bash
# Pack a stapled .app into the updater archive the Tauri updater plugin can install.
#
# Usage: scripts/pack_updater_archive.sh <App.app> <out.app.tar.gz>
#
# The plugin (tauri-plugin-updater 2.x, macOS) reads the download as a gzip
# stream over a tar archive whose top-level entry is the .app directory. The
# dmg recipe used to rebuild this file with `ditto -c -k`, which writes a ZIP,
# so every published archive since v0.1.2 was refused with "invalid gzip
# header" (first visible Tue 15 Sep 2026 through the apply result endpoint).
# This script is the single writer of that file and proves the magic bytes
# before it returns, so a wrong container fails the build, never the update.
set -euo pipefail

app="${1:?usage: pack_updater_archive.sh <App.app> <out.app.tar.gz>}"
out="${2:?usage: pack_updater_archive.sh <App.app> <out.app.tar.gz>}"
[ -d "$app" ] || { echo "[ERROR] not an app bundle directory: $app" >&2; exit 2; }
case "$out" in *.app.tar.gz) ;; *) echo "[ERROR] archive name must end in .app.tar.gz: $out" >&2; exit 2 ;; esac

rm -f "$out"
# COPYFILE_DISABLE keeps bsdtar from adding AppleDouble ._ entries, which would
# land inside the installed bundle and break its seal.
COPYFILE_DISABLE=1 tar -czf "$out" -C "$(dirname "$app")" "$(basename "$app")"

# `… | head` under pipefail SIGPIPEs tar/od (exit 141) on some CI runners.
magic="$(od -An -tx1 -N 2 "$out" | tr -d ' \n')"
[ "$magic" = "1f8b" ] || { echo "[ERROR] $out is not gzip (magic $magic); the updater would refuse it" >&2; exit 3; }
listing="$(tar -tzf "$out")"
first="${listing%%$'\n'*}"
[ "$first" = "$(basename "$app")/" ] || { echo "[ERROR] archive top-level entry is '$first', expected '$(basename "$app")/'" >&2; exit 3; }
echo "[OK] updater archive $out: gzip, top-level $(basename "$app")/, $(stat -f %z "$out" 2>/dev/null || stat -c %s "$out") bytes"
