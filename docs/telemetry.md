# Error telemetry (Sentry)

Open DJ reports uncaught exceptions to Sentry so a tester does not have to be
talked through finding a log file. Before a tester accepts the terms nothing
leaves the machine at all. After acceptance a packaged build sends errors AND
session replays (masked text and attributes, no media, no waveforms; every
consented session by default, `SENTRY_REPLAY_SESSION_SAMPLE_RATE` to lower
it, see "Session replay"); a checkout sends errors only when it opts in. No
profiling, no logs, no metrics, no analytics, and request tracing only when
one process opts in (see "Tracing" below). Nothing is sent while a deck is
playing or audible (see "The live-set gate").

State of the org as reviewed Mon 21 Sep 2026: `open-dj-be` receives engine
AND browser errors (the latter tagged `origin=browser`); `open-dj-fe` exists
and has never received an event; the spans, logs, metrics and replay datasets
are empty. The Loader Script toggles on the preview key (tracing, replay,
logs and metrics, feedback, SDK debug) had been switched on in the org UI and
were switched OFF the same day: no loader script is embedded anywhere in the
app, so they controlled nothing, and a pasted loader would bypass the
scrubber and record the library through replay. Keep them off.

This document covers how the integration behaves and what leaves the machine.

## On or off

One tri-state variable, and the build decides when nobody says otherwise.
There is no browser-side switch, because the browser does not talk to Sentry
(see "Why the engine reports browser errors" below).

| Setting | Value |
|---|---|
| Switch | `OPENDJ_TELEMETRY` |
| DSN | `SENTRY_DSN` |
| Environment | `SENTRY_ENVIRONMENT` (dev / preview / fleet / ship; else the build decides) |
| Tracing | `SENTRY_TRACES_SAMPLE_RATE` (default 0, errors only) |
| Replay | `SENTRY_FRONTEND_DSN` (checkout) or the baked `frontend_dsn` (dmg); `SENTRY_REPLAY_SESSION_SAMPLE_RATE` (default 1.0) |
| Consent | `<data-dir>/telemetry-consent.json`, written by `PUT /api/v1/telemetry/consent` |
| Build stamp | `OPENDJ_PAYLOAD_MANIFEST` (payload vs repo) |

| State | Meaning |
|---|---|
| unset | **The build decides.** A packaged build (a dmg) ships its DSN in the payload and is **on** (OBS-04); the tester turns it off by creating `telemetry-opt-out` in the engine data directory. A repo checkout is **off**: a DSN in a developer's `.env` is not consent, and fleet test builds opt in with `OPENDJ_TELEMETRY=1`. |
| `1` / `true` / `on` | On. A missing DSN (env or bundled) is a **hard error at startup**. Wins over the opt-out marker: an operator who asked by name is not the marker's audience. |
| `0` / `false` / `off` | Off. The Sentry SDK is never imported at all. Wins over everything. |

"Off" is provable rather than asserted: `sentry_sdk` is imported inside
`init_telemetry` and nowhere else, so a disabled boot leaves it absent from
`sys.modules` and a test can assert that by inspection. The browser ships no
Sentry code in any configuration.

### Why the two failure modes differ

An **explicit** enable with no DSN raises and refuses to start. Somebody
asked for telemetry by name, and starting anyway would mean the errors they
were waiting on never arrive with nothing to say so.

A **shipped build** whose operator did not set `OPENDJ_TELEMETRY=1` stays off
even when a DSN is present. The user never asked for telemetry. Fleet test
builds opt in explicitly. If an explicit enable cannot load `sentry-sdk`,
startup fails loudly; a missing SDK on a default-off boot cannot take the
app down.

Every engine and client error also appends one local JSONL (override
`OPENDJ_ERROR_SINK_LOG`, else `~/jobs/logs/opendj-error-sink.jsonl` on nucbox)
carrying `error_id`, `host`, and `build_sha`. Build and CI failures post to
that same file as `kind=build`.

## Release and environment

`environment` is `ship` for an installed build and `dev` for a checkout,
unless `SENTRY_ENVIRONMENT` names one of `dev`, `preview`, `fleet`, `ship`
(the afmac preview launcher sets `preview`). Any other value fails at
startup. `release` is the full git sha of the build.

## The live-set gate (OBS-02)

