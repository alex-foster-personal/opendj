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

## Open the UI in a browser (UIURL-02)

The engine serves the whole UI, so a browser tab and the app window are the
same library. Settings > "Open in browser" does this from the app; the agent
twins are:

```bash
opendj open browser                 # opens http://opendj.localhost:<port>/
opendj --json open browser          # same, prints {ui_url, loopback_url, port, opened}
opendj api GET /api/v1/ui-url       # the address only, opens nothing
```

The packaged app asks for port 7700 first so the address survives a relaunch,
and takes an OS-assigned port when 7700 is in use. A portless name would need
port 80 and a privileged helper, so it is out of scope.

## MyTag rename and delete

These use the library modal's existing PATCH and DELETE `/api/v1/mytags/{name}`
routes, with the same validation, refusal responses and undo history:

```bash
opendj track mytag-rename "Warm up" "Opening"
opendj track mytag-rename "Opening" "Existing tag" --merge
opendj track mytag-delete "Opening" --confirm --undo-session review
opendj --lock PATH --json track mytag-rename "Warm up" "Opening"
opendj track mytag-rename -- --old --new
opendj api POST /api/v1/undo -H 'X-Undo-Session:review'
```

Deletion removes the tag from **every track** and requires the exact `--confirm`
flag. Rename refuses an existing target unless `--merge` is explicit. Both verbs
use undo session `opendj-cli` by default; `--undo-session SESSION` selects another
nonempty session. Quote names containing spaces; use `--` before names beginning
with a dash, placing options before it. Put global `--lock` before `track`.
`--json` may precede `track` or follow the verb. Refusals print the server's body
to stderr and return a nonzero exit code (409 conflict returns 4).

## CLI meta commands

