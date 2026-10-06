# The auto-update channel

How Open DJ learns that a newer build exists, and how it installs one.

## The shape of it

Two halves, deliberately split, because they have different capabilities and
different audiences.

| | ASKING | APPLYING |
|---|---|---|
| Owner | the engine (Python) | the desktop shell (Tauri updater) |
| Surface | `GET /api/v1/update/check` | `plugin:updater` over IPC |
| Works in a browser | yes | no |
| Works for an agent | yes, over HTTP and a CLI | yes, via shell command poll |
| Verifies the package signature | no, and never | yes, and only here |

The **check** is answered by the engine so that a browser tab and an agent can
ask the same question the desktop button asks. It is plain HTTP, it parses the
release manifest, and it compares versions. It downloads nothing.

The **apply** is `tauri-plugin-updater` and nothing else. The plugin re-fetches
the manifest itself and verifies its minisign signature against the public key
compiled into the binary. That re-fetch is not redundant work: an installer
that accepted a manifest some other component had already parsed would have its
signature check bypassed by whoever called it. Verification and installation
must not be separable, so the engine's check is never wired into the installer.

Both halves read ONE endpoint constant. `tests/engine_core/test_update_channel.py`
asserts that `UPDATE_ENDPOINT` in `apps/engine_core/update_channel.py` is
byte-identical to `plugins.updater.endpoints[0]` in `tauri.conf.json`, so the
button and the CLI cannot drift onto different channels.

## The endpoint

```
https://github.com/alex-foster-personal/issue-assets/releases/latest/download/latest.json
```

GitHub Releases is the standard host for a Tauri updater: it needs no server,
no bucket and no custom domain, and `releases/latest/download/<asset>` is a
stable URL that always resolves to the newest published release.

### Public release host

`music-dj-tools` is private, so its GitHub release assets return 404 to an
unauthenticated updater. The endpoint above uses the public
`alex-foster-personal/issue-assets` release repository instead. It contains
only signed desktop release assets and `latest.json`, never source or secrets.

Before the first Air release it returns 404 because no public release exists.
That is still a named `endpoint-refused` fault, never a false `up-to-date`.
`just release` is the only publication path: it builds, verifies, and publishes
the assets and manifest together to the public repository.

## Versions: what the updater compares, and what it cannot see

The updater keys on **semver**, from `version` in `tauri.conf.json`. Today every
build Open DJ ships is `0.1.0`; that number changes when somebody cuts a
release, not when somebody merges.

The identity that actually distinguishes two builds is the git sha and build
time already stamped into the payload manifest by
`scripts/build_engine_payload.py` and served at `/api/v1/build-info`. The
update channel reuses that -- it does not invent a second version source.

So the check reports both, and names the awkward case rather than hiding it: a
release carrying the same semver as the running build but a different sha is
`up-to-date` by the rule the updater will apply, with
`same_version_different_build` set, and the UI says "same version, other build"
rather than a bare "up to date". A human staring at a reassuring readout while
running a different commit is the exact wasted afternoon `build-info` was
written to end.

**Consequence for releases: bump `version` in `tauri.conf.json` when you cut
one.** Two releases sharing a semver are invisible to the updater.

## Gatekeeper: release artifact requirements

The updater's minisign signature and Apple's code signature are **different
things** and neither substitutes for the other:

- **minisign** proves the update package came from whoever holds the updater
  private key. This is wired and working; the key exists.
- **Apple Developer ID + notarization** is what lets macOS run the result.

`just release` rejects `MDT_SHIP_UNSIGNED`, requires both Apple signing values
and the updater signing key, then verifies `stapler validate` and `spctl` for
both the app and dmg. It notarizes and staples the `.app` before rebuilding the
updater archive and dmg, so offline Gatekeeper verification applies to every
published install path.

No N to N+1 updater round trip has been observed yet. The Air-machine result
below remains the only acceptable evidence for that claim.

## The remote-origin capability, and why it is scoped the way it is

The shipped app's main UI is **not** the Tauri frontend. `apps/desktop/setup`
is a bootstrap page that probes the engine and then navigates the window to
`http://127.0.0.1:<ephemeral>`, which Tauri classifies as a **remote** origin
and denies IPC to by default. Without a grant, the check-for-updates control
would be inert in the shipped app while working fine in a dev browser -- the
worst possible split.

`capabilities/updater.json` therefore grants `updater:default` and
`process:allow-restart` to loopback origins:

```json
"remote": { "urls": ["http://127.0.0.1:*/*", "http://localhost:*/*"] }
```

The port is wildcarded because `engine::free_loopback_port()` picks a new one
every launch, so it cannot be named ahead of time.

**Know what this grant is.** Any process that gets a page loaded into this
window from a loopback origin can invoke the updater. The mitigations are that
the grant is limited to loopback, limited to the `main` window, limited to two
permissions (no filesystem, no shell), and that the updater will still only
install a package whose signature verifies against the compiled-in public key.
Widening this list, or adding permissions to it, deserves more thought than it
took to write.

## Keys

The public key is compiled into the shipped app. Keep its private signing
key in the deployment secret manager, outside the repository. Provision
the documented signing environment through the authorized operator path.

Losing the private key prevents existing installed apps from accepting
updates signed by a different key. Rotation requires a verified migration.

## Cutting a release