The reporting policy says never send while any deck is playing or audible.
`apps/shared/telemetry/live.py` is that rule in code, on both paths:

- A **browser error** carries `any_deck_live` from the page. `app-init.ts`
  registers `anyDeckPlaying` (playing OR audible, both flags, the same read
  PERFMODE-04 sheds on) as the probe `client-error-reporting.ts` consults when
  it builds a payload. `null` means no probe was registered (pre-boot, or a
  probe that threw) and never means "idle". When the flag is a bool it is
  authoritative: `true` holds the event, `false` sends it whatever the engine's
  mirror says.
- An **engine exception** (and an opt-in transaction) asks a probe the engine
  entry point registers over the page's UI mirror
  (`PUT /api/v1/state/ui-mirror`, published once a second): any
  `decks[*].playing` or `audible`, refused when `received_at` is older than
  15 s so a page that closed mid-track cannot mute the engine forever. A
  missing or raising probe reads NOT live: a broken gate fails toward
  sending, never toward silence.

The local JSONL sink and the daily client log are written BEFORE the gate, and
the gate runs BEFORE the quota budget, so nothing is lost locally and a held
event spends no quota. Held events are counted in `LIVE_GATE.suppressed`. The
consequence to know about: a fault that fires mid-mix shows up in Sentry only
if it recurs off-set. The hourly nucbox sink triage (OPS-34) reads the local
sink, not Sentry, so it still sees every event.

## Tracing (OBS-03, opt-in)

`SENTRY_TRACES_SAMPLE_RATE` is a number in [0, 1]. Unset or `0` is errors
only, which is every build today. Any other value turns request tracing on
for that process at that fraction; a value outside the interval (or not a
number) refuses to boot, naming the variable, even when telemetry is off, so
a typo is found on the first boot and not on the one that matters.

Transactions pass `before_send_transaction`: the live-set gate above, then
the same scrub as an error, extended to spans. The transaction name and every
span description are path- and token-scrubbed; span `data` and `tags` are
allowlisted like `extra`. They spend Sentry's span quota, not the error
quota, and mint no error id and write no sink row.

Profiling, session replay, logs and metrics have no switch. Replay is ruled
out as long as the library is what the page shows; logs already ride as
breadcrumbs on the error that needs them.

## Quota guard (ADR-0040)

Sentry bills per event, and Team and Business both include 50k errors a
month. `apps/shared/telemetry/budget.py` caps what one process sends: 3
events per error id per rolling hour, 100 per UTC day. Perf-event console
mirrors (`[perf-event]` in a console-warn or console-error) never leave the
machine, because the perf-event log already escalates the real fault as a
`ui-error`. The local JSONL sink is written first and still records every
event, so Sentry counts are a lower bound and the sink is the full count.
The limits came from replaying a real sink; see `specs/sentry-quota-spec.md`
and `scripts/sentry_budget_replay.py` before changing them.

Logging never creates Sentry events (`LoggingIntegration(event_level=None)`);
log lines ride along as breadcrumbs. An error reaches Sentry only through an
explicit capture or `warning_log`'s ERROR forward, so the client-errors route
logs at WARNING. `logentry` (template, params, formatted) is scrubbed like
`message`. Vite dev-server console lines (`[hmr]`, `[vite]`) stay local.
Transient browser fetch/abort TypeErrors stay local too (OBS-07): Safari
`Load failed`, Chromium `Failed to fetch`, Firefox's network TypeError, and
abort strings. Exact match only, so a missing dynamically imported module
still ships. The loader SDK `beforeSend` on `open-dj-fe` applies the same
drop so GlobalHandlers cannot open a second issue for the same blip.

Org-side settings that complete the guard (Sentry org, not code). State as
applied and measured Wed 16 Sep 2026, org `maintainer`, region `de.sentry.io`:

- **Spike protection: ON.** `quotas:spike-protection-disabled` is `false` on
  `open-dj-be`. This is the backstop that actually applies here.
- **A separate client key (DSN) for preview/dev hosts: DONE.** `open-dj-be`
  carries `preview-dev (agents + preview Macs)` alongside `Default`. Doppler
  `OPENDJ_SENTRY_DSN_BACKEND` (the only thing `opendj-preview/engine-launch.sh`
  reads) points at it, and the `Default` DSN is parked as
  `OPENDJ_SENTRY_DSN_BACKEND_SHIP` for ship builds, which today bake no DSN.
