# apps/webui -- music-dj-tools web UI daemon (Phase 11)

Local-first FastAPI backend + SvelteKit SPA. Binds to `127.0.0.1` by
default (D5); override only behind Tailscale or Cloudflare Tunnel.

## Bootstrap

```bash
uv sync
just webui-ports
just run
```

`just run` claims this worktree's port pair, starts backend and frontend in
their own sessions, then opens `http://127.0.0.1:<frontend>`:

- macOS: one Terminal.app window per server (`just webui-backend`,
  `just webui-frontend`), then `open` the loopback URL.
- Linux: detached tmux session `mdt-webui-<frontend>` (attach with
  `tmux attach -t mdt-webui-<frontend>`). No browser opener; the URL is
  printed. If the frontend is already answering, spawn is skipped.

The ignored root `.env` is the worktree-local port surface
(`MUSIC_DJ_BACKEND_PORT`, `MUSIC_DJ_FRONTEND_PORT`). The first launch
reserves the pair in Git's shared metadata. Manual split is still
`just webui-backend` and `just webui-frontend` in two terminals; endpoints
are printed by `just webui-ports`.

Logs append per local day. On agentbox that is
`~/.local/share/music-dj-tools/webui/webui-{backend,frontend}-YYYY-MM-DD.log`.
Elsewhere it is the OS temp dir. `.tmp/webui-*.log` is a Cursor symlink
to today's file.

Browser failures are also captured: uncaught errors, rejected promises,
SvelteKit errors, and every error toast POST a bounded report to the daemon.
The backend log gets one short summary and event ID; the complete browser
stack and environment are appended to
`~/.local/share/music-dj-tools/webui/webui-client-errors-YYYY-MM-DD.log`
(`.tmp/webui-client-errors.log`). The daily file is capped at 10 MiB and the
browser retains at most 20 unsent reports for retry after a network failure.

Every browser page load also posts one privacy-bounded `page-view` record to
`~/.local/share/music-dj-tools/webui/webui-visitors-YYYY-MM-DD.log`
(`.tmp/webui-visitors.log`). It contains timestamp, path without query/hash,
client IP, user agent, viewport, secure-context state, and Tailscale identity
headers when Serve supplies them. It sets no tracking cookie and the file is
private (`0600`) and capped at 10 MiB/day. HTTP-to-HTTPS visits are recorded in
the separate `agentbox-http-redirect-YYYY-MM-DD.log`, also without query text.
This is operational visitor logging, not a product analytics system: Sentry is
errors-only, and no PostHog/session-replay/tracing pipeline is installed.

## Agentbox (tailnet)

```bash
just run-agentbox
just agentbox-check
```

Works from agentbox or either Mac. On the box it restarts this worktree's
servers, points tailnet-only Tailscale HTTPS Serve `:443` at Vite on loopback,
points tailnet-only HTTP `:80` at a fixed-host redirector, and probes the
MagicDNS `Host`. This makes `http://agentbox/<path>` redirect to the secure
MagicDNS origin (`agentbox.<tailnet>.ts.net`); port 80 never proxies Vite
directly because that would disable AudioWorklet. On a Mac it BatchMode-SSHs
to the tailnet alias `agentbox` and runs `--local` there (no Mac bind or Mac
`tailscale serve`).
macOS then `open`s the origin the launcher reported it served; on the box
the URL is printed. Claude Code's hosted browser is outside the tailnet and
cannot load this private URL. The launcher instead runs its own real Chromium
check on agentbox and fails unless both HTTP aliases redirect and the secure
page initializes JavaScript, AudioWorklet, performance IPC, API health, and
visitor logging.

Requires `MUSIC_DJ_ALLOWED_HOSTS` in the root `.env` (bare hostnames only,
no scheme, port, or wildcard). A deployment that reaches the box by MagicDNS
lists its own name there; the tailnet label is deployment config, so this repo
writes the placeholder `agentbox.<tailnet>.ts.net` and never the real one.
Servers still bind `127.0.0.1`; ufw is unchanged. SSH destinations are the alias
`agentbox` plus whatever `MDT_AGENTBOX_SSH_HOSTS` names -- no public IP.

Agentbox-only deployment code belongs in `apps/agentbox`; see its README for
the current ownership map and the two older web UI modules still awaiting a
mechanical move.

### Agentbox library crate

Agentbox uses an explicit local replica rather than falling through to a
stale `/Users/...` tree. Its root `.env` sets:

```text
MDT_LIBRARY_MODE=remote
MDT_CRATE_ROOT=/data/mdt-crate
```

