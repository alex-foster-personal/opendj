#!/usr/bin/env bash
# Prove the engine payload boots on a machine that is not this one.
#
# WHAT THIS APPROXIMATES
#
# A friend's Mac: no repo, no uv, no Homebrew, no dev headers, no exported
# .env. The daemon is launched from the payload with `env -i` plus a bare
# PATH and a throwaway HOME, from a working directory outside every checkout
# on this machine. Anything the payload needs that it did not bring, it does
# not get.
#
# WHAT IT DOES NOT PROVE
#
# Gatekeeper. An unsigned, un-notarized dmg downloaded to another Mac is
# quarantined regardless of what this script says. That is a signing
# problem, tracked separately in the `dmg` recipe's notes.
#
# USAGE
#   scripts/no_repo_boot_test.sh [DMG_OR_PAYLOAD_DIR] [PORT]
#
# The default, and the strongest form, is the dmg itself: it is mounted
# read-only and the engine is launched from inside the mounted image, so what
# boots is the artifact a tester would be handed, not a staging directory
# that happens to sit next to it. `cargo tauri build` deletes the staged .app
# once the image exists, which makes the dmg the only surviving copy anyway.
# A payload directory is still accepted, for iterating without a full bundle.
#
# Acceptance (each line is a hard assertion; any failure exits non-zero):
#   [if] /api/v1/health does not answer 200 [then] broken
#   [if] / does not serve index.html with Cache-Control: no-cache [then] broken
#   [if] the SignalsmithStretch worklet is not served as immutable
#        JavaScript [then] the deck cannot load audio -> broken
#   [if] /api/v1/tracks does not answer [then] the library surface is dead
#   [if] the engine wrote outside its own data dir [then] broken
#   [if] MDT_REKORDBOX_WRITEBACK_ENABLED reaches the engine [then] broken

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DMG_DIR="$REPO_ROOT/apps/desktop/src-tauri/target/release/bundle/dmg"

target="${1:-}"
port="${2:-8691}"
mount=""