- **A per-key rate limit on that key: NOT POSSIBLE on this plan.** Key rate
  limits are Business and Enterprise only; this org is on Team. The API does
  not say so: `PUT .../keys/{id}/` with a `rateLimit` body returns **200 and
  silently stores `null`**, so a script that trusts the status code reports a
  limit that does not exist. Read the key back before believing it.
  What the separate key buys without a limit is a **kill switch**: setting
  `isActive=false` on it stops every preview and agent host reporting while
  leaving testers on `Default` untouched.
- A pay-as-you-go budget cap. Past it, Sentry drops events instead of
  billing them.

Measured the same day, so the missing rate limit is a small risk rather than a
live one: **394 accepted errors in 14 days, about 844 a month against the
50,000 the plan includes**, with 190 `client_discard` in the same window, which
is the client-side budget visibly refusing events. The client-side guard, not
the org settings, is what keeps this number down, which is what ADR-0040
argued: a server-side limit is blind to which fault is repeating.

This comes from the build identity the repo already keeps rather than a
second notion of "is this a real build": `apps/engine_core/build_info.py`
reports `source="payload"` for an installed build and `source="repo"` for a
checkout, and the engine entry point feeds that to `decide_telemetry`.

A build that cannot state its own sha is treated as dev and tags no release.
It never guesses from live git: a build that cannot say what it is must not
claim a release.

## Why the engine reports browser errors

Browser errors reach Sentry, but the browser never talks to Sentry itself.
There is no `@sentry/browser` in `package.json` and no Sentry code in any
chunk.

The forcing constraint is the CI bundle gate: `scripts/check-bundle-size.sh`
allows 250 KB gzipped across every emitted chunk, and main already sits at
about 250.8 KB of it. That leaves roughly 5 KB, while the smallest useful
errors-only browser SDK is an order of magnitude larger. Lazy-loading does
not help, because the gate sums every chunk whether or not it is fetched.

That constraint turns out to favor the better design anyway. Every client
error already travels to the engine through the browser's own durable queue
(`reportClientError` -> `POST /api/v1/client-errors`), which survives an
engine that is temporarily down, so nothing is lost by reporting from the
engine side. `capture_browser_error` forwards it, tagged `origin=browser`,
after the local daily log is written. One scrubber then governs every event
the app sends instead of two implementations that have to be kept in
agreement.

What this costs: no Sentry-native browser breadcrumbs, and browser stacks
arrive as message text rather than SDK-parsed frames.

## What is sent, and what is not

The library is the user's private music collection, and a track title is the
single most identifying thing this app touches. The payload is therefore an
**allowlist**, not a denylist: a key nobody has approved is redacted, so a new
field is invisible in Sentry until somebody adds it on purpose. A missing
diagnostic costs one PR; a leaked track title cannot be taken back.

**Sent**

- Exception type, message and stack frames (file, line, function).
- The allowlisted context keys in `ALLOWED_CONTEXT_KEYS`: route, method,
  status, error code, engine version, contract rev, boot id, build source,
  job id and kind, deck id, adapter, sample rate, channels, duration, counts,
  platform.
- SDK-built context blocks: runtime, OS, browser, device, trace ids.
- Release sha and environment.

**Never sent**

- Track titles, artists, album names, playlist names, or any other library
  content. Any context key not on the allowlist becomes `[redacted]`.
- Filesystem paths. Paths rooted at a real filesystem root, and any bare
  filename with an audio extension, are reduced to `<path.mp3>` in exception
  messages, breadcrumbs and context alike. App routes such as `/performance`
  are deliberately preserved: they are diagnostics, not paths.
- Frame-local variables. `include_local_variables=False` on the backend and
  `defaultIntegrations: false` on the frontend. This is the switch that stops
  a local named `track` carrying an artist and title into an issue.
- Request bodies, cookies and headers. Dropped wholesale, not filtered.
- User identity and IP. `send_default_pii=False`, and the `user` block is
  deleted from every event regardless.
- Anything at all, if the scrub throws. A payload whose contents could not be
  checked is a payload whose contents are unknown, and unknown contents do
  not leave the machine.

Token-shaped strings (bearer tokens, `sk-`, `ghp_`, Slack tokens) are filtered
from free text as well.