The Air remains the owner, but either side may initiate the same
reconciliation. `--local` means the command runs beside the owner and pushes;
`--remote` means it runs beside the replica and pulls over SSH. With neither
flag, explicit `MDT_LIBRARY_MODE` selects the side. Therefore the same command
works from both machines:

```bash
# Air: push
just crate-sync --local --playlist Base

# agentbox: pull
just crate-sync --remote --playlist Base
```

`just crate-push` and `just crate-pull` are explicit aliases. Select a caller-owned
playlist with `--playlist`, or supply its stable IDs with `--stable-ids`.

The personal `/performance/preload1` demonstration and captured beat-grid
regression inputs are withheld from this public copy. `--preload1` and the
preset-dependent `crate-verify` / `--verify-spike` workflow are unavailable;
they are not replaced with invented IDs. The full-crate spike prerequisite
remains enforced, so these instructions do not authorize a full-crate push.
Generic scoped planning and transfer retain their existing configuration and
safety checks.

The performance smoke selector retains three tagged tests in two files:
BAR/sync and waveform/IPC controls, plus CloudSync quick actions. The private
preload stop-and-settle spec is unavailable. The optional
`pnpm test:e2e:performance:autoplay-proof` selector has no retained spec and
must fail with no tests found; it is not a passing public acceptance gate.
Use the retained full suite and real runtime evidence for their actual scope.

The sync batches NUL-delimited rsync file lists per source root, so spaces in
track names are preserved without one SSH connection per file. Rsync's
size+mtime quick check means an already reconciled file is not transferred
again. The owner builds `manifest.json` from its live databases and files,
including a `stable_id -> crate file` index and a canonical, bounded track and
playlist ledger. The replica independently stats every destination and reads
the corresponding records from its own SQLite database; both file and library
digests must agree. No `state.db` is copied. Explicit remote mode applies the
owner records transactionally through `StateWriter`, taking a SQLite backup
before a changed reconciliation. A disagreement fails the command; inspect it
directly with `just crate-audit`.

The stable-ID index closes the gap between Rekordbox's current `FolderPath`
and an older `tracks.file_path`. The structured library ledger also closes the
case where a playlist on agentbox still names a superseded path-derived stable
ID: its membership is reconciled to the Air's current ID rather than guessed
from a title or filename. Local mode never applies replica state. The sync
writes the box's `data/path-map.json` and manifest only after the media transfer
and proves the resulting state digest.
Streaming and absent tracks remain unavailable. The Air-to-agentbox audio sync
does not copy stems; remote stems are a separate derived cache at:

```text
/data/mdt-crate/derived/stems/demucs4/<stable_id>/
/data/mdt-crate/derived/stems/roformer2/<stable_id>/
```

`apps/webui/library_assets.py` is the single mode boundary. A local app reads
and writes `data/state/stems*`; remote mode reads and writes only the crate
paths above. The existing `POST /api/v1/stems/generate` now resolves crate
audio through the production path map and writes one per-track bundle to the
remote Demucs root. Verify a stored bundle through the real browser graph:

```bash
just agentbox-stems-check <stable_id>
```

A 404 `STEM_BUNDLE_NOT_FOUND` still means exactly that: the track has audio but
no derived bundle has been generated or copied onto this server. The manifest
GET is now HTTP 200 unavailable with that code; a part GET is still 404 when no
bundle exists.

## Endpoints (v1)

All under `/api/v1`:

| Method | Path | Purpose |
|---|---|---|
| GET    | `/tracks`                 | list tracks (filter, paginate) |
| GET    | `/tracks/{stable_id}`     | track detail with provenance envelope |
| PATCH  | `/tracks/{stable_id}`     | edit rating / tags / notes; If-Match + 409 |
| GET    | `/playlists`              | list playlists |
| GET    | `/playlists/{id}`         | playlist detail + diff (fixture) |
| GET    | `/pairings`               | list pairings |
| POST   | `/pairings`               | create pairing (idempotent) |
| DELETE | `/pairings/{id}`          | delete pairing; If-Match required |
| GET    | `/queues/{kind}`          | read-only M3 triage queue |
| GET    | `/health`                 | daemon + cloud + syncthing state |
| GET    | `/settings`               | read-only effective runtime config (bind host, backend, storage paths); anything not introspectable is marked TBD |
| POST   | `/auth/login`             | start Google sign-in; returns the consent URL + CSRF state |
| GET    | `/auth/callback`          | Google's loopback redirect target; plants the session cookie |
| GET    | `/auth/me`                | signed-in user or signed-out envelope (HTTP 200) |
| POST   | `/auth/logout`            | drop the session, clear the cookie |
| GET    | `/assistant/status`       | engine-only. Is an OpenRouter key configured, and which model would answer: `{configured, model}` |
| POST   | `/assistant/chat`         | engine-only. `{messages:[{role,content}]}` -> the completion streamed as plain UTF-8 text |

