# music-dj-tools v1.0-rc1

*Release candidate, 2026-04-17.*

music-dj-tools v1 is the first tagged release of a bi-directional sync + enrichment + authoring toolchain for working DJ libraries on macOS. It reads and writes Rekordbox, djay Pro, Serato, and Traktor at the file level, ships an open cross-format metadata layer (`open-dj` v0.2), and adds analysis, dedup, smart-playlists, set recording, voice control, cloud sync, and a Tauri cmd-K launcher on top.

This is the rc1 candidate. CI is green on 1834 tests. Expect rc2 once the Phase 10.1 Pioneer / CDJ USB export path finishes HITL hardware validation.

## What ships in v1

**Sync**
- Bi-directional Rekordbox <-> djay Pro playlist, cue, loop, beatgrid, analysis-metadata, and 0-5 star rating sync.
- Chromaprint fingerprint fallback matcher on top of a six-signal matcher.
- Every write path gated by the six-rail safety pattern.

**Enrichment**
- Phase 5 shared state layer with stable_id identity (ISRC -> fingerprint -> path+size+duration), provenance envelopes on every field, JCS-canonical JSON, SAVEPOINT writer.
- librosa + madmom default analysis backend and a Mixed In Key subprocess backend.
- Chromaprint-based dedup + tag unification pipeline.
- Auto-cue proposer (transient + downbeat + energy-peak candidates).
- Bad-beatgrid detector.

**Catalog growth**
- Smart-playlist rule engine (AND / OR / NOT over genre, BPM, Camelot, rating, energy, tags, dates, color tags) with `[SL]` name-prefix namespacing.
- Spotify importer (reader + matcher + state writer + acquisition markdown + CSV reports + rematch CLI).
- USB sync + verify + remediate-drift (profile YAML, preflight, plan, apply, parallel hashing, ffmpeg transcode).
- Cloud sync (Litestream + R2 lock + Syncthing fallback + LaunchAgent + self-check).
- FastAPI daemon with 5 endpoint groups + OpenAPI 3.1 + SvelteKit SPA with 5 pages.

**Sets and AI**
- Set recorder (MP3 + structured deck-state / transition / FX timeline) with djay and Rekordbox HISTORY sources.
- Transition classifier (rules + trained model + label CLI).
- Set replay API + audio streaming.
- Multiple named play-orders per playlist + `PLAY IT` beam-search solver over BPM / Camelot / energy curves, with per-entry key and tempo overrides.
- Next-track suggester stack (BPM + key + crowd-energy aware).
- Voice control: wake-word + WebRTC VAD + Whisper STT + grammar intent + action bus, with destructive intents gated behind an explicit flag.

**Open standard (open-dj v0.2)**
- JSON Schema + reference parser + canonicaliser + diff + CLI.
- Read / write adapters for Rekordbox, djay, Serato, and Traktor with a shared conformance corpus.
- 10-track fixture roundtrip harness.
- Spec published at `open-dj/spec/v0.2/` under CC BY 4.0.

**Launcher (Hyper-K)**
- Tauri + React cmd-K launcher with `Alt+Space` hotkey and FTS5 search backend.
- Drag-drop adapters for djay, Rekordbox, Serato, and Traktor via a hermetic drag-core Rust crate.
- Six-rail safety pattern extended to XML + NML write paths.

## Safety

Every write in this release follows the same six rails: typed confirmation, `pgrep` abort if the target app is running, timestamped backup, dry-run default, atomic write (temp + rename), and post-write readback with an emitted reversal script. See `apps/sync/safety.py` and any phase UAT under `.planning/phases/*/` for the exact invocations.

## What is not in v1

Known deferrals that will land in rc2 or v1.1:

- **CAT-06 (Phase 10.1)**: Pioneer / CDJ USB export (OneLibrary write + agentic Rekordbox trigger + ANLZ read). Spike + fixture + 3 prototypes landed; HITL hardware validation is the current blocker.
- **LAUNCH-03**: DJ deck driver installer / updater / verifier. Deferred to v2.
- **SMART-04**: Dedicated web-first smart-playlist rule editor UI (partial scaffold lives in the SvelteKit SPA).
- Phase 10 live Litestream + MinIO and live R2 round-trip integration tests.
- Phase 11 SSE / WebSocket event bus (30 s poll ships for v1) and shared-secret auth escape hatch.
- Tailwind v4 migration, Svelte unit tests, cmd-K quick-open.
- open-dj YAML support (0.7) and cue write (0.3+).
- MkDocs + Material docs site and `.github/workflows/docs.yml`.
- Set-retention cron / launchd job; real recorded-WAV Whisper integration test; push-to-talk MIDI pedal; LLM fallback for grammar misses.

Full milestone-level status: [`.planning/milestones/M2-complete.md`](.planning/milestones/M2-complete.md) through [`M7-complete.md`](.planning/milestones/M7-complete.md). Full change detail: [`CHANGELOG.md`](CHANGELOG.md). Ship-level writeup: [`.planning/V1-SHIP-SUMMARY.md`](.planning/V1-SHIP-SUMMARY.md) (landed by the v1 ship agent).

## Install

Python 3.11 or newer:

```bash
git clone https://github.com/former-work-account/music-dj-tools.git
cd music-dj-tools
python3 -m venv .venv
source .venv/bin/activate
pip install --no-build-isolation -r requirements.txt
python -m pyrekordbox download-key
make test
```

Optional: `ffmpeg` for transcode, `fpcalc` (chromaprint) for dedup, `doppler` for Spotify + cloud secrets.

See [`README.md`](README.md) for the architecture tour and [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup and DCO sign-off.

## Credits

See [`docs/prior-art-and-communities.md`](docs/prior-art-and-communities.md) for the full survey. In brief: this project stands on pyrekordbox (Dylan Jones), rekordcrate / serato-tags / triseratops (Holzhaus), crate-digger / dysentery / beat-link (Deep-Symmetry / James Elliott), Mixxx, traktor-nml-utils, libdjinterop, beets, mutagen, chromaprint / AcoustID, madmom, librosa, Essentia, and libKeyFinder. Thank you.

## License

Apache 2.0 (code); CC BY 4.0 (open-dj spec). See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE), and [`open-dj/LICENSE-SPEC.md`](open-dj/LICENSE-SPEC.md).