All of it lives in `apps/shared/telemetry/`, split in two: `scrub.py` is the
pure policy (no SDK, no environment, no I/O, so it reads and tests as policy
on its own) and `__init__.py` is the gating and the SDK calls.

It sits in `apps/shared` rather than `apps/engine_core` because
`apps.engine_core` imports `apps.webui` and `apps.webui` imports the
telemetry module, so it has to belong to neither or the import graph closes
a cycle. The build-identity lookup that feeds the decision stays in the
engine entry point for the same reason.

## Where events come from

Engine exceptions come from the Sentry FastAPI integration, wired in the
entry point before `create_app` so route handlers are instrumented as they
are registered.

Browser errors come from the funnel the app already had: `reportClientError`
in `$lib/client-error-reporting` catches window errors, unhandled rejections,
SvelteKit's `handleError` and explicit UI reports, queues them durably in
`localStorage`, and POSTs them to `/api/v1/client-errors`. The route writes
the daily log first and forwards to Sentry second. The local log is the
record that must never be lost; Sentry is remote aggregation on top of it.

No frontend file changed to make this work.

## What the dmg ships (OBS-04)

Until Mon 21 Sep 2026 a dmg carried neither the SDK nor a DSN, so no installed
engine had ever reported an error. Now:

- **The SDK is in the payload.** `scripts/build_engine_payload.py` requests
  the `observability` extra, and the payload's own interpreter must import
  `sentry_sdk` in the verify step or the build fails. (CI's `requirements.txt`
  still omits it; the pytest lanes add the extra themselves.)
- **The ship DSN is baked.** `scripts/payload_telemetry.py` reads
  `OPENDJ_SENTRY_DSN_BACKEND_SHIP` from the build environment, else from
  Doppler `general` / `dev_personal`, and writes `telemetry.json` at the
  payload root. The build host's own `SENTRY_DSN` is the preview-dev key and
  is deliberately not a fallback: a dmg reporting under it would file every
  tester as a preview host. No DSN, no build; `scripts/dmg_preflight.sh`
  checks it up front.
- **The launcher exports the path** as `OPENDJ_BUNDLED_TELEMETRY`;
  `apps/shared/telemetry/bundled.py` loads it and `decide_telemetry` turns a
  packaged build on under that DSN with environment `ship` and the build sha
  as release. `SENTRY_DSN` in the environment, if an operator sets one,
  outranks the bundle.
- **Opt-out.** A tester creates `telemetry-opt-out` (any contents) in the
  engine data directory and the next boot is off, with the marker named in
  the log line. `OPENDJ_TELEMETRY=0` also works where there is a shell.
- **Damaged installs keep running.** A launcher naming a bundle the engine
  cannot read logs `[ERROR] bundled telemetry unreadable` and boots with
  telemetry off; a payload that lost its SDK logs and boots. Neither state
  describes a build that shipped, because the build verifies both.

