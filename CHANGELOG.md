# Changelog

All notable changes to music-dj-tools are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Milestone-level detail lives under [`.planning/milestones/`](.planning/milestones/); this file is the user-facing summary.

## [Unreleased]

## [1.0.0-rc1] - 2026-04-17

First release candidate of music-dj-tools v1. Tag target: `v1.0-rc1` on `master`. CI green with 1834 pytest tests passing.

### Milestone 1 - Reconciliation baseline

#### Added
- RECON-01 Rekordbox vs filesystem reconciliation (`apps/audit/rekordbox_vs_music.py`).
- The six-rail safety pattern that every later write path inherits: typed confirm, pgrep app abort, timestamped backup, dry-run default, atomic write, post-write readback + reversal script.
- pytest suite + `scripts/pytest_reqs_plugin.py` + `@pytest.mark.requirement` + `coverage-matrix.md` generator.

### Milestone 2 - Bi-directional sync

#### Added
- SYNC-01: djay Pro playlist + track membership enumeration (TSAF parser v2).
- SYNC-02: Rekordbox <-> djay track matcher (six-signal + chromaprint fingerprint fallback).
- SYNC-03: Bi-directional playlist sync with TSAF builder (page packer + int64-LE rowid array).
- SYNC-04: Bi-directional cue / loop / beatgrid sync (RB reader + writer; djay TSAF cue / loop parser + writer primitives).
- SYNC-05: Analysis metadata sync (BPM, key, energy, custom tags).
- SYNC-06: 0-5 star ratings sync (TSAF tail writer + direction policy + `apply_ratings` CLI + safety harness).
- `@pytest.mark.integration` marker + `--run-integration` flag so DB-touching smoke tests skip by default.

#### Changed
- Safety-rail pattern extended from reconciliation to every M2 write path.

#### Deferred
- Live ratings apply pass (write primitives covered by 14 round-trip tests; live typed-confirm apply is a manual UAT).
- djay cue blob fixture harvest (requires quitting djay and running `scripts/harvest_djay_cues.py`).
- Phase 4 O1 / O2 runbook probes documented but not executed.

### Milestone 3 - Metadata enrichment

#### Added
- INFRA-01: Shared state layer (schema + stable_id + provenance envelope + event bus + Rekordbox ingest adapter + SAVEPOINT writer + CLI).
- OPEN-01b: stable_id algorithm - ISRC, then fingerprint, then path+size+duration tiers; SHA-1 based.
- OPEN-01c: Provenance envelope (source + timestamp + confidence + actor) for every field write.
- META-01: Mixed In Key integration + analyser abstraction (librosa + madmom default backend, MIK subprocess backend). Tag-writer for ID3v2.4 / MP4 atoms / Vorbis comments with safety rails.
- META-02: Bad-beatgrid detector (cue-in-impossible-position + BPM-drift detectors).
- META-03: Chromaprint-based dedup + tag unification pipeline (scan, find_clusters, apply, unify, preview, apply).
- META-04: Auto-cue proposer (transient + downbeat + energy-peak candidates).

#### Deferred
- Full JSON Schema runtime validation (lands in Phase 15).
- Property-based tests for ISRC normalisation.
- `--archive-duplicates` flag.
- Phase 4 rating-merge wiring; Phase 6 `fetch_mik` hook stubbed.

### Milestone 4 - Catalog growth

#### Added
- SMART-01/02/03: Smart-playlist rule schema (AND/OR/NOT over genre / BPM / Camelot / rating / energy / custom tags / dates / color), materialisation engine with `[SL]` name-prefix, re-evaluation triggers + debouncer.
- CAT-01 (incl. a, b): Spotify reader + matcher adapter + state writer + acquisition markdown / CSV reports + `rematch` CLI.
- CAT-02: USB sync + verify - profile YAML, preflight, plan, apply, verify, remediate-drift, parallel hashing, ffmpeg transcode.
- CAT-03: Track-pairings schema + repo + CLI + into-direction + confidence + CHECK constraints.
- CAT-04 (incl. a, b): Cloud sync - Litestream + R2 lock + Syncthing fallback + LaunchAgent + `replicate.self_check`.
- CAT-05 (incl. a, b): Web UI - FastAPI daemon with 5 endpoint groups + OpenAPI 3.1 + SvelteKit SPA with 5 pages + Playwright e2e stubs.

#### Outstanding
- **CAT-06 (Phase 10.1)**: Pioneer / CDJ USB export (OneLibrary write + agentic RB trigger + ANLZ read). Spike + fixture + 3 prototypes landed; sibling gap-fill agents are validating HITL hardware paths.
- **SMART-04**: Web-first rule editor UI - partial scaffold in the SPA; dedicated editor deferred.

#### Deferred
- Phase 10 live Litestream + MinIO integration test (external dependency).
- Phase 10 live R2 round-trip (no Doppler token in sweep workspace).
- Phase 11 SSE / WebSocket event bus (30 s client poll ships for v1).
- Phase 11 shared-secret auth escape hatch (Tailscale / Cloudflare Tunnel recommended).
- Phase 11 Tailwind v4, Svelte unit tests, cmd-K quick-open deferred to v2.

