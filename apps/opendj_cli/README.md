# opendj CLI and shipped MCP (AGENT-11)

The `opendj` console script drives a running Open DJ over the AGENT-03 command
bus. On an installed app, the same entrypoint also hosts the stdio MCP server
agents register against.

## Installed app registration

```bash
claude mcp add opendj -- "/Applications/Open DJ.app/Contents/Resources/payload/bin/opendj" mcp
```

Codex equivalent:

```bash
codex mcp add opendj -- "/Applications/Open DJ.app/Contents/Resources/payload/bin/opendj" mcp
```

No repo checkout is required when Open DJ is running and the bundled binary is
used.

## Installed shell navigation (AGENT-12)

On a cold launch the desktop shell lands on the library route. Mirror-dependent
verbs auto-request `/performance` via `POST /api/v1/shell/navigate`; only the
installed shell webview consumes that poll. Explicit navigation:

```bash
opendj open performance
opendj --json open /performance
```

MCP equivalent: `open_route` with `route` defaulting to `/performance`.
`ui_url` opens a browser tab only and does not move the shell window.

## CLI meta commands

| Command | Purpose |
|---------|---------|
| `opendj status [--json]` | Lock-file origin, health, build-info (MCP `status` parity) |
| `opendj api METHOD PATH` | Raw HTTP to `/api/v1/*` only; refuses traversal and non-JSON 2xx bodies; exit codes: see `apps/opendj_cli/__init__.py` |
| `opendj dj <tool>` | AutoDJ tools (AGENT-29), same JSON as the MCP tools below: `state`, `profile`, `windows`, `score`, `suggest`, `styles`, `propose`, `validate`, `schedule`, `amend`; `try` renders and ranks candidate transitions (TRYER-11): `opendj dj try --out X --in Y -n 6 --json`, `opendj dj try pick JOB CAND --etag E`, `opendj dj try undo JOB --etag E` (the etag of the job document you reviewed) |

## MCP tools

| Tool | Purpose |
|------|---------|
| `status` | Lock-file origin, health, build-info |
| `app_state` | GET-only proxy to `/api/v1/*` |
| `command` | AGENT-03 bus dispatch with mirror deltas |
| `library` | LIBM-11 library HTTP against the engine origin (`body` = JSON string) |
| `ui_url` | Engine-served SPA URL for browser MCP (does not move the shell) |
| `open_route` | Navigate the installed desktop shell to `/performance` |

### AutoDJ tools (AGENT-29, AGENT-24)

The canon doc's agent surface (`(private path)`): read the
mix in bars and sections, plan a transition, and have the page perform it on the outgoing
deck's bars. The library is `apps/autodj`; the HTTP glue is `dj_service.py`.

| Tool | Purpose |
|------|---------|
| `get_mix_state` | Per deck: track, playing, bar, bars left, section, mixer strip; master, crossfader, AutoPlay, running plan |
| `get_track_profile` | Sections, relative energy/bass, vocal bars, key segments, hot cues (never a full grid) |
| `get_mix_windows` | Ranked places to leave (`out`) or enter (`in`) a track, with reasons |
| `score_pair` | Key, tempo, vocal and bass clash for two tracks at given or best windows |
| `suggest_next` | DJ copilot next-track ranking (default: the master deck's track) |
| `list_verbs` | Every `command` primitive with its arguments and rampability |
| `list_transition_styles` | Style catalog, with why a style is unavailable; `params` (a parametric style's settings, else null) and `spotify` (the Spotify transition it mirrors, else null) |
| `propose_transition` | Top validated plans into a track, from free-text intent |
| `validate_plan` | Errors, warnings and clash metrics for a plan against the live mix |
| `schedule_plan` | Perform a plan (destructive): loads the idle deck if needed, arms the page executor; `notes` says what the compiler decided (e.g. an uncapped outgoing fader) |
| `amend_plan` | `abort`, `finish_now` or `extend` a scheduled plan |
| `try_transitions` | Render and rank N candidate transitions for a pair (TRYER-12); returns a job id and per-candidate score, vetoes, `measured k of n`, render URL |
| `pick_transition` | Record the chosen candidate for a try job (If-Match guarded; reversible) |
| `undo_transition_pick` | Remove a try job's pick |

A plan only continues a playing set: it is refused unless its outgoing deck is playing.

Agent audio is one switch per session, `OPENDJ_AGENT_AUDIO` (AGENT-25):

| Value | What the agent can do |
| --- | --- |
| `muted` (default) | Never make a sound. Every `command` that starts a deck, and every `schedule_plan`, mutes the master first (for this run only; nothing is saved to ui prefs). |
| `live` | DJ to the room. Nothing is muted, so a plan's hand-over is heard. |

Any other value is refused. Both `opendj mcp` and `opendj dj` read it, and
`get_mix_state` / `opendj dj state` report it as `agent_audio`.

Safety rails are enforced in the server: the agent audio switch above, writeback
blocked, destructive library verbs gated unless `OPENDJ_MCP_ENABLE_DESTRUCTIVE=1`
(tests only; do not enable in installed registration).

## Debug shell vs installed app

- Debug Tauri shell with WebDriver: `apps/desktop/mcp/webview-mcp.ts`
- Installed release app over lock-file HTTP: `opendj mcp` (this path)