### Assistant (engine-only)

The sidebar chat that keeps the user company while the first-run import
runs. A stateless proxy in front of OpenRouter -- no conversation is stored
engine-side, so the whole thread is resent each turn.

| Env var | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | none | The credential. Absent = `/chat` refuses. |
| `MDT_ASSISTANT_MODEL` | `google/gemini-3.7-flash` | Any OpenRouter slug. |
| `MDT_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Aim the proxy elsewhere (the tests point it at a local stub). |

Refusals carry `{"detail": {code, message}}` like every other router here:
`assistant_key_missing` (409, names the env var) when no key is set, and
`assistant_upstream_error` (502, plus `upstream_status`) when OpenRouter
refuses or cannot be reached. There is no fallback reply, no second
provider and no retry -- an assistant that cannot reach a model says so.

```bash
curl -N localhost:8682/api/v1/assistant/chat \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"what is importing?"}]}'
```

The system prompt lives server-side, so an agent driving `/chat` with curl
gets the same assistant the sidebar does. A caller may supply its own
leading `system` message instead.

## Google sign-in (identity, not access control)

The user bauble in the top right signs in with Google. Read the next
paragraph before assuming this protects anything.

**Sign-in establishes who you are; it does not gate a single endpoint.**
Every route above is still reachable without a session. This is identity
for attribution and personalisation, not authorisation, so the exposure
advice below is unchanged by its existence.

Flow: Authorization Code + PKCE against a Google **Desktop app**
(installed) OAuth client. Desktop clients accept any
`http://127.0.0.1:<port>/...` loopback redirect, which is what lets each
worktree sign in on its own port. A "Web application" client would need
every worktree's port registered by hand.

Scopes are `openid email profile` and nothing else.

Credentials are read from the environment, first match wins:

  * `OPENDJ_GOOGLE_OAUTH_CLIENT_ID` / `OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET`
  * `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`

Both live in Doppler (project `general`, config `dev_personal`), so start
the daemon through Doppler:

```bash
doppler run --project general --config dev_personal -- \
  uv run --no-sync python -m apps.webui.server --host 127.0.0.1
```

Without them `POST /auth/login` returns 503 carrying the full provisioning
runbook. It never falls back to a degraded sign-in.

Session model: the browser holds an opaque token in an httpOnly cookie;
the daemon stores only its sha256, alongside the Google refresh token, in
`users` / `auth_sessions` in `data/state/state.db` (schema v6). So a
leaked database cannot be replayed as a cookie, the browser never holds a
Google token, and sign-in survives both a daemon restart and a browser
restart. Sessions last 30 days.

The cookie is `SameSite=Lax` and not `Secure`: the daemon is http on
loopback by design, and a `Secure` cookie would simply never be stored.
`Lax` (not `Strict`) is required because the cookie is set during the
top-level redirect back from `accounts.google.com`.

The session cookie name is `opendj_session`, with `Path=/`, `SameSite=Lax`,
`HttpOnly`, not `Secure`, and no `Domain` attribute (host-only for the
origin you signed in on, e.g. `127.0.0.1`).

The cookie is stored in **this browser profile's** cookie jar. A second Chrome
profile, Safari, or the packaged app will show signed out until you sign in
there too. That is how browser cookies work, not a daemon bug.

Chrome and Safari development loops proxy to the same backend daemon and the
same `GET /api/v1/auth/me` handler. If a client presents the session cookie,
it receives that identity; if it omits the cookie, `/auth/me` returns the
signed-out envelope even when `auth_sessions` still has a row for another
browser's sign-in.

To sign in on the other browser, click **Sign in with Google** again with the
same Google account. That creates a second session row (same user, separate
30-day cookie in that profile's jar).

Always open the app at `http://127.0.0.1:<port>`, never `localhost`: the
host-only cookie does not cross those hostnames.

## Network exposure posture (CAT-05b)

The daemon has **no authorisation layer**. Expose via Tailscale
(recommended) or Cloudflare Tunnel + Cloudflare Access. Setting
`MUSIC_DJ_BIND_HOST=0.0.0.0` adds an `X-Bind-Warning` header to every
response so the UI shows a red banner.

### Host allowlist and origin guard (SEC-01)

Every request must present a `Host` (or `X-Forwarded-Host`) in the
configured allowlist: loopback names, `MUSIC_DJ_SHARE_HOST`, and any bare
hostnames in `MUSIC_DJ_ALLOWED_HOSTS`. Foreign hosts receive
`403`/`HOST_NOT_ALLOWED`.

Mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) with an `Origin`
header must match the same trusted browser origins used by CORS (frontend
port, backend port for the desktop shell, share origin, e2e literals).
Foreign origins receive `403`/`ORIGIN_NOT_ALLOWED`. Requests without an
`Origin` header (curl, agents, `just run`) are unchanged.

