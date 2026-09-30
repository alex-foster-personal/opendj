# Architecture: music-dj-tools

*Rewritten Sat 15 Aug 2026. Supersedes the Fri 17 Apr 2026 revision, which described a
different product.*

## What this is, and why it exists

**An agent-drivable DJ rig, built to open up the DJ-software moats.**

The incumbent tools (rekordbox, Serato, Traktor) are closed at every layer that matters:
closed library formats, hardware locked to vendor software, no automation surface, and no
way to extend them. This project attacks that. Concretely it aims at:

| Goal | What it means here |
|:-----------------------|:--------------------------------------------------------|
| **Agent-drivable** | Every UI control also reachable as a typed command, so agents can operate, test and extend the rig. No vendor tool permits this. |
| **Missing capabilities** | State-of-the-art stem separation, real library management, inline lyrics. Things rekordbox has no equivalent for. |
| **Low bloat, high perf** | The rig should be fast and small, not a plugin host. |
| **Community modding** | Easy to fork and extend, which follows from open formats plus an automation surface. |
| **No lock-in** | Local-first. The library outlives any one vendor. |
| **Cross-platform library** | The library is the asset; it should move between tools freely. |
| **Honest library view** | Missing file paths are surfaced so they can be repaired. |

The current build target is `/performance`: a pixel-faithful, local-only clone of the
rekordbox 7 Performance-mode screen (4-deck layout). **Parity is the strategy, not the
goal.** Cloning a familiar shell means a working DJ can pick it up with no retraining, and
it gives every component an unambiguous acceptance test: does it match rekordbox on real
data. The actual product is what parity makes possible afterwards, which is the capability
column above.

`PARITY-TODO.md` is the live board and the authority on what is shipped, partial or pending.
This document explains the shape underneath it.

## The rule of the build

One principle explains most decisions in this codebase, and it is worth stating before any
module list:

> **Nothing is mocked, nothing is invented.** A control with no real data source renders
> visually authentic but inert, with the tooltip `not implemented - see PARITY-TODO`.

Its consequences appear everywhere, and they are not separate policies. They are the same
idea applied at each layer:

| Rule in situ | Layer |
|:-----------------------------------------------------|:-------------------|
| Show the actual library counts, never screenshot counts | library listing |
| Missing file paths surfaced as missing-file rows, never masked | library listing |
| "no invented grid points"; every beat drawn comes from a real PQTZ row | waveform |
| "empty deck = flat dark row, never invented" | deck strip |
| Absent PVDI vocal data renders as an honest `not_analyzed` state | vocal bars |
| `layoutOfBuffers()` throws on an unrecognised stem set; there is no default layout | audio engine |
| Manifests that do not declare `layout` are rejected at the API edge | stems API |
| Six-rail safety on every destructive write (see below) | vendor writers |
| Fail fast, no hidden defaults, no fallbacks that mask failure | everywhere |

If a change would make the UI show something the data does not support, it is wrong,
regardless of how good it looks. That is the single most useful thing to know before
touching this repo.

## Two products, one substrate

The repo contains two distinct products built roughly four months apart. Reading it as one
system is the main way to get confused.

**April 2026 (v1.0), the library-truth layer.** A bi-directional sync, enrichment and
authoring toolchain: ingest from Rekordbox/djay/Serato/Traktor into a shared state DB,
analyse, dedup, reconcile broken links, smart playlists, set recording, Spotify import,
voice, a Tauri launcher. CLI-first. `README.md` still describes this product and is stale.

**July 2026 onward, the performance rig.** The `/performance` 4-deck UI, Web Audio engine,
stem separation, vocal analysis, MIDI controllers. This is the current build.

The second is not a rewrite of the first. **The April work is substrate**, deliberately
harvested by the new build. `PARITY-TODO.md` names the reuse explicitly for
`apps/{sets,analysis,voice,spotify,smartlists,reconcile,launcher,dj_copilot}`.

Measured Sat 15 Aug 2026, the webui imports 13 April-era modules (`apps/shared` 31 times).
That is a live dependency, not a vestige.

### Warm, wired and cold

The useful distinction is not "old versus new", it is how far each module has travelled
toward the rig.

| Tier | Meaning | Modules |
|:---------|:-------------------------------------------|:------------------------------|
| **Warm** | Serving real data to `/performance` today | `shared`, `webui`, `sync`, `analysis`, `tags`, `stems`, `vocals`, `reconcile`, `adapters`, `open_dj` |
| **Wired** | Endpoint exists and the UI calls it, but no data has been created yet | `smartlists`, `pairings`, `spotify`, `voice` |
| **Cold** | Built, tested, not yet surfaced in the rig | `sets`, `play_analytics`, `dedup`, `dj_copilot`, `cloud`, `audit`, `launcher` |