### Milestone 5 - Sets and AI

#### Added
- SET-01: Set recorder - MP3 + structured timeline (deck state, transitions, FX); djay + Rekordbox HISTORY sources; watermark + retention.
- SET-02: Transition classification - rules + trained classifier + label CLI.
- SET-03: Set replay - sessions API + audio API + replay engine + FastAPI router.
- PLAY-01: Multiple named play-orders per playlist (open-dj serde + repo + `play_orders` table).
- PLAY-02: `PLAY IT` solver - beam-search over BPM / Camelot / energy curve with `InsufficientDataError`.
- PLAY-03: Per-entry overrides - target key (key-sync transpose) + target tempo (BPM shift) + transition hints.
- AI-01: Next-track suggester stack + CLI + event doc.
- AI-02: Crowd-energy-aware suggestions (`apps/dj_copilot/`).
- VOICE-01: Wake-word + WebRTC VAD + Whisper STT + grammar intent + action bus + read-only + destructive-with-confirm intents.
- `StateBackedBus` + `JsonlStubBus` + `InMemoryBus` with `make_bus()` fallback.

#### Changed
- Destructive voice intents gated behind `--enable-destructive` (default false; regression-tested).

#### Deferred
- Phase 5 events shim wire-up once `apps.shared.state.events.publish` lands.
- `scripts/sets/retention-run` cron / launchd story.
- Beam-search numpy / C-extension rewrite (current perf 600-700 ms on Python 3.14).
- Real recorded-WAV Whisper integration test.
- Wake-word mute-during-confirm assertion; push-to-talk MIDI pedal; LLM fallback for grammar misses.

### Milestone 6 - Open standard

#### Added
- OPEN-01: open-dj metadata format v0.2 - spec + JSON Schema + reference parser (`apps/open_dj/{id,canon,validate,diff,cli,schema_loader}`) built on the Phase 5 stable_id + provenance envelope.
- OPEN-02a / OPEN-02b: Rekordbox + djay open-dj adapters (read + write) with a 10-track conformance corpus.
- OPEN-02c: Serato adapter (clean-room from triseratops docs; tagstream + GEOB + database V2).
- OPEN-02d: Traktor adapter (NML read / write; byte-stable on JCS, verified by `test_jcs_stable_across_two_writes`).
- OPEN-03: Published spec + reference impl - `open-dj/spec/v0.2/`, README + CHANGELOG + LICENSE-SPEC; conformance corpus at `tests/fixtures/conformance/`; `@pytest.mark.conformance` harness.

#### Deferred
- YAML support for open-dj (deferred to 0.7).
- open-dj cue write (read-only at v0.2 / 0.3).
- MkDocs + Material scaffold, `.github/workflows/docs.yml`, `scripts/build-spec-site.py`.
- `scripts/conformance-report.py` until `open-dj-tool conformance` CLI lands.

### Milestone 7 - Launcher

#### Added
- LAUNCH-01: Tauri + React cmd-K launcher - `Alt+Space` primary + `Ctrl+Cmd+Space` fallback hotkey, blur-autohide, cmdk + match-sorter palette, FTS5 search backend + frecency + pytest bootstrap.
- LAUNCH-02a: djay drag spike - PROCEED path confirmed via pure-Rust `validate_drag_path` + Dispatcher routing + djay shim.
- LAUNCH-02b: Rekordbox XML adapter + dup detect + 12 adapter tests.
- LAUNCH-02c: Serato adapter - bundle IDs + primary drag + clipboard fallback + 5 adapter tests.
- LAUNCH-02d: Traktor NML adapter + 10 adapter tests.
- Six-rail safety pattern extended to the XML + NML write paths in Phase 18.
- drag-core Rust crate kept separate from the Tauri shell so adapter tests stay hermetic.

#### Deferred
- LAUNCH-03 (deck driver installer / updater / verifier) to v2.
- Manual djay drag HITL checklist (`apps/launcher/spikes/djay-drag/FINDINGS.md`).
- `pnpm install` + `cargo build` on the sweep workspace (network-gated); actuation on first local run.
- Latency P95 verification (requires cargo + rusqlite build).
- Real branded icons.

### Credits

music-dj-tools is only possible because of the open reverse-engineering and open-source work surveyed in [`docs/prior-art-and-communities.md`](docs/prior-art-and-communities.md). Particular thanks to:

- pyrekordbox (Dylan Jones) for Rekordbox 6 / 7 master.db access.
- rekordcrate, crate-digger, dysentery, beat-link, and djl-analysis.deepsymmetry.org (Holzhaus and Deep-Symmetry / James Elliott) for pdb + ANLZ + ProDJ Link format documentation.
- Mixxx for being the gold-standard open DJ app and an ongoing bugs-as-oracle reference.
- serato-tags + triseratops (Holzhaus) for the Serato GEOB + crate spec.
- traktor-nml-utils for NML parsing patterns.
- libdjinterop (xsco) for Engine DJ format coverage.
- beets, mutagen, mediafile (beetbox) for the music-library-management prior art and tag I/O.
- chromaprint / AcoustID, madmom, librosa, Essentia, libKeyFinder for MIR + fingerprint building blocks.