| Command | Purpose |
|---------|---------|
| `opendj status [--json]` | Lock-file origin, health, build-info (MCP `status` parity) |
| `opendj client_errors [--since ISO] [--build-sha PREFIX] [--kind KIND] [--contains TEXT] [--limit N] [--untriaged]` | Browser errors the app recorded, filtered (AGENT-8304); `--json` is the MCP `client_errors` document: `{count, filters, errors: [{timestamp, build_sha, kind, message, event_id}]}` |
| `opendj api METHOD PATH` | Raw HTTP to `/api/v1/*`, GET/HEAD `/api/sets` and `/api/sets/*`, and the exact recorder lifecycle POST routes below; refuses traversal and non-JSON 2xx bodies; exit codes: see `apps/opendj_cli/__init__.py` |
| `opendj ingest stage PATH...` | Stage ingest uploads and conditionally start batch refresh (issue #12216); same HTTP flow as the ingest drop modal |
| `opendj dj <tool>` | AutoDJ tools (AGENT-29), same JSON as the MCP tools below: `state`, `profile`, `windows`, `score`, `suggest`, `styles`, `propose`, `validate`, `schedule`, `amend`, plus `show` (a plan as a mix score, bar grid or JSON; offline); for whole sets `set-plan`, `set-show` (offline) and `set-next`; `try` renders and ranks candidate transitions (TRYER-11): `opendj dj try --out X --in Y -n 6 --json`, `opendj dj try pick JOB CAND --etag E`, `opendj dj try undo JOB --etag E` (the etag of the job document you reviewed) |

## MCP tools

| Tool | Purpose |
|------|---------|
| `status` | Lock-file origin, health, build-info |
| `app_state` | GET-only proxy to `/api/v1/*`, `/api/sets` and `/api/sets/*` |
| `command` | AGENT-03 bus dispatch with mirror deltas |
| `library` | LIBM-11 library HTTP against the engine origin (`body` = JSON string) |
| `ui_url` | Engine-served SPA URL for browser MCP (does not move the shell) |
| `open_route` | Navigate the installed desktop shell to `/performance` |
| `client_errors` | Browser errors, filtered by `since`, `build_sha` prefix, `kind`, `contains`, `limit`, `untriaged` (AGENT-8304); same document as `opendj --json client_errors` |

### Recorder lifecycle (SETREC-12874)

These shipped MCP tools call the same daemon routes as REC and return its
status document, including ownership, recoverability and capture diagnostics.

| Tool | Arguments | Daemon route |
|------|-----------|--------------|
| `recorder_status` | none | GET `/api/sets/recorder` |
| `recorder_start` | required `source`; optional `session_id`, `device_name`, `ffmpeg_device_idx`, `sources` | POST `/api/sets/recorder/start` |
| `recorder_stop` | required `session_id` | POST `/api/sets/recorder/{session_id}/stop` |
| `recorder_recover` | required `session_id`, positive `expected_pid` | POST `/api/sets/recorder/{session_id}/recover` |

Choose `source` explicitly: `master`, `loopback`, `external`, or `none`
(tracklist only). External/loopback requires exactly one input name or
nonnegative device index. Master requires the daemon's real page/master tap.
Optional event `sources` are `djay_monitor`, `rb_history`, and `opendj_decks`;
omitting them retains the daemon default, while `[]` disables event pollers.
Session IDs use `YYYY-MM-DDTHH-MM-SS` with an optional `_N` suffix.

Before recovery, read `recorder_status`, check `recoverable`, and pass the
observed session ID and PID. A live or changed owner is refused; inspect
status again instead of retrying with an automatically refreshed PID.
Daemon refusals remain MCP errors with their `status_code` and complete
`detail`, including validation lists. Capture transitional states are returned
as reported by REC. Other `/api/sets` writes remain refused.

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
| `show_plan` | A plan as a mix score, bar grid or canonical JSON (offline) |
| `schedule_plan` | Perform a plan (destructive): loads the idle deck if needed, arms the page executor; `notes` says what the compiler decided (e.g. an uncapped outgoing fader) |
| `amend_plan` | `abort`, `finish_now` or `extend` a scheduled plan |
| `plan_set` | A whole set from a running order: the best validated transition per neighbor pair, as JSON and a set score |
| `show_set` | A set as a set score, one bar grid per transition, or JSON (offline) |
| `schedule_set_next` | Perform the set's transition that is due now (destructive); says why when none is |
| `try_transitions` | Render and rank N candidate transitions for a pair (TRYER-12); returns a job id and per-candidate score, vetoes, `measured k of n`, render URL |
| `pick_transition` | Record the chosen candidate for a try job (If-Match guarded; reversible) |
| `undo_transition_pick` | Remove a try job's pick |

A plan only continues a playing set: it is refused unless its outgoing deck is playing.

Every tool that takes a plan accepts a **mix score** too (AGENT-5858): the same plan as text, one
move per line, and each proposal carries one as `notation`. Grammar and examples:
`apps/autodj/notation.py`; design notes: `(private path)`.

```
transition bass_swap_blend
out D1 trk-out from 113 for 16
in  D2 trk-in from 17 for 16
tempo sync

@0       D2 play
@0       D2 eq_low kill
@0..8    D2 fader -> 1
@8       D1 eq_low kill
@8       D2 eq_low flat
@8..16   mix -> 1
@12..16  D1 fader -> 0
@16      D1 stop
```

A whole set is one document too (AGENT-27): a **set score** is a `set <name>` header and one
`transition` block per handover, each block a mix score. Every transition must mix out of the deck
and track the previous one brought in, after that one finishes. An agent plans the set once with
`plan_set`, then calls `schedule_set_next` after each transition ends: it schedules the first
transition whose outgoing track is playing while its incoming one is not, and nothing while a plan
is running. Code: `apps/autodj/setscore.py`.

```
set warmup
intent "wind down"

transition long_blend
out D1 trk-a from 65 for 32
in  D2 trk-b from 1 for 32
...

transition long_blend
out D2 trk-b from 65 for 32
in  D1 trk-c from 1 for 32
...
```

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