**Cold does not mean dead.** Each cold module is staged work with a named entry point on the
parity board. Deleting them would destroy deliberate substrate. The honest open question is
which of them the rig actually needs, and that is a product call, not a code observation.

## The spine: `data/state/state.db`

Re-verified Sat 15 Aug 2026 and unchanged since April. Owned by `apps/shared/state/`:
`schema.py` (DDL + idempotent `apply_migrations`), `db.py` (WAL, `busy_timeout=5000`,
`foreign_keys=ON`), `ids.py` (three-tier `stable_id`: ISRC -> chromaprint ->
path+size+duration), `writer.py` (`StateWriter`, the only sanctioned mutation surface),
`events.py` (in-process bus with a durable tail in `events`), `ingest/` (one adapter per
vendor).

Four rules hold the spine together:

1. **Derivative, not authoritative, EXCEPT for a named set of tables.** State can be wiped
   and rebuilt from vendor DBs for everything else. This is what makes aggressive tooling
   safe on the rest of the schema.

   **Decided (LIBM-D4, ADR-0022, Sun 14 Sep 2026): the rule is amended, not restored.**
   `track_fields` rows where `field_name in ('notes','tags')`, `pairings`, `smartlists`,
   `play_orders`/`play_order_entries`, webui-created playlists before an explicit writeback,
   and a webui-edited `rating` that has diverged from the vendor's own value are genuinely
   authoritative: no ingest adapter writes them, and re-ingest for `rating` overwrites rather
    than merges. No ingest adapter writes the other named authoritative tables. Several
    of the features that populate them have
   since shipped (LIBM-83 smartlists CRUD, LIBM-62/66 notes/tags/genre/comments bulk edit),
    so backup must protect populated authoritative tables as well as empty ones.
    `apps/sync_hub/hub_backup.py` backs up the hub's merged copy of these tables for
   any machine that has synced; per-machine local backup for the unenrolled/offline gap: see
   [state-authoritative-backup.md](ops/state-authoritative-backup.md). Playlists and rating are also writeback-eligible to rekordbox
   (`LIBM-117`, gated, off by default) as an alternative to backup-only; the rest of the named
   set has no vendor equivalent to write back to at all. Full rationale: ADR-0022.
2. **EAV with provenance.** Values live in `track_fields` with source, observed_at and
   confidence; history in `track_field_history`. New fields need no migration. Provenance is
   what lets the UI tell the truth about where a number came from.
3. **Single writer.** Only `StateWriter` may `INSERT`/`UPDATE`. Five exceptions exist and
   are catalogued in `refactor1.md` R1-07.
4. **Events are durable.** Every write appends to `events` in the same transaction, so a
   consumer that misses a live event can rebuild from a cursor.

The state schema includes tracks, fields, events and playlist membership tables;
their row counts and database size depend on the imported library.

`data/master.plain.db` is a **static decrypted copy** of rekordbox's `master.db`, read
alongside it. Refreshing means re-decrypting and then `rm -rf data/state/anlz-cache/`.
Rekordbox writes land in `master.db-wal`, so copying `master.db` alone silently misses recent
rows: copy db + wal + shm together, then `sqlcipher_export`.

## The rig: `/performance`

SvelteKit (Svelte 5 runes) frontend, client-side Web Audio deck/mixer graph, FastAPI daemon
on `:8585`, ANLZ parsed via pyrekordbox over the decrypted rekordbox DB. Frontend dev server
on `:5173`.

### Layout

Fixed-viewport grid: **TopBar**, **WaveformStack** (4 rows), a deck area of four **Deck**s
around a central **Mixer**, **BrowserPanel**, **QuickDrawMenu**. All under
`src/lib/components/rb/`.

| Panel | Direct children |
|:-----------------|:-----------------------------------------------------------|
| `Deck` | `DeckHeader`, `HotCueBank`, `JogDial`, `LoopCluster`, `PitchFader`, `StemRow`, `StripWaveform`, `TransportCluster` |
| `Mixer` | `AssignMatrix`, `ChannelStrip`, `Crossfader`, `HeadphoneCluster` |
| `WaveformStack` | `WaveRow` x4 |
| `TopBar` | `MidiPanel`, `midi/MidiLearnLogPopout` |
| `BrowserPanel` | `RecommendedSection`, `SuggestNextStrip`, `BulkEditModal`, `FindReplaceModal`, `MyTagEditorModal`, `browser/{AddTrackSearch,IconRail,PaneTabs,PlaylistTree,SearchBox,TrackTable}` |

