# apps/desktop -- Open DJ desktop shell

Tauri 2 native window wrapping the local engine's web UI. Compiles via
`cargo check` and launches via `cargo tauri build --debug --no-bundle`;
see `.planning/evidence/shell-smoke-2026-08-19.png` for a launch smoke test.

## Thin-shell rule (non-negotiable)

This app is a **window, not an application**. All product logic lives in the
engine (FastAPI daemon + SvelteKit webui) served over HTTP at
`http://127.0.0.1:8683`. The Tauri window just points at that URL and gets
out of the way.

Concretely:

- **No `@tauri-apps/api` in the webapp.** The SvelteKit frontend served by the
  engine must not import from `@tauri-apps/api/*`. It should render and
  behave identically whether it's opened in a Tauri window or a plain browser
  tab. If a feature needs desktop-only behavior, it does not belong in the
  webapp -- it belongs behind a capability check the engine already exposes
  over HTTP, or it is out of scope for this shell.
- **No Tauri IPC commands for app logic.** `src-tauri/src/main.rs` does not
  define `#[tauri::command]` handlers that implement business logic. If the
  shell ever needs a native-only capability (e.g. a system file picker), that
  capability must stay a thin pass-through -- it forwards to the engine or
  returns raw OS data, never makes product decisions.
- **Rust owns the window, nothing else.** No state, no business rules, no
  data access in `src-tauri/`. If you find yourself reaching for `rusqlite`,
  `reqwest`, or anything that talks to the engine's data layer from Rust,
  that logic belongs in the engine instead.

Rationale: the engine is the single source of truth and is already
browser-addressable. Duplicating logic into the shell would fork behavior
between "desktop app" and "open the URL in a browser" -- exactly the
maintenance trap this thin-shell rule exists to avoid.

## Current state

- The window no longer points straight at the engine. It loads the bundled
  bootstrap page in `setup/`, which probes the engine and either navigates
  to it or renders the setup screen (see "The engine gap" below).
