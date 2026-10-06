# Changelog

All notable changes to music-dj-tools are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Milestone-level detail lives under [`.planning/milestones/`](.planning/milestones/); this file is the user-facing summary.

## [Unreleased]

## [app-v1.0.0-alpha.1] - Open DJ 1.0 alpha (unreleased)

The first public release of the Open DJ desktop app, named **Open DJ 1.0 alpha** and tagged
`app-v1.0.0-alpha.1`. The release lane replaces "(unreleased)" with the date when it creates the
tag on the exact commit that built the dmg.

App releases use their own `app-v<semver>` tags (ADR "Launch release name 'Open DJ 1.0 alpha'
and an app tag scheme of its own", `docs/decisions/`). The `1.0.x` entries below this one, and
the `v1.0*` tags, are the April 2026 library toolchain, not app releases. The `v1.1.1` tag was
an internal test build and was never public.

### What it is

- A local-first DJ app for macOS: a 4-deck performance screen running on a Web Audio engine
  over your own library. Your library stays on your Mac.
- Alpha software. Features, screens and stored data formats can change between releases, and
  some controls are not finished yet. Keep a backup of your DJ library before letting Open DJ
  write to it. The alpha stage is shown next to the wordmark in the app (OSSPUB-04).
- Licensed Apache-2.0. Copyright The Open DJ contributors.

### Added

- Signed and notarized macOS dmg (OPS-14) for Apple Silicon Macs only (arm64; INSTALL-05),
  macOS 14 or newer. Intel Macs are not supported in this release.
- First-run setup that imports an existing rekordbox library (tracks, playlists, BPM, key,
  waveforms, beatgrids and cue points) without writing to rekordbox, or opens with a folder of
  audio files or an empty library.
- In-app updater (OPS-15). Each later alpha is offered to installed copies of this one.
- Each build shows its own identity (git SHA, build time) in the app and at
  `GET /api/v1/build-info` (INSTALL-07), so a bug report can name the exact build.

### Known issues

- **Stems:** <!-- STEMS-KNOWN-ISSUE: placeholder. Replace with the stems Known-issue line that
  the Preview & Pins stems worker posts on issue #5638 (orders board, item W4). --> _Pending:
  the stems memory note is still being written._
- **Browser output device:** in the browser web UI without microphone permission, choosing the
  default output device can fail with "device default is not found" and show an error toast
  (board item W8; a fix is in progress). The desktop app is not affected.
- **Unfinished controls:** a control with no real data source behind it is visible but inert,
  and its tooltip says "not implemented - see PARITY-TODO". This is deliberate: Open DJ never
  shows invented data.
- **Apple Silicon only:** the dmg does not open on Intel Macs.

## [1.0.1] - 2026-04-17

Patch release capturing post-v1.0 safety rails, robustness fixes, refactors,
hygiene, and documentation. ~30 PRs merged since v1.0 (tag `ecbd31d`). See
[`RELEASE-NOTES-v1.0.1.md`](RELEASE-NOTES-v1.0.1.md) for the full ledger.

### Added
- P1-A `apply_plan` self-guard against in-place writes (#110).
- P1-B `DjayPlaylistWriter` self-guard (#111).
- P2 djay-db write-path contract enforcement (#116).
- TSAF constant startup validation (#124).
- `.env.sample` documentation (#124).
- `MILESTONES.md` release ledger (#119).
- `CODEBASE-MAP-v1` full module tree audit (#126).
- 76 new regression tests (2251 → 2327 passing).

### Changed
- Voice daemon degrades gracefully on missing `sounddevice` (rc=0) (#113).
- `matcher.py` decomposed into focused modules (#120).
- `scratch/` and `demo/` relocated out of `apps/` (#109).
- `pyproject.toml` dependency cleanup (#123).
- `.mailmap` unified to a single canonical identity (#107, #118).
- Sleep-based tests converted to event-wait patterns (#115).
- Frozen datetime + 5 additional MEDIUM/LOW diagnose cleanups (#125).

### Fixed
- LIC-1 license-isolation documentation gap closed (#124).

### Documentation
- Health audit (#106), forensics audit (#105), progress report (#114),
  diagnose audit (#121), codebase-map (#126).

## [1.0.0] - 2026-04-17

Stable v1.0 release. Rolls up the rc3 line plus 39 commits of final adversarial
remediation: 17 codex / GPT-5.4 phase-by-phase fixes (phases 02–18), the
launcher drag-core fan-out v2 sweep, the rc3-deferred resource-leak fixes
(#23, #25), three security red-team fixes (#59), and a license isolation that
moves GPL `mutagen` to an opt-in extra (#60). See
[`RELEASE-NOTES-v1.0.md`](RELEASE-NOTES-v1.0.md) for the full ledger.

Metrics (authoritative; measured on `master` at v1.0 cut):

- Tests: 2251 passing, 45 skipped, 0 failing on `make test`.
- Requirements: 53 v1 requirements, **51 shipped**, 2 deferred to v1.1
  (`LAUNCH-03`, `SMART-04`). Source: [`reqs.json`](reqs.json).
- Phases: 1–18 all delivered.

### Added
- `RELEASE-NOTES-v1.0.md` ship-state document.

### Changed
- Version bumped from `1.0.0rc3` to `1.0.0`.
- `reqs.json` regenerated against `.planning/REQUIREMENTS.md`.
- `mutagen` moved from runtime deps to optional `[tags]` extra (GPL vs Apache).

### Fixed
- **Codex / GPT-5.4 adversarial sweep (phases 02–18):** RB matcher candidate
  scoring (#74), playlist apply verify-in-tx + pgrep-fail-safe (#81), state
  writer rollback on bus publish failure (#75), Spotify `--max-tracks` safe
  default (#77), `apps.sets` stop finalizes recorder + manifest (#76),
  `open-dj-tool export` routed through v0.2 wire serializer (#79).
- **Serato adapter (R4 findings):** preserve subcrate playlist membership on
  read (#65), preserve cues+loops when `__raw__` GEOB present (#62), sanitize
  playlist name before subcrate write (#67).
- **Other R4 findings:** voice/bus commit-and-isolate (#63), pairings/add
  conn close on any exception (#64), USB verify HashCache write
  serialization (#66), webui sqlite_backend update_track serialization (#68),
  cloud/lock FakeS3Client `If-Match '*'` wildcard semantics (#69).
- **Launcher drag-core fan-out v2:** mtime re-check before sidecar rename
  (#56), sidecar fsync error propagation (#55), percent-encode rekordbox file
  URL (#57), reject relative sidecar dirs in default_set (#53), `stable_id`
  derivation for streaming tracks (#54), preserve `BPM=0` as `0.0` (#52).
- **Resource leaks rolled forward from rc3:** HashCache conn close on
  `_ensure_schema` failure (#23), capture stderr_log fd close on Popen
  failure (#25).
- **Security red team:** one HIGH + two MEDIUM findings (#59).

### Audits + governance
- M7 round-2 re-verification (#40), M2–M4 post-merge re-audit (#47),
  M5+M6 re-audit (#49), check-plan round-2 sweep across 18 phases / 19 PRs
  (#73), validate-work round-2 Nyquist coverage sweep (#72), fan-out v2
  triage with 3/3 + 2/3 consensus (#51), merge-watch round-2 report (#80),
  e2e re-run of reconcile / sync-cues / open-dj-roundtrip (#46), 17-file
  codex-pro triage (#78), GPT-5.4 Mux routing verification (#70), Sourcery
  web-scanner integration design (#61), planning-doc secret/identifier
  redaction (#92), codex CLI ChatGPT Pro OAuth doc (#58).

## [1.0.0-rc2] - 2026-04-17

Follow-up release candidate on top of `v1.0-rc1`. Fixes the wheel / sdist build, finishes Phase 16 (Serato GEOB cue write), lands the Phase 15 / 16 CLI surface, the docs site, and a governance + anti-slop sweep. Covers Wave 5 through Wave 7 follow-up work and the Wave 8 release-asset fixes.

Metrics (authoritative; measured on `master` at commit `ddc5ebc` via `pytest --collect-only -q`):

- Tests: 2238 collected.
- Requirements: 53 v1 requirements, 50 shipped, 3 pending (CAT-06, SMART-04, LAUNCH-03). Source: [`reqs.json`](reqs.json).

### Added
- OPEN-01 + OPEN-03: `open-dj-tool` CLI gains `export`, `import`, and `conformance` subcommands (Phase 15).
- OPEN-03b: MkDocs site scaffold plus `docs.yml` GitHub Actions workflow for Pages publishing (Phase 16).
- USB / Pioneer sync: promoted the ad-hoc diff script to a typed `differ` module with a public API.
- USB / Pioneer sync: `diff-matrix` CLI and parametrized pytest harness that runs the differ across N fixtures.
- Appendix B conformance: authored the remaining 12 conformance fixtures (04-15).
- Phase 16 Serato: GEOB cue write plumbed through `SeratoAdapter.write()` via mutagen (P0).

### Changed
- `pyproject.toml`: added `[build-system]` + `[tool.setuptools.packages.find]` so `python -m build` succeeds. Wheel ships `apps/` only; `data/`, `tests/`, `scripts/`, `docs/`, `open-dj/`, `.planning/`, `usb-profiles/`, and `htmlcov/` are excluded. License migrated to the SPDX expression form (`license = "Apache-2.0"` + `license-files = ["LICENSE"]`); the redundant `License :: OSI Approved :: Apache Software License` classifier was dropped. rc1 could not produce a wheel or sdist because the `[build-system]` table was missing and flat-layout discovery collided with the top-level `data/` directory.
- Governance pass for the v1 release: audited and enhanced `SECURITY.md`, the Code of Conduct, `CONTRIBUTING.md`, and `NOTICE`.
- Anti-slop audit: applied 7 targeted fixes plus a systemic sweep across generated docs.

### Fixed
- Phase 4 `apply_ratings --live` now routes to the LIVE database paths instead of the staging copy (resolves #1).
- Phase 16 MkDocs strict link validation relaxed for cross-repo references so the docs build stays green (OPEN-03b).
- Phase 16 Serato: removed the cues-drop mask from the `03-8-hot-cues` fixture now that GEOB write works.
- CI: Pages deploy step is now best-effort until repository Pages is enabled, so the workflow no longer fails the pipeline.
- Tests: USB diff-matrix tests now skip cleanly when the `rbox` package is unavailable.

### Documentation
- Phase 10.1 Plan 01 rewritten as a delivery plan now that the spike has landed.
- Phase 10.1 CAT-06 closeout: VERIFICATION, UAT, and VALIDATION docs.
- Captured the SQLite WAL sidecar bug surfaced by the USB writer round-trip in `solutions/`.
- Published diff-matrix results across all available USB export fixtures.
- Forensics pass 2 on project health after Wave 5.
- Anti-slop review of generated content and a re-audit of Wave 5 fix-code-review claims.
- Closed the export / import / conformance CLI deferrals (Phases 15, 16) and the OPEN-03b MkDocs deferral in their SUMMARY docs.
- Swept pending todos post Wave 5 fanout.
- Stash audit + prune post Wave 6.

## [1.0.0-rc1] - 2026-04-17

First release candidate of music-dj-tools v1. Tag target: `v1.0-rc1` on `master`. CI green at the rc1 tag. The authoritative, live pytest count is published in the rc2 entry below and in `README.md`; earlier historical counts for rc1 (1834) are superseded by the rc2 measurement to avoid drift between docs.

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