### Agent-native parity, the load-bearing constraint

`performance-ipc.svelte.ts` is the reason this project is not just a rekordbox clone.
Every control routes through one validated, serialized dispatch/query/capture path, shared
by the UI and by agents. A control wired directly to the engine, bypassing the dispatcher,
is a defect even if it works, because it makes that capability invisible to automation.

This is also the project-wide rule from `CLAUDE.md`: every UI interaction must have a
matching CLI/IPC/HTTP endpoint. Audit for parity gaps after any UI work.

Known debt: Mixer and TopBar controls are not yet routed through the dispatcher
(`PARITY-TODO.md`, S-list).

The jog radial waveform (DECKUX-02) is a display preference on `GET/PUT
/api/v1/ui-prefs` (`jog_radial_waveform`), not an engine command.

### The transport clock invariant

Non-obvious and easy to break. Public `position_ms`, `audible` and `transport_pending` must
come from the **output presentation clock** plus acknowledged schedule revisions.
`AudioContext.currentTime` is only the control and render planning clock. Display state is
engine-owned; the UI never optimistically predicts position. Waveform drag is a coalesced
direct-manipulation seek with one in-flight request plus a latest trailing target.

Get this wrong and the waveform lies about where the music is, which violates the rule of
the build in the most visible possible way.

The Rust engine (`odj-audio`, phase 20) keeps the same rule in a different form. It reports
each deck's `position_ms` and `rate` about 30 times a second, and the page extrapolates
`position_ms + rate * elapsed` from the last report it received
(`apps/webui/frontend/src/lib/audio-engine/client.ts`, `predictPositionMs`). That is
prediction between acknowledged reports, never ahead of one: a play, stop or seek the page
sends moves nothing until a state message shows the engine applied it.

### Audio graph

```
AudioBufferSourceNode -> TRIM gain -> lowshelf(250Hz) -> peaking(1200Hz)
  -> highshelf(5kHz) -> channel-fader gain -> crossfader gain -> master gain -> destination
```

Stem playback swaps the single source for `AlignedStemDeckProcessor` (`stem-graph.ts`): one
`StretchDeckProcessor` plus one `GainNode` per stem part, summed back in at TRIM.
Construction validates sample rate, frame count, channels and duration across every part
before connecting anything, and schedules parts via `Promise.allSettled` so a partial
failure disconnects the whole group rather than leaving a silently desynced deck.

Master Tempo is key-preserving time-stretch through a pinned `signalsmith-stretch` 1.3.2
AudioWorklet. Worklet timeouts and processor errors are terminal, never degraded.

Beat Sync defaults to `BAR`, which PREFERS an exact lock preserving PQTZ beat numbers 1->1
through 4->4. When only a 0.5x/2x fold fits the deck's pitch range, `BAR` takes the fold and
warns in orange rather than refusing; a folded lock searches every follower
beat number, because the fold has given up the bar count. The pitch-range bound is still hard:
a ratio outside it fails loudly. `BEAT` is an explicit opt-out permitting 0.5x/2x normalisation
without a warning. Fresh decks default Quantize, Beat Sync and Master Tempo on.

### Stem layouts, and why `layout` is load-bearing

| Layout | Parts | Controls exposed |
|:------------|:-----------------------------|:-----------------------------|
| `demucs4` | vocals, drums, bass, other | vocal, instrumental, drums (instrumental drives bass + other together) |
| `roformer2` | vocals, instrumental | vocal, instrumental only |

`roformer2`'s `instrumental` already contains drums, so a DRUMS control would be a no-op.
It is therefore omitted from `STEM_LAYOUT_CONTROLS` and rendered inert, rather than shipped
as a button that silently does nothing. `layoutOfBuffers()` infers layout from the exact
key-set of decoded buffers and throws on anything unrecognised. There is no default.

Heavy ML never enters the repo venv. `apps/stems` and `apps/vocals` shell out to standalone
PEP 723 scripts (`scripts/stem_bundle_worker.py`, `scripts/vocal_region_worker.py`) that
declare their own deps and run under `uv run`. Verified: no `import torch` or `import demucs`
anywhere under `apps/`.

On-disk corpora (note the naming problem, `refactor1.md` R1-05):

| Directory | Loadable state | Layout | Notes |
|:-----------------------------------|:------------------------------|:------------|:------|
| `data/state/stems` | Requires a valid manifest matching the actual audio format | demucs4 | Repair incompatible manifests before reuse. |
| `data/state/stems-roformer-spike` | Requires valid bundles keyed by stable ID | roformer2 | Loose title-keyed sets need stable-id mapping and bundle assembly before the loader can see them. |
| `data/state/stems-demucs-ab` | Requires a valid `manifest.json` | demucs4 | Inventory alone does not establish loadability. |

