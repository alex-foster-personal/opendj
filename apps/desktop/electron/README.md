# Electron desktop shell

The Open DJ desktop shell on Electron, built beside the Tauri shell
(`../src-tauri/`), which keeps shipping until the cutover plan 21-02. Why and
how: `(private path)` and
`(private path)/`.

The shell does what the Tauri shell does and nothing more. It starts or adopts
the bundled engine, supervises it, serves shell health, and opens one Chromium
window onto the engine's loopback origin. The page reaches four native
features (folder picker, open in browser, quit, updater) through
`window.opendjShell`, and the web UI's `src/lib/shell/native-shell.ts` picks
whichever shell is present.

## Layout

| File | Port of (`../src-tauri/src/`) |
|---|---|
| `src/main.ts` | `main.rs`: flags, single instance, window, quit gate, IPC |
| `src/preload.ts` | the `initialization_script` globals, plus the bridge |
| `src/engine.ts`, `src/engine-log.ts`, `src/shell-log.ts` | `engine.rs`, `engine_log.rs` |
| `src/launch.ts`, `src/boot.ts` | `launch.rs`, `start_engine` in `main.rs` |
| `src/supervisor.ts` | `supervisor.rs` (INSTALL-23) |
| `src/shell-health.ts`, `src/output-health.ts` | `shell_health.rs`, `output_health.rs` (probe pending, D7) |
| `src/updater.ts` | the updater plugin (OPS-15) |
| `src/policy.ts` | flags, window size, build identity, trusted origins, permissions |

## Commands

```sh
pnpm install --frozen-lockfile   # downloads the Electron runtime
pnpm test                        # node:test, real processes, no Electron binary needed
xvfb-run -a pnpm e2e             # the real app on Linux; on a Mac, plain `pnpm e2e`
OPENDJ_BUILD_LANE_LABEL=electron OPENDJ_BUILD_CHANNEL=dev pnpm pack:dir   # unsigned macOS arm64 .app (darwin only)
```

`pnpm pack:dir` compiles `dist/main.js`, stamps `dist/build-identity.json`, then
runs `electron-builder --dir --mac --arm64` with `CSC_IDENTITY_AUTO_DISCOVERY=false`
and `mac.identity` null, so it does not sign, notarize, or open a keychain dialog.
The `.app` lands at `out/mac-arm64/Open DJ.app`. `payload/` is an empty
`extraResources` placeholder (see `payload/README.md`); it is not a built engine
payload.

The stamp (`scripts/stamp-build.mjs`) reads the lane label and release channel
from `OPENDJ_BUILD_LANE_LABEL` and `OPENDJ_BUILD_CHANNEL`, the same two variables
the dmg recipe exports for the Tauri build, and has no default for either. If one
is not set the script exits 1, names it, and writes nothing, so `pack:dir` stops
before electron-builder. An empty value is accepted and means none.
`pack:dir` is the only caller.

The bundle's own icon is `assets/icon.png` (`build.mac.icon`; electron-builder
turns the 1024 px PNG into `Contents/Resources/icon.icns`), so Finder and the
Dock show the Open DJ mark before the app runs. `assets/` is also in
`build.files`, because `src/main.ts` loads the same PNG at runtime for the Dock.
`test/pack-config.test.ts` fails if either is dropped; without `mac.icon`
electron-builder ships the stock `electron.icns`.

`build.appId` is `com.opendj.desktop`, the id the Tauri app uses. An unsigned
pack can be given its own bundle id for that pack only:

```sh
OPENDJ_BUILD_LANE_LABEL=electron OPENDJ_BUILD_CHANNEL=dev pnpm pack:dir --config.appId=com.opendj.desktop.electron
```

That changes `CFBundleIdentifier` and nothing else. It does not separate the two
apps' data: the engine's data directory comes from `DEFAULT_IDENTIFIER` in
`src/main.ts`, not from the bundle id, so the pack opens the same library as the
Tauri app.

Close the Tauri app before starting a packed Electron build, and do not run the
two at once. If an engine is already running on that data directory, this shell
adopts it instead of starting its own, and quitting this shell then stops the
adopted engine while the other app is still using it (issue
[#6118](https://github.com/private_owner/music-dj-tools/issues/6118)).

Run it by hand against a payload (the same layout the dmg ships:
`bin/opendj-engine`, optionally `runtime/bin/python3` and `bin/odj-audio`):

```sh
OPENDJ_PAYLOAD_DIR=/path/to/payload OPENDJ_SHELL_DATA_DIR=/tmp/odj-data pnpm start
```

Unpackaged builds only: `OPENDJ_PAYLOAD_DIR` and `OPENDJ_SHELL_DATA_DIR`
override the payload and data dir, and `OPENDJ_REMOTE_DEBUGGING_PORT` opens the
Chromium DevTools port; packaged builds ignore all three. `OPENDJ_ENGINE_ORIGIN`
points the window at an engine you started yourself, in any build, as it does
in the Tauri shell. As root (CI containers) Chromium needs `--no-sandbox`; the
e2e passes it only then.