1. Bump `version` in `apps/desktop/src-tauri/tauri.conf.json`. This release
   advances it to `0.1.1`.
2. On the Air, load signing values without printing them, then run
   `just release`. It builds the dmg, staples the app before recreating and
   signing the updater archive, generates `latest.json`, and publishes the dmg,
   archive, signature and manifest to `alex-foster-personal/issue-assets`.
3. A retry for the same tag validates its public `latest.json` against the
   configured version, build SHA, tag URL, and updater signature. It then
   returns without rebuilding or overwriting the immutable release.

```json
{
  "version": "0.2.0",
  "notes": "built from <full git sha>",
  "pub_date": "2026-09-01T00:00:00Z",
  "platforms": {
    "darwin-aarch64": {
      "signature": "<contents of the .app.tar.gz.sig file>",
      "url": "https://github.com/alex-foster-personal/issue-assets/releases/download/v0.2.0/Open.DJ.app.tar.gz"
    }
  }
}
```

Put the running build's full sha in `notes`: that is what
`same_version_different_build` reads to tell a user on a same-numbered but
different build that they are not on the release.

## First observed updater round trip

Pending Air-machine evidence. Record the exact initial and final app versions,
the two full git SHAs, the release URL, and measured check, download, install,
and restart timings here. Do not claim this step from a build or manifest-only
check: it requires an installed N app to fetch, install, restart, and report
N+1.

## Agent-native parity

Every UI action has a non-UI path, per the project rule.

| UI action | Agent path |
|---|---|
| the update indicator | `GET /api/v1/update/check` |
| "check for updates" | `python -m apps.engine_core.update_channel check` |
| verify a release semver bump before building | `just release-check-semver` |
| check a *running* app's build | `... update_channel check --engine http://127.0.0.1:<port>` |
| "install and restart" | `POST /api/v1/update/apply` then poll `GET /api/v1/update/apply/{command_id}`; the desktop shell consumes `GET /api/v1/commands/next?consumer=shell`; CLI: `python -m apps.engine_core.update_channel apply --engine http://127.0.0.1:<port>` |

The check CLI exits **non-zero** when the channel could not be read, so a
script cannot mistake an outage for "up to date". The apply CLI uses three
exit codes:

| Exit code | When |
|---|---|
| **0** | Post-apply `GET /api/v1/build-info` on the engine discovered via `.engine.lock` reports an `app_version` strictly higher than the pre-apply snapshot |
| **2** | Pre-claim failures: unreachable `--engine`, check not `update-available`, apply refused, missing `command_id`, or status poll transport/HTTP errors while the command is still `pending` |
| **3** | The shell posts `state: failed` for the apply command (result JSON on stdout); or post-claim relaunch polling ends without a higher version within 120 s |

When the old engine socket dies after the shell claims apply, the CLI does not
treat that as failure; it polls `/api/v1/build-info` on the new engine
discovered from `.engine.lock` for up to 120 s.

**How apply reaches the shell.** The engine cannot replace its own bundle, so
`POST /apply` only enqueues work. Poll
`GET /api/v1/update/apply/{command_id}` for lifecycle state (`pending`,
`claimed`, `succeeded`, `failed`) and, when complete, the shell outcome and
error text. Failed results are logged at WARNING in
`apps.webui.server.shell_commands` so `logs/engine-warn.log` on an installed
app carries the reason. The installed shell webview polls `?consumer=shell` on
the shared command bus (ADR-0050), distinct from the `/performance` AGENT-03
poll, and calls `applyUpdate()` unchanged so minisign verification stays
inside `tauri-plugin-updater`. Bulk artifact install to known machines
remains `scripts/ship_dmg.sh`.

## Files

| Path | What it is |
|---|---|
| `apps/engine_core/update_channel.py` | the check: resolver, route, CLI |
| `apps/webui/frontend/src/lib/rb/update-channel.ts` | fetching, rendering, applying |
| `apps/webui/frontend/src/lib/components/rb/BuildIdentity.svelte` | the surface, beside the build stamp. A repo or vite build renders `dev build, no update channel` (collapsed badge hidden); a payload or Tauri fault still renders `UPDATE CHECK FAILED`. |
| `apps/desktop/src-tauri/capabilities/updater.json` | the remote-origin grant |
| `apps/desktop/src-tauri/tauri.conf.json` | endpoint, pubkey, `createUpdaterArtifacts` |
| `tests/engine_core/test_update_channel.py` | 24 tests incl. the public-host and anti-drift assertions |
| `apps/webui/frontend/tests/unit/update-channel.test.mjs` | 16 tests |

## Archive container (Tue 15 Sep 2026)

The updater plugin gunzips the download and untars it; the top-level entry must be the `.app` directory. `scripts/pack_updater_archive.sh` is the only writer of `*.app.tar.gz` in the dmg recipe and proves the gzip magic before returning, and `scripts/release.sh` refuses to publish an archive that is not gzip. Before this, the recipe rebuilt the archive with `ditto -c -k` (a ZIP), so every published archive from v0.1.2 to v0.1.4 was refused by the shell with `invalid gzip header`; the refusal became visible through `GET /api/v1/update/apply/{command_id}` (#2951).

## Runtime evidence

Retain installed N-to-N+1 version, signature, restart and build-identity
evidence privately. A manifest or build-only check cannot establish this
runtime outcome. The archive must use the required gzip container.