The ingest coverage and refresh queue use every configured stem root, not just
`stems`. A valid lower-precedence bundle remains usable when a higher-precedence
candidate is invalid; the failed candidate is logged with its root and reason.

`apps/vocals` writes `data/state/vocal-cache/<stable_id>.json`, read by
`rb_vendor.py:vocals_for_content` for the PreviewStrip vocal bars. `from-stems` derives
those regions from an existing stem bundle on CPU with no demucs invocation. Both `trickle`
and `from-stems` require an explicit `--dry-run` or `--live`.

### `apps/analysis_waveform` (NATIVE-06)

Own waveform analysis for audio the app decodes itself, added Tue 8 Sep 2026 as the first
lane of `specs/native-analysis-v1.md`. A top-level package rather than another module under
`apps/webui/server/rb_vendor_pkg/`, because the decode is a domain producer that the webui
CONSUMES: `rb_vendor.py` and `track_rows.py` keep re-export shims, so every pre-existing
import path still resolves and there is exactly one implementation.

| Module | What it owns |
|:-----------------------------------|:-----------------------------------------------------------|
| `decode.py` | `DecodeProfile` and `bytes on disk -> (columns, 3) uint8 peaks`. Nothing else reaches the filesystem from here. |
| `local_waveform.py` | The version-keyed cache, the `/anlz` payload shapes and the decode admission gate. Moved from `rb_vendor_pkg/`. |
| `bands.py` | Band reduction shared with the ANLZ path, so the two lanes cannot drift on preview width. Moved from `rb_vendor_pkg/waveform_bands.py`. |
| `native.py` | The optional Rust waveform backend gate. Moved from `rb_vendor_pkg/waveform_native.py`. |
| `score.py` | Per-band Pearson r against real rekordbox PWV6, with both controls gating its exit code. |

One ffmpeg process decodes a track once at 44.1 kHz into three band chains (crossovers 200 Hz
and 4 kHz, two cascaded 2-pole Butterworth sections per edge) merged by `amerge` into a single
3-channel interleaved stream, so a track is never decoded three times and PCM is never
buffered whole. A track with decodable audio and no rekordbox mapping is served
`kind: "tri"`; a failure is `not_decoded` with a stated reason and empty bands, never a
synthesized shape.

The cache under `data/state/local-waveform-cache/` is keyed by a version string built by
walking the `DecodeProfile` fields, so a field added later cannot be forgotten in the key and
a mono-era entry is rebuilt rather than reinterpreted as tri-band.

Direction of dependency, which is why the scorer lives here and its fixture BUILDER does not:
this package imports no webui - the arch gate's cycle list is `smartlists`, `stems` and
`vocals` against `webui`, and `analysis_waveform` is not on it.
`scripts/build_waveform_bundle.py` has to read a PWV6 tag
through `apps.webui.server.rb_vendor_pkg.anlz`, which would close an
`apps.analysis_waveform <-> apps.webui` package cycle the arch gate counts, so it stays in
`scripts/`.

### Backend surface

`create_app()` in `apps/webui/server/app.py`; `python -m apps.webui.server` serves
`127.0.0.1:8585`. Off-loopback binding warns and sets `X-Bind-Warning`. Backend selection is
automatic: `SqliteBackend` over `data/state/state.db` when present, else `InMemoryBackend`.

31 route modules, 77 served paths under `/api/v1`:

| Group | Routers |
|:-----------------------|:-------------------------------------------------------|
| Track / library core | `tracks`, `analysis`, `bulk_edit`, `find_replace`, `mytag`, `smartlists`, `search` |
| Playlists | `playlists`, `playlist_write`, `playlist_writeback`, `queues`, `dedup_review`, `pairings`, `play_it` |
| Rekordbox / perf data | `rb_assets`, `rb_hot_cues`, `stems`, `stem_tiers`, `progress` |
| Filesystem maintenance | `reconcile`, `relocate`, `usb_export` |
| AI / assist | `copilot`, `settings_ai` |
| Ops / meta | `health`, `settings`, `ui_prefs`, `bench`, `spotify`, `voice_probe` |

`rb_vendor.py` is the rekordbox asset path: `stable_id -> track_vendor_ids ->
data/master.plain.db` (`djmdContent`, `djmdCue`), then ANLZ parsed from disk. Three disk
caches under `data/state/`: `anlz-cache/` (schema-versioned, self-heals on bump),
`beatgrid-issue-cache`, and the vocal cache, each behind a per-path lock.