Binding on a non-loopback address without a non-empty
`MUSIC_DJ_ALLOWED_HOSTS` refuses startup. This is separate from
`MDT_SYNC_TRUST_TAILNET` / sync bind guard.
## Sharing the daemon with other people

Signing in identifies a person; it does not authorise them, and there is no
per-user permission model. Everything below is the actual access control.

For one or a few private testers, share
only the agentbox machine with Tailscale node sharing and restrict
`autogroup:shared` to ports 80 and 443. This does not add them as members of
your tailnet or expose other machines. See `apps/agentbox/README.md`.

For a public hostname without the Tailscale client, put Cloudflare Tunnel +
Access in front of loopback Vite, then set:

- `MUSIC_DJ_SHARE_HOST` -- the public hostname Access serves
- `MUSIC_DJ_SHARE_AUTH=cloudflare-access` -- require the identity headers from
  a Cloudflare Access-authenticated request
- `MUSIC_DJ_SHARE_READ_ONLY=1` -- share host can GET, not PATCH/POST/DELETE
- `MDT_PATH_MAP` -- JSON prefix rewrite so Mac `FolderPath`s resolve on
  this disk after the crate is copied here
- `MDT_AUDIO_SHARE_MAX_VENUE=warehouse` -- friends get the lossy ceiling
  when a lower-or-equal copy exists; you on loopback still get Stadium

Loopback and Tailscale Hosts stay full-access. `MUSIC_DJ_BIND_HOST=0.0.0.0`
adds an `X-Bind-Warning` header so the UI shows a red banner.

Copy `apps/webui/share/path-map.sample.json` to `data/path-map.json` and
`apps/webui/share/cloudflared.sample.yml` next to a named tunnel. Origin
must stay `http://127.0.0.1:<frontend>`. The template requires cloudflared to
validate the Access JWT signature and application audience before forwarding;
the app then requires both forwarded identity headers and defaults the share
host to read-only. The old token session remains available only with explicit
`MUSIC_DJ_SHARE_AUTH=token` plus `MUSIC_DJ_SHARE_TOKEN`.

This is an opt-in proposal, not a live endpoint. The agentbox setup and review
checklist are in `apps/agentbox/CLOUDFLARE_ACCESS.md`. Tailscale Serve remains
configured independently and is not replaced by the tunnel.

### CORS policy (non-localhost exposure)

The default CORS configuration allows both loopback origins for the configured
`MUSIC_DJ_FRONTEND_PORT`, with wildcard `allow_methods` and `allow_headers`.
This is safe for the default
`127.0.0.1` bind. If you override `MUSIC_DJ_BIND_HOST` to expose the
daemon over LAN / Tailscale, tighten the CORS policy in
`apps/webui/server/app.py` to an explicit allow-list before exposure:

  * `allow_methods=["GET", "POST", "PATCH", "DELETE"]`
  * `allow_headers=["Content-Type", "If-Match"]`
  * `expose_headers=["ETag", "X-Bind-Warning"]` (already set)

The `If-Match` header must remain in `allow_headers` for optimistic
concurrency preflights to succeed under strict browser CORS.

## Optimistic concurrency (D6)

Every write requires `If-Match` with the current ETag
(`sha1(stable_id + ":" + modified_at)`). Stale etag -> 409 with current
body + fresh ETag. Missing etag -> 428.

## State backend

Routes talk to a narrow `StateBackend` protocol. v1 uses
`InMemoryBackend`. Phase 5 will provide a sqlite-backed adapter in
`apps.shared.state`; see `TODO(phase-5)` in
`apps/webui/server/backend.py`.

## OpenAPI

The schema is committed at `apps/webui/openapi.json` so Phase 17
(launcher) can codegen a typed Rust client. Regenerate with
`make webui.openapi` after any route change.

## Reused by Phase 17 (launcher)

The launcher embeds this same daemon; the FastAPI surface is the
contract. Do not break backwards compatibility without bumping
`apps.webui.__version__` and running `make webui.openapi`.