Consent is the install: a tester who runs the dmg is running telemetry until
they create the marker. What leaves the machine is unchanged (see "What is
sent, and what is not"), and the live-set gate holds mid-mix.

## Consent: nothing leaves until the tester agrees (OBS-05)

A packaged build starts its SDK at boot, but a tester has not agreed to
anything by double-clicking a dmg. `apps/shared/telemetry/consent.py` is the
gate between the two: the SDK is live, the local sink records every event,
and no error, forwarded browser error or transaction reaches the transport
until `<data-dir>/telemetry-consent.json` says `accepted` for the current
`TERMS_VERSION`. An operator who set `OPENDJ_TELEMETRY=1` by name (a fleet or
preview host) is not a test user and is never held.

The page asks once, after the boot window, with the text in
`docs/legal/test-user-terms.md`, and only when there is something an
acceptance would turn on (`telemetry_active` or a replay loader URL on
`GET /api/v1/telemetry/consent`). The consent module and the dialog are
imported from a deferred boot task and mounted into `<body>` on demand, so
they are charged to the deferred shell rather than the library page's
first-paint bundle budget. `PUT` records the answer and flips the
in-process gate, so acceptance takes effect on the next captured event with
no restart. `declined` is remembered and read as an opt-out on the next boot.
Bumping `TERMS_VERSION` makes a stored acceptance read as `undecided`, so the
tester is asked again and nothing is sent in between.

## Session replay for test users (OBS-06)

Replay is the one signal that needs a browser SDK, and the bundle gate does
not allow one in the app's own chunks. The Sentry Loader Script is an
external script, which the gate does not count, so that is what runs: the
engine derives the loader URL from the frontend DSN (`OPENDJ_SENTRY_DSN_FRONTEND`
baked into `telemetry.json` for a dmg; `SENTRY_FRONTEND_DSN` in `.env` for a
checkout) and the page injects it only after acceptance.

What a replay contains, set in `window.sentryOnLoad` before the SDK's own
init: the app's screen with **all text masked** (track names become blocks),
**all inputs masked**, **all media blocked** (no album art), no canvas
capture (waveforms are not recorded), clicks, and errors at the time.
Masking covers the attributes the library UI carries track text in
(`MASKED_ATTRIBUTES`: `title`, `alt`, `aria-*`, `data-title` and friends, a
superset of the SDK default), and a `beforeAddRecordingEvent` hook rewrites
the click and input breadcrumbs the SDK derives from the live element, whose
CSS selector otherwise names the `title`/`aria-label`/`alt` value verbatim
(measured through the real loader, Mon 21 Sep 2026: a masked replay still
carried the track title in its `ui.click` breadcrumb). Only structural
attributes keep their value there (`SELECTOR_ATTRIBUTES_KEPT`). The
real-loader e2e decodes the uploaded rrweb segment and asserts the visible
fixture title is absent. The loader SDK's own error events pass a path-scrubbing `beforeSend` (a port of
the engine scrub's path rule) and carry `origin=browser-sdk`, so the
engine-forwarded copy of the same error is told apart. Transient fetch
TypeErrors (`Load failed` / `Failed to fetch`) are dropped in that
`beforeSend` as well (OBS-07). Sampling is
`replaysSessionSampleRate` 1.0 by default (`SENTRY_REPLAY_SESSION_SAMPLE_RATE`
to lower it; the test cohort is small) and `replaysOnErrorSampleRate` 1.0.

The live-set rule holds here too, on three paths. A rune-effect watcher
(`live-transport-watch.svelte.ts`, wired by app-init) reads `anyDeckPlaying`
and stops the replay in the microtask that flips a deck to playing or
audible, before any of the SDK's flush timers can run; the stop passes
`forceFlush: false`, which DISCARDS the segment buffered since the last
periodic flush instead of sending it, so nothing recorded up to that instant
leaves either; and the loader SDK's `beforeSend` returns null while a deck is
live, so a browser exception it captures mid-set is dropped rather than sent
around the engine's `any_deck_live` gate. A 2 s poll is the fallback for a
page without the watcher and restarts the replay after two idle polls, from a
fresh segment. Replays land in the
`open-dj-fe` project, whose key's loader options were set to replay only
(tracing, debug, feedback, logs and metrics off) on Mon 21 Sep 2026.

Two more rules on the loader path. A stored `accepted` never outranks THIS
boot's decision: with the `telemetry-opt-out` marker or `OPENDJ_TELEMETRY=0`
the engine has no client, the consent route withholds the loader URL, and the
page loads no replay SDK even though the file says accepted. And the loader
SDK's own error events are scrubbed by a port of the engine scrubber
(`scrubEvent` in `telemetry-scrub.ts`: tokens and paths out of every
string, the same context allowlist, SDK context blocks kept in shape, frame
locals and request/user/server_name removed, fail-closed); the two allowlists
are copied from `scrub.py` and a unit test fails if they drift.

Quota: replay is billed per replay, separately from errors. Read the org's
usage before widening the cohort; lower `SENTRY_REPLAY_SESSION_SAMPLE_RATE` on
the build host if it climbs.

## Known gaps

- **No in-app switch.** Consent is asked once, and the opt-out is a marker
  file (or deleting `telemetry-consent.json` to be asked again), not a
  settings toggle; a toggle needs the settings store, which the engine
  initializes after the telemetry decision. Follow-up.
- **Replay restarts only after two idle polls.** Up to 4 s of a
  stopped-but-ringing-out deck passes before recording resumes; that is
  silence on the recording side, never a send. The stop side is immediate
  (watcher) and discards; the residual is the SDK's own periodic flush firing
  in the same task as the play dispatch, which the effect ordering rules out
  for a state flip but which has not been measured against every audio
  start path.