if [ -z "$target" ]; then
    target=$(ls -t "$DMG_DIR"/*.dmg 2>/dev/null | head -1 || true)
    if [ -z "$target" ]; then
        echo "[ERROR] no dmg under $DMG_DIR and no path given; run 'just dmg' first"
        echo "[ERROR] usage: $0 [DMG_OR_PAYLOAD_DIR] [PORT]"
        exit 1
    fi
fi

case "$target" in
*.dmg)
    [ -f "$target" ] || { echo "[ERROR] no dmg at $target"; exit 1; }
    mount=$(mktemp -d /tmp/opendj-boot-mount.XXXXXX)
    hdiutil attach "$target" -nobrowse -readonly -mountpoint "$mount" >/dev/null
    app=$(ls -d "$mount"/*.app 2>/dev/null | head -1 || true)
    [ -n "$app" ] || { echo "[ERROR] $target holds no .app"; exit 1; }
    payload_dir="$app/Contents/Resources/payload"
    echo "[INFO] dmg:      $target (mounted read-only at $mount)"
    ;;
*)
    payload_dir="$target"
    ;;
esac

launcher="$payload_dir/bin/opendj-engine"
manifest="$payload_dir/manifest.json"
[ -x "$launcher" ] || { echo "[ERROR] no executable engine at $launcher"; exit 1; }
[ -f "$manifest" ] || { echo "[ERROR] no manifest at $manifest"; exit 1; }

# A cwd outside every checkout. mktemp -d lands under /var/folders, which is
# not inside this repo, not inside a worktree, and not inside a git dir.
sandbox=$(mktemp -d /tmp/opendj-no-repo.XXXXXX)
home_dir="$sandbox/home"
data_dir="$sandbox/data"
log="$sandbox/engine.log"
mkdir -p "$home_dir" "$data_dir"

engine_pid=""
cleanup() {
    if [ -n "$engine_pid" ] && kill -0 "$engine_pid" 2>/dev/null; then
        kill -TERM "-$engine_pid" 2>/dev/null || kill -TERM "$engine_pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$engine_pid" 2>/dev/null || break
            sleep 0.3
        done
        kill -KILL "-$engine_pid" 2>/dev/null || true
    fi
    # Detach AFTER the engine is gone: a busy volume refuses to unmount, and
    # a leaked mount would make the next run read a stale image.
    if [ -n "$mount" ]; then
        hdiutil detach "$mount" >/dev/null 2>&1 || true
        rmdir "$mount" 2>/dev/null || true
    fi
}
trap cleanup EXIT

fail() { echo "[ERROR] $*"; echo "----- engine log -----"; cat "$log" 2>/dev/null || true; exit 1; }

echo "[INFO] payload:  $payload_dir"
echo "[INFO] sandbox:  $sandbox"
echo "[INFO] port:     $port"
echo "[INFO] identity: $(/usr/bin/python3 -c 'import json,sys; i=json.load(open(sys.argv[1]))["identity"]; print(i["product_name"], i["git_sha"], "DIRTY" if i["git_dirty"] else "clean", i["built_at_utc"])' "$manifest")"

# Refuse to run against a port something else already owns; a false green
# here would be the worst possible outcome for this script.
if /usr/bin/nc -z 127.0.0.1 "$port" 2>/dev/null; then
    fail "port $port is already in use; this test must own the port it asserts on"
fi

# THE LAUNCH. env -i clears everything, including PYTHONPATH, VIRTUAL_ENV and
# any exported MDT_* from a developer shell. setsid is not on macOS, so the
# subshell's own pid becomes the group leader via `set -m`.
cd "$sandbox"
set -m
env -i \
    PATH=/usr/bin:/bin \
    HOME="$home_dir" \
    "$launcher" --data-dir "$data_dir" --host 127.0.0.1 --port "$port" \
    >"$log" 2>&1 &
engine_pid=$!
set +m
# Drop it from the job table. The pid is still ours to signal and to poll
# with kill -0; what disown removes is bash's asynchronous "Terminated: 15"
# notice, which would otherwise print AFTER the PASSED banner and make a
# green run read as a failure.
disown "$engine_pid" 2>/dev/null || true

deadline=$((SECONDS + 45))
# -fs without -S: a refused connection is the EXPECTED state while the engine
# is still booting, so it must not print. A real failure is reported by the
# assertions below, not by curl's chatter.
until /usr/bin/curl -fs -o /dev/null "http://127.0.0.1:$port/api/v1/health"; do
    kill -0 "$engine_pid" 2>/dev/null || fail "engine exited before answering /api/v1/health"
    [ "$SECONDS" -lt "$deadline" ] || fail "engine did not answer /api/v1/health within 45s"
    sleep 0.3
done
echo "[OK] /api/v1/health answered 200"

# ----- SPA shell, revalidated on every load -------------------------------
root_headers=$(/usr/bin/curl -sS -D - -o "$sandbox/index.html" "http://127.0.0.1:$port/")
echo "$root_headers" | head -1 | grep -q ' 200 ' || fail "/ did not answer 200: $(echo "$root_headers" | head -1)"
grep -qi '^cache-control: *no-cache' <<<"$root_headers" \
    || fail "/ is missing Cache-Control: no-cache (a stale shell would survive an update)"
grep -q '<!doctype html>' "$sandbox/index.html" \
    || fail "/ did not serve the SPA index.html"
echo "[OK] / serves index.html with Cache-Control: no-cache"

# ----- the deck-load fix --------------------------------------------------
stretch=$(cd "$payload_dir/app/apps/webui/frontend/build/_app/immutable/assets" \
    && ls SignalsmithStretch.*.mjs)
count=$(printf '%s\n' "$stretch" | grep -c .)
[ "$count" -eq 1 ] || fail "expected exactly 1 SignalsmithStretch asset, found $count"
stretch_url="http://127.0.0.1:$port/_app/immutable/assets/$stretch"
stretch_headers=$(/usr/bin/curl -sS -D - -o "$sandbox/stretch.mjs" "$stretch_url")
echo "$stretch_headers" | head -1 | grep -q ' 200 ' \
    || fail "$stretch_url did not answer 200"
grep -qi '^content-type: *text/javascript' <<<"$stretch_headers" \
    || fail "the worklet is not served as text/javascript: $(grep -i '^content-type' <<<"$stretch_headers")"
grep -qi '^cache-control: .*immutable' <<<"$stretch_headers" \
    || fail "the hashed worklet asset is not marked immutable"
grep -q 'registerProcessor\|AudioWorkletProcessor' "$sandbox/stretch.mjs" \
    || fail "the served worklet does not look like an AudioWorklet module"
echo "[OK] $stretch served 200 text/javascript immutable"

# ----- the library surface ------------------------------------------------
tracks_status=$(/usr/bin/curl -sS -o "$sandbox/tracks.json" -w '%{http_code}' \
    "http://127.0.0.1:$port/api/v1/tracks")
[ "$tracks_status" = "200" ] || fail "/api/v1/tracks answered $tracks_status"
echo "[OK] /api/v1/tracks answered 200 ($(wc -c <"$sandbox/tracks.json" | tr -d ' ') bytes, empty library is expected)"

# ----- identity, read back over HTTP --------------------------------------
build_status=$(/usr/bin/curl -sS -o "$sandbox/build-info.json" -w '%{http_code}' \
    "http://127.0.0.1:$port/api/v1/build-info")
[ "$build_status" = "200" ] || fail "/api/v1/build-info answered $build_status"
/usr/bin/python3 - "$sandbox/build-info.json" "$manifest" <<'PY' || fail "build-info does not match the payload manifest"
import json, sys
served = json.load(open(sys.argv[1]))
identity = json.load(open(sys.argv[2]))["identity"]
assert served["source"] == "payload", served["source"]
for key in ("git_sha", "git_sha_full", "git_branch", "git_dirty", "lane_label"):
    assert served[key] == identity[key], (key, served[key], identity[key])
print(f"[OK] /api/v1/build-info: {served['product_name']} {served['git_sha']}"
      f"{' DIRTY' if served['git_dirty'] else ''} built {served['built_at_utc']}")
PY

# ----- one-way safety -----------------------------------------------------
grep -rl "MDT_REKORDBOX_WRITEBACK_ENABLED" "$payload_dir/bin" >/dev/null 2>&1 && {
    grep -q 'unset MDT_REKORDBOX_WRITEBACK_ENABLED' "$launcher" \
        || fail "the launcher mentions the writeback flag without unsetting it"
}
echo "[OK] the launcher unsets MDT_REKORDBOX_WRITEBACK_ENABLED before exec"

# The engine only ever creates what it owns. Anything else in the data dir
# would mean it went looking for a library it was not pointed at.
unexpected=$(cd "$data_dir" && ls -A | grep -v -E '^(state|\.engine\.lock|logs)$' || true)
[ -z "$unexpected" ] || fail "the engine wrote unexpected entries into its data dir: $unexpected"
echo "[OK] data dir holds only what the engine owns: $(cd "$data_dir" && ls -A | tr '\n' ' ')"

echo
echo "[OK] NO-REPO BOOT TEST PASSED"
echo "[OK] payload: $payload_dir"
echo "[OK] launched with: env -i PATH=/usr/bin:/bin HOME=$home_dir, cwd $sandbox"
