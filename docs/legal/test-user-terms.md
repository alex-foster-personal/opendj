# Open DJ test build: diagnostics terms

Version: 2026-11-04 (`TERMS_VERSION` in `apps/shared/telemetry/consent.py`;
bump both together, and the app asks every tester again).

This is what the first-launch dialog shows. It is written for the tester,
not for a lawyer, and it must stay true: every claim below is backed by a
test or a build check named in `docs/telemetry.md`.

## What you are agreeing to

You are running a pre-release build of Open DJ. If you agree, the app sends
diagnostics to Sentry (an error-monitoring service; this project's data is
held in the EU region) so the people building it can see what broke without
asking you to find a log file. If you do not agree, the app works exactly the
same and sends nothing.

## What is sent

- **Crash and error reports.** The error type and message, the code
  location (file, line, function), the app route you were on (for example
  `/performance`), the build id, and machine facts such as OS version,
  browser engine, sample rate and channel count.
- **Session replays.** A recording of the app's screen while you use it,
  with **all text masked** (track names, artists and playlists on screen are
  replaced by blocks), **all media blocked** (album art is not captured), and
  no canvas capture (waveforms are not recorded). Replays include where you
  clicked and any errors that fired at the time.

## What is never sent

- Audio in error reports or session replays. Diagnostics never include your music files.
- Track titles, artists, album or playlist names inside error reports. The
  reports carry an allowlist of fields; anything not on it is redacted.
- File paths. A path is reduced to its extension, so `.../Artist - Title.flac`
  arrives as `<path.flac>`.
- Your account identity or IP address. Reports carry no user block.
- Anything at all while a deck is playing or audible. Error reports are held
  in the local log, and replay recording stops, until the set is over.

## What this consent does not cover

Separating a track into stems, when you ask for it, can upload that track's
audio file to the project's stems relay so a GPU worker can return FLAC stems.
That send is not a diagnostic and is not gated by agreeing or declining this
dialog. A checkout with no stems relay URL configured uses the local worker
and keeps the file on your machine.

## Where it goes and how long it stays

Reports go to this project's Sentry organization in the EU (region `de`).
Sentry keeps error events and replays for a limited period, on the order of
weeks, under the plan the project is on; nothing is shared onward.

## Changing your mind

- Declining now: nothing is sent and the app does not ask again.
- Later: create an empty file named `telemetry-opt-out` in the Open DJ data
  folder (under `Library/Application Support`) and restart the app, or delete
  `telemetry-consent.json` in that folder to be asked again.

## For the maintainers

The dialog reads `GET /api/v1/telemetry/consent` and writes
`PUT /api/v1/telemetry/consent`. The engine holds every Sentry send until the
stored decision is `accepted` for this terms version (OBS-05); the page loads
the Session Replay loader only after acceptance (OBS-06). `docs/telemetry.md`
has the full inventory of what leaves the machine.
