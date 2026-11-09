# Open DJ Track Catcher (Mac app)

A small always-on-top window that shows the live key, notes and BPM of what's
playing and catches the track into the Open DJ Inbox. The UI contract is the
design artifact (v16); requirements are TCAPP-6577b..6577i, the decision record is
`(private path)`.

## Build and run (macOS 14+)

    # The repo's own interpreter, resolved once to an absolute path, so a PATH
    # shim or a stray python3 never runs the bundler.
    PY="$(uv run --locked python -c 'import sys; print(sys.executable)')"
    "$PY" apps/track-catcher-mac/scripts/bundle.py --sign "<your code-signing identity>"
    open "apps/track-catcher-mac/dist/Open DJ Track Catcher.app"

Sign every build with the same identity, or macOS asks for the mic again
after each rebuild. `security find-identity -v -p codesigning` lists yours.

Catch uses ShazamKit, which matches songs only when the bundle id
(`com.opendj.trackcatcher`) has the ShazamKit App Service turned on in the
Apple developer account and the app is signed with that team's identity.
Without it, Catch reports no match for every song.

## Test

    (cd apps/live-core && cargo build --release)
    ODJ_LIVE=$PWD/apps/live-core/target/release/odj-live swift test --package-path apps/track-catcher-mac

The core tests run on Linux too.

## Screenshots of every state

    "apps/track-catcher-mac/dist/Open DJ Track Catcher.app/Contents/MacOS/TrackCatcher" --snapshot /tmp/tc-shots

## CLI

    track-catcher catch [--playlist NAME]
    track-catcher now-playing [--playlist NAME]
    track-catcher listen | stop | tap | copy | settings | setup | quit
    track-catcher analyze FILE
    track-catcher wheel 12B
    track-catcher inbox