- `bundle.active` is `true` with an `app` target ONLY. Tauri's own dmg
  bundler lays the image's window out by driving Finder over AppleScript,
  which times out (AppleEvent -1712) on any machine nobody is interactively
  logged into, so `just dmg` builds the app and then creates the image
  itself with `hdiutil create -format UDZO` -- a plain file operation that
  needs no Finder and no logged-in user (#1711). It proves the artifact by
  mounting it, and is the only supported way to produce one.
- Icons under `src-tauri/icons/` are generated from the existing open-dj
  brand mark (`apps/webui/frontend/static/icon-512.png`), converted to RGBA
  (Tauri's `generate_context!` requires RGBA source icons; the brand PNG is
  opaque RGB).

## Development loops: attach without a release artifact

The UI, engine, and shell are independent feedback loops. Claim this
worktree's port pair once with `just webui-ports-claim`, then use
`just webui-ports` whenever a command below needs the backend or frontend URL.
Run each long-lived command in its own terminal.

### Fast UI loop

Run `just webui-backend`, then start Vite from source:

```sh
cd apps/webui/frontend
pnpm exec vite dev --host 127.0.0.1
```

Open the claimed frontend URL in Chrome. Vite proxies `/api` to the claimed
backend and applies HMR on save. Chrome is the fastest feedback loop and the
control arm, not the final browser-engine verdict.

### WebKit-truthful UI loop

Run the coupled production-bundle watcher and engine:

```sh
just webui-webkit-watch
```

The recipe runs `pnpm build --watch`, waits for its initial production build,
then starts the reloadable engine with `MDT_FRONTEND_BUILD_DIR` pointed at that
bundle. Keep this terminal running. On every save, Vite rebuilds the same
production output that the engine is already serving. If the watcher exits or
does not finish its initial build within 12 seconds, the loop fails loudly.

Open the claimed backend URL in Safari. The engine serves the rebuilt bundle,
so Safari exercises the system WebKit and the same production transforms used
by the packaged interface.

### Engine loop

Run `just webui-backend`. It starts the FastAPI engine with `--reload` through
this worktree's `.venv/bin/python`, so Python changes restart the daemon while
the browser or attached shell stays on the same claimed backend origin. This
is deliberately not `uv run`: macOS attributes a Media Library permission to
the executable that touches the protected track, so the engine must be the
venv Python rather than uv. A reload that cannot boot fails in that terminal
instead of falling through to another engine.

### Shell loop

Build the SPA before starting `just webui-backend`, following the WebKit loop's
startup order above. With that engine healthy, run:

```sh
just dev-attach
```

The recipe reads this worktree's claimed backend origin, verifies that engine
is healthy and serving the SPA, builds the debug Cargo target, and launches it
with `OPENDJ_ENGINE_ORIGIN` set. It also exposes the debug-only WebDriver seam
on port `4456`; pass another explicit port as `just dev-attach 4457` when needed.
Drive that real WKWebView through the webview MCP described in
`apps/desktop/mcp/README.md`. The release build deliberately has no WebDriver
surface.

The DMG is a release artifact only. Do not use it as an inner development loop
and do not replace files inside an installed `.app`: doing so breaks its code
signature and build-stamp contract.

## The engine gap

A packaged app on a tester's Mac has no engine: no Python, no checkout, no
daemon on `:8685`. The SPA cannot cover this, because the SPA is served BY
the engine -- when the engine is down there is nothing to load. So the
bootstrap page lives in the shell bundle instead, where it is always
available.

`setup/index.html` + `setup/setup.js` do exactly two things:

- engine reachable -> navigate the window to the engine origin and get out
  of the way. Verified against a real packaged build: the loopback server's
  access log shows `GET /api/v1/health` followed by `GET /`, which proves
  WKWebView permits navigating from the `tauri://localhost` app origin to a
  loopback HTTP origin.
- engine unreachable -> render a friendly setup screen with retry guidance
  and the attempt count. Exact address, failure text, start command and the
  `no-cors` probe note live in a closed **Details for agents** disclosure,
  not in the default copy. See
  `.planning/evidence/dmg-setup-screen-2026-08-19.png` for the older
  always-exposed layout this replaced.

There is no third state: no blank window, no endless spinner, no fake data.

The probe is a `no-cors` fetch. The engine's CORS allowlist covers the
dev-server origins only, so a normal cross-origin read from
`tauri://localhost` would be blocked before it could tell "refused" from
"absent". `no-cors` yields one honest bit: the connection was accepted, or
it was not. That technical note is in the closed agent disclosure, not the
default operator copy.

**Runtime supervision (INSTALL-23, issue #2916):** after boot the shell polls
its engine child every 5 s. A dead or zombie engine is reaped, shell-side
`GET /api/v1/health` on the loopback port in `.engine.shell.json` reports
`engine: dead`, the title bar shows `engine dead`, and the webview returns to
the bootstrap fatal view instead of spinning on a dead port. The same loopback
server also exposes `GET /api/v1/audio/output-health` (macOS CoreAudio delivery
probe) and `POST /api/v1/audio/switch-output` (cycle default output away and
back). The engine proxies both at `/api/v1/audio/*` for agents and the
performance UI. Off macOS they return `verdict: unknown` honestly. Within 30 s the
shell either restarts on a fresh port (logging
`engine restarted after exit code N` with a UTC timestamp) or shows fatal with
Relaunch. When restart fails because the data-dir mount has under 1 GiB free,
the shell enters `waiting for disk space` (shell health `engine: waiting-disk`,
`reason: low-disk`) and auto-restarts once space returns instead of showing the
fatal dialog. Skipped when `OPENDJ_ENGINE_ORIGIN` is set.

**The shell spawns its own engine.** `start_engine` picks a free loopback
port, spawns the bundled payload and waits for health before the window
opens (`src-tauri/src/main.rs:179-183`). It attaches to an engine somebody
else started ONLY when `OPENDJ_ENGINE_ORIGIN` is set, which is the operator
dev-attach path, not the shipped one: an origin that is set is honoured
exactly and no second engine is started behind the operator's back
(`main.rs:237-248`). A set-but-empty value panics rather than falling back.

For UI and transport iteration without the shell at all, see
`docs/architecture/chrome-dev-loop.md`: vite from source in Chrome against a dev engine and a
copied library, with the shipped-app-versus-loop topology diagram.

Still not built: a single-instance lock, and "sidecar dies with the app".
The shutdown path should use the zombie-aware group check in
`apps/engine_core/jobs/reap.py` rather than a raw `killpg(pid, 0)`
existence loop.

### How the engine is bundled

**The .app carries its own engine.** `scripts/build_engine_payload.py`
stages a relocatable CPython, the locked dependency closure, the engine
source and the built SPA into `apps/desktop/src-tauri/payload`, which
`tauri.conf.json` copies to `Contents/Resources/payload`; the shell starts
that engine on an OS-assigned port at launch (`justfile:655-661`). The .app
no longer expects a repo checkout, and there is no address to bake, which is
why `MDT_DESKTOP_ENGINE_ORIGIN` is gone.

The route not taken was pip-installing the wheel into the bundle and driving
it from a Tauri sidecar. The wheel ships `apps/*` only
(`[tool.setuptools.packages.find]`, `pyproject.toml`) and excludes `data*`,
`scripts*`, `open-dj*`, yet at least eight production call sites derive a
repo root via `parents[N]` and reach into exactly those excluded
directories: `apps/open_dj/schema_loader.py` wants `open-dj/schema/`,
`apps/stems/cli.py` and `apps/vocals/` want `scripts/*_worker.py`,
`apps/webui/server/routes/progress.py` wants `data/progress-tree.yaml` and
shells out to git at the root. Only `platform_paths.py` has an env escape
hatch. An installed-wheel engine would boot and then break silently on the
first path touch. Staging a repo-shaped tree is what avoids that.

### Engine origin

| Source                         | When                         | Wins over         |
| ------------------------------ | ---------------------------- | ----------------- |
| `?engine=` query param         | tests                        | everything        |
| `OPENDJ_ENGINE_ORIGIN` env var | runtime, road-tests          | the baked default |
| `OPENDJ_DEFAULT_ENGINE_ORIGIN` | compile time, via `just dmg` | the constant      |
| `http://127.0.0.1:8685`        | shipped default              | --                |

Non-loopback or malformed values are refused, never silently replaced.

## Real-shell e2e (tier 2)

`just real-shell-e2e` drives THIS window's actual WKWebView:
`apps/desktop/wdio.conf.ts` plus `tests/real-shell-smoke.e2e.ts`. It boots its
own engine on `:8691` over a generated fixture library, so it never touches a
real data dir (the engine lock is singleton, so sharing one is not an option
anyway).

It is deliberately small, because the sibling tier already covers the fault
class. `just webkit-deckload-e2e` runs Playwright's webkit -- the same
WKWebView core -- against the production build served by the engine, which is
what caught the worklet defect that shipped broken. This tier covers only what
that structurally cannot see: the Rust `initialization_script` injecting
`OPENDJ_ENGINE_ORIGIN`, the bootstrap page, and the navigation off
`tauri://localhost`.

**Driver.** macOS has no WKWebView WebDriver, so `tauri-driver` is Windows and
Linux only. `@wdio/tauri-service` with `driverProvider: 'embedded'` compiles a
W3C WebDriver server into the binary and drives the webview through a
`WKScriptMessageHandler`. `tauri-plugin-playwright` was evaluated and rejected:
it returns every command result through `__TAURI_INTERNALS__.invoke`, and Tauri
v2 classifies this shell's engine origin as `Origin::Remote` and denies it IPC,
so it would time out on every command the moment `setup.js` navigates. Using it
would mean granting the http origin permission to invoke Tauri commands, which
is the thin-shell rule above inverted.

**What the driver can drive.** A click reaches the app (the row it clicks
becomes the selection, asserted in the smoke). A synthesized double-click does
not reach `ondblclick`, and `moveTo()` does not produce a CSS `:hover` state,
so the hover-revealed per-row load buttons never display. Both work in
Playwright's webkit against the same build, so they are driver limits rather
than product defects. Gesture coverage therefore stays in tier 1, and the deck
load here goes through the agent-native IPC. Do not "fix" that back into a
double-click without re-measuring.

**The fixture engine runs with a sandboxed HOME.** `platform_paths.py` derives
both `~/Library/Pioneer/rekordbox` and `~/Music` from HOME. Without the
sandbox, a `setup.import-rekordbox` job reached the live rekordbox database,
decrypted a copy into the fixture dir and wrote 32 real tracks into a two-track
generated library (Wed 19 Aug 2026, read-only toward rekordbox). Both e2e tiers
now point HOME at an empty dir inside their own fixture, so a real library is
unreachable by construction.

**It cannot reach a dmg.** `tauri-plugin-wdio-webdriver` is declared under
`[target.'cfg(debug_assertions)'.dependencies]`, so a release build cannot
compile it in. Verified on the artifacts rather than assumed: a release binary
contains 0 occurrences of `wdioEvalResult` (debug: 3), 0 of
`tauri-plugin-wdio-webdriver` (debug: 27), 0 of `TAURI_WEBDRIVER_PORT`
(debug: 1) and 0 of `/session` (debug: 4). The one surviving `wdio` string is
the plugin name inside Tauri's ACL blob, with no backing code.

## Build

```sh
just dmg                                # release + dmg + mount verification
cd apps/desktop/src-tauri && cargo check
```

`just dmg` reads two optional `.env` values, both unset in a plain
checkout:

- `MDT_LANE_LABEL` -- suffixes the bundle identifier and productName so two
  bake-off lanes coexist on one Mac. `B` yields productName `Open DJ (B)`,
  identifier `com.opendj.desktop.lane-b`, artifact
  `OpenDJ-B-0.1.0-aarch64.dmg`. The identifier is the real clash key:
  macOS derives Application Support, Caches and WebKit storage from it.
  Derived by `scripts/desktop_lane_config.py`, which refuses a label it
  cannot turn into a safe identifier rather than sanitising it.
- `MDT_DESKTOP_ENGINE_ORIGIN` -- bakes the engine address the build looks
  for, so two lanes do not both default to `:8685` and answer for each
  other.

The window title is `productName`, so the overlay labels the window with no
second place to edit.

### Signing and notarization

This is the **Developer ID** path: a dmg a tester downloads and opens by
double-click, cleared by Gatekeeper because it is notarized and stapled. It
is not the Mac App Store path. `scripts/ship_appstore.sh` uses a Mac App
Distribution certificate, a provisioning profile and sandbox entitlements,
off the **same Team ID**. Same account, different certificates, and they are
not interchangeable.

Two values, never committed:

- `MDT_MACOS_SIGNING_IDENTITY` -> exported as `APPLE_SIGNING_IDENTITY`,
  which tauri-cli reads as the override for `bundle.macOS.signingIdentity`
  (verified in tauri-cli 2.11.4 `interface/rust.rs`).
- `MDT_MACOS_NOTARY_KEYCHAIN_PROFILE` -> an `xcrun notarytool
store-credentials` profile name. Tauri's bundler **cannot** consume one:
  it accepts only `APPLE_API_*` or `APPLE_ID`/`APPLE_PASSWORD`/
  `APPLE_TEAM_ID` (verified in tauri-bundler 2.9.4), so `just dmg` runs
  `notarytool submit --wait` then `stapler staple` itself.

**They are all-or-nothing, and refusal is the default.** Either both are set,
or the build stops. Setting one without the other is refused before the build
starts: a notary profile with no identity because notarizing an unsigned app
is impossible, and an identity with no notary profile because a Developer ID
signature that is never notarized is still refused by Gatekeeper on download,
so it buys nothing. Neither set at all is also refused, and the error prints
the identities that do exist on the machine.

`MDT_SHIP_UNSIGNED=1` is the single deliberate way to build unsigned. It
prints a loud warning block, and it is refused if a signing identity is also
set, because that combination is contradictory intent rather than something
to guess at.

#### Where the signing happens, and why it is in three places

The app carries a relocatable CPython under `Contents/Resources/payload` --
91 Mach-O files including the interpreter. Tauri's bundler signs the `.app`
but does not walk into a resource directory, so those files have to be signed
before the bundle is sealed around them. `scripts/sign_macos_developer_id.sh`
holds each stage and `just dmg` calls it at three points:

1. `payload` -- after `build_engine_payload` stages the directory and before
   `cargo tauri build` seals it. Every Mach-O gets `--options runtime`
   (hardened runtime) and `--timestamp`. Skipping this is what makes the
   notary service return `Invalid`.
2. `verify-dmg-app` -- mounts the built image and asserts the `.app` really
   carries a `Developer ID Application` authority, the hardened runtime flag
   and a secure timestamp. These are checked here because the notary service
   reports them slowly and confusingly, and because the image is the copy a
   tester actually receives.
   The staged `.app` in `bundle/macos` survives the build. Tauri no longer
   creates an image, so it no longer deletes the app it bundled: `just dmg`
   picks that app up and runs `hdiutil create` on it itself, on the signed
   and the unsigned path alike (#1711).
3. `dmg` then `notarize` -- signs the image itself (Gatekeeper assesses the
   dmg a tester double-clicks, not only the app inside it), submits, staples,
   and runs `spctl -a` to confirm the ticket takes.

`notarytool submit --wait` **exits 0 on an `Invalid` status**, so the helper
parses the status line and requires `Accepted`. Trusting the exit code alone
ships un-notarized images that report success.

#### Unsigned builds

With `MDT_SHIP_UNSIGNED=1`: `codesign` reports `adhoc, linker-signed` with
`Sealed Resources=none`, and `spctl -a` rejects the bundle on the machine
that built it. A tester who downloads the dmg also gets
`com.apple.quarantine`, so Finder refuses to open the app at all.
`ship_dmg.sh` clears the attribute on install for this case only -- it probes
the artifact with `stapler validate` rather than reading the build
environment, because the artifact-reuse path can install a dmg built in a
different shell under different variables. A tester installing by hand must
run:

```sh
xattr -dr com.apple.quarantine "/Applications/Open DJ.app"
```

### arm64 only

v1 is Apple Silicon only, by decision. An Intel Mac cannot run this
artifact at all. `just dmg` prints the architecture it produced.

productName: `Open DJ`
identifier: `com.opendj.desktop`
