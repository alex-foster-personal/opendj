#!/usr/bin/env bash
# Seed genre provider keys into a DEV build's Keychain service (GENRE-09).
#
#   scripts/dev_seed_keys.sh --app "/Applications/Open DJ.app"
#
# Reads OPENROUTER_API_KEY and SERPER_API_KEY from Doppler general/dev_personal
# and stores them under the app's Keychain service (apps/genre_infer/keystore.py).
# Dev only: refuses unless the app's payload manifest says
# release_channel=local-unnotarized (a --local-fast build). Open DJ has ONE
# bundle id (com.opendj.desktop), so the channel is what tells dev from release.
# Keys never enter the built artifact; values never reach stdout or argv logs.
#
# -Claude
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP=""
while [ $# -gt 0 ]; do
    case "$1" in
        --app)
            APP="$2"
            shift 2
            ;;
        *)
            echo "[ERROR] unknown argument $1" >&2
            exit 2
            ;;
    esac
done
[ -n "$APP" ] || {
    echo "[ERROR] --app <path to .app> is required" >&2
    exit 2
}
manifest="$APP/Contents/Resources/payload/manifest.json"
[ -f "$manifest" ] || {
    echo "[ERROR] no payload manifest at $manifest" >&2
    exit 2
}
channel=$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("identity") or {}).get("release_channel",""))' "$manifest")
if [ "$channel" != "local-unnotarized" ]; then
    echo "[ERROR] refusing to seed keys: $APP is release_channel=${channel:-<none>}, not a local-unnotarized dev build" >&2
    exit 3
fi
command -v doppler >/dev/null || {
    echo "[ERROR] doppler CLI not found; cannot read dev keys" >&2
    exit 3
}
doppler run -p general -c dev_personal -- bash -c '
  set -euo pipefail
  for k in OPENROUTER_API_KEY SERPER_API_KEY; do
    [ -n "${!k:-}" ] || { echo "[ERROR] $k missing from Doppler general/dev_personal" >&2; exit 3; }
    printf "%s\n" "${!k}" | env -u OPENROUTER_API_KEY -u SERPER_API_KEY \
      uv run --project "$0" --no-sync python -m apps.genre_infer keys set "$k" >/dev/null
    echo "[OK] $k seeded into the dev Keychain service"
  done
' "$ROOT"