**No authentication.** The security model is loopback binding plus a CORS allowlist of known
dev ports. Errors centralise in `errors.py`: `NotFoundError` 404, `ConflictError` 409 with an
`ETag` for optimistic concurrency, `BackendError` 422. No background workers or websockets;
every request is synchronous.

### ANLZ facts that cost real time to rediscover

- Tri-band waveforms come from `.2EX` PWV6/PWV7 raw entries. Byte order is
  **byte0=low, byte1=mid, byte2=high**, scale 0..127, proven by correlating against ffmpeg
  band envelopes. An earlier mid/high/low guess was wrong.
- **Cues come from `djmdCue`, not ANLZ PCOB/PCO2**, which are empty in rekordbox 6 and 7.
- pyrekordbox's PWV6 `tag.get()` raises `KeyError: 0`; `rb_vendor.py` walks raw PMAI
  sections instead.
- Preview fallback chain is PWV6 -> PWV4 -> PWAV -> null. Normalise by per-track
  `preview_max`, never `/127` (values top out near 87).
- PVDI vocal intensity is written by rekordbox 7+ analysis. Presence tracks
  **analyzer version, not vocal content**. Probe the file,
  never a DB flag.

## The six-rail safety pattern

Every destructive write path goes through six rails. Reference: `apps/sync/safety.py`.

1. **Typed confirmation.** Operator types a non-trivial string.
2. **Running-app check.** `pgrep` for Rekordbox / djay / Serato / Traktor. A missing or
   erroring `pgrep` is a hard error, not a skip. Override is `--force-no-pgrep`.
3. **Timestamped backup** of the target. Rail 6 reads from it.
4. **Dry-run default.** Live writes need both `--live` and `--i-understand-the-risks`.
5. **Atomic write** (temp file + `os.rename`). Never truncate in place.
6. **Post-write readback + reversal script.**

Imported by `apps/sync/{playlist_apply,apply_analysis,apply_ratings,apply_cues}.py` and
`apps/smartlists/{writers,djay_writer}.py`. `apps/reconcile` and `apps/spotify` carry
equivalents. `apps/stems`, `apps/vocals` and `apps/sets` do not need it: they write only
derived artifacts under `data/`, never operator files.

## MIDI and controllers

`PARITY-TODO.md` still says "Excluded permanently: DJ controller / hardware / MIDI / PAD
interfacing (user decision)". **That line is stale.** WebMIDI core, a mapping contract,
action glue, and device maps for DDJ-FLX10, Reloop Mixtour and DDJ-400 were built Tue 21 to
Wed 22 Jul 2026, and the rig has been played live on a DDJ-400.

Current scope decision (Sat 15 Aug 2026): **in scope, secondary.** On-screen parity comes
first; MIDI re-lands after the core surface is solid. `PARITY-TODO.md` should be corrected.

## Known defects

Nine verified issues, with evidence and proposed fixes, live in
[`refactor1.md`](../refactor1.md). The two most likely to waste your time:

- **R1-09**: a daemon from the `wt-ddj400` worktree may be holding `:8585` and serving an
  empty library. The rig looks broken and is not. Check `lsof -ti :8585` before debugging.
- **R1-02**: `GET /api/v1/usb/volumes` is a 404 that the frontend calls in two places.

## Related documents

- [`docs/moc/README.md`](moc/README.md) - per-area maps of content: what an area is, read
  order, known traps, a 60-second health check. Go here once you know which area your task
  touches; this document stays the repo-wide overview.
- [`PARITY-TODO.md`](../PARITY-TODO.md) - the live board, authority on shipped vs pending
- [`.planning/rekordbox-parity/`](../.planning/rekordbox-parity/) - ground-truth spec and recon
- [`refactor1.md`](../refactor1.md) - open defects
- `.planning/REBUILD-FROM-JUL24.md` - the re-land plan. **Not on this branch**: it lives on
  `af--rebuild-from-jul24` only, because the rewind it describes has not been decided. Read it
  with `git show af--rebuild-from-jul24:.planning/REBUILD-FROM-JUL24.md`, or in the
  `music-dj-tools-wt-rebuild` worktree.
- [`.planning/FANOUT-CONVENTIONS.md`](../.planning/FANOUT-CONVENTIONS.md) - parallel work rules
- [`apps/vocals/CLAUDE.md`](../apps/vocals/CLAUDE.md) - vocal-cache contract
- [`README.md`](../README.md) - **stale**, describes the April product
