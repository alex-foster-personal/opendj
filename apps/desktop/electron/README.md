# Electron desktop shell

The Open DJ desktop shell on Electron, built beside the Tauri shell
(`../src-tauri/`), which keeps shipping until the cutover plan 21-02. Why and
how: `docs/decisions/ADR-NEW-electron-desktop-shell.md` and
`.planning/phases/21-electron-desktop-shell/`.

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
```

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
