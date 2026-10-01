# Roadmap: music-dj-tools

## Overview

Monorepo DJ library toolkit in progressive milestones:

1. **Library Hygiene** ✅ complete - Phase 1 + 1.1. Broken count → 0.
2. **Bi-directional Sync** (current) - RB ↔ djay sync: tracks, playlists, cues, beatgrids, ratings.
3. **Metadata Enrichment** - analysis (librosa+madmom / MIK), beatgrid, dedup, tag-unification, auto cues.
4. **Catalog Growth** - smartlists, pairings, Spotify import, USB sync, cloud+web UI.
5. **Sets & AI** - set recording, classification, replay, play-orders, PLAY IT, voice commands.
6. **Open Standard (open-dj)** - spec + RB/djay/Serato/Traktor adapters + conformance + publish.
7. **cmd-K launcher** - Tauri cmd-K + drag-drop to all four DJ apps.

Granularity: **coarse-to-standard** - 2-4 phases per milestone.

## Milestones

- [x] **Milestone 1: Library Hygiene** - complete 2026-04-17
- [x] **Milestone 2: Bi-directional Sync** - complete 2026-04-17 (Phases 2, 3, 4)
- [x] **Milestone 3: Metadata Enrichment** - complete 2026-04-17 (Phases 5, 6, 7)
- [x] **Milestone 4: Catalog Growth** - complete 2026-04-17 (Phases 8, 9, 10, 11; Phase 10.1 gap-fill)
- [x] **Milestone 5: Sets & AI** - complete 2026-04-17 (Phases 12, 13, 14)
- [x] **Milestone 6: Open Standard** - complete 2026-04-17 (Phases 15, 16)
- [x] **Milestone 7: Launcher** - complete 2026-04-17 (Phases 17, 18)

## Cross-cutting decisions

- **C1** Shared state layer (SQLite + Python event bus + Litestream replication).
- **C2** Cloud provider: Cloudflare R2 primary; Syncthing fallback.
- **C3** Web UI: SvelteKit SPA + FastAPI daemon shared with the launcher.
- **C4** Voice + AI: local Whisper + openWakeWord + grammar-based intent; sub-800ms budget.
- **C5** open-dj: read-first, ratify existing; JSON Schema; stable_id + provenance envelope.
- **C6** Launcher: split into M7a (cmd-K), M7b (drag-drop), M7c (driver manager - deferred).
- **C7** Analysis libraries: librosa (ISC) + beat-this (MIT) primary; MIK optional CLI; Essentia (AGPL) subprocess-only. madmom's code is BSD-3-Clause but its pretrained models are CC BY-NC-SA 4.0 (non-commercial), so it is never on the shipped path -- bench/reference only, gated behind `MDT_BENCH_NONSHIPPABLE=1` (NATIVE-08).
- **C8** Serato adapter: clean-room from triseratops (MPL-2) reference.
- **C9** License-contagion: embed MIT/Apache/ISC/BSD; file-level MPL-2; subprocess-only for GPL/AGPL.

## Phases

### Milestone 1: Library Hygiene ✅

- [x] **Phase 1**: Reconcile broken Rekordbox links. RECON-01..04.
- [x] **Phase 1.1** (INSERTED): Remove a stale duplicate. RECON-05. Broken count → 0.

### Milestone 2: Bi-directional Sync ✅

- [x] **Phase 2**: djay reader extensions + RB↔djay matcher. SYNC-01, SYNC-02, SYNC-06 (partial).
  - shipped 2026-04-17
- [x] **Phase 3**: Bi-directional playlist sync. SYNC-03.
  - shipped 2026-04-17
- [x] **Phase 4**: Cue / beatgrid / analysis / ratings sync. SYNC-04, SYNC-05, SYNC-06.
  - shipped 2026-04-17

### Milestone 3: Metadata Enrichment ✅

- [x] **Phase 5**: Shared state layer with stable_id + provenance envelope. INFRA-01, INFRA-02, INFRA-03, OPEN-01b, OPEN-01c.
  - shipped 2026-04-17
- [x] **Phase 6**: Analysis pipeline - librosa + madmom + MIK integration + beatgrid detection + auto-cue generation. META-01, META-02, META-04.
  - shipped 2026-04-17
- [x] **Phase 7**: Dedup via chromaprint + tag unification + write-tags-to-file. META-03, META-01 extension.
  - shipped 2026-04-17

### Milestone 4: Catalog Growth ✅

- [x] **Phase 8**: Smart playlists + pairing memory. SMART-01, SMART-02, SMART-03, CAT-03 (SMART-04 web rule editor deferred).
  - shipped 2026-04-17
- [x] **Phase 9**: Spotify playlist importer + acquisition queue. CAT-01 (incl. CAT-01a, CAT-01b).
  - shipped 2026-04-17
- [x] **Phase 10**: USB sync + verify. CAT-02.
  - shipped 2026-04-17
- [ ] **Phase 10.1** (INSERTED - gap-fill in progress): Pioneer/CDJ USB export. CAT-06. Validating the OneLibrary writer.
- [x] **Phase 11**: Cloud sync (Litestream → R2 / Syncthing) + web UI v1 (SvelteKit + FastAPI). CAT-04 (04a/04b), CAT-05 (05a/05b).
  - shipped 2026-04-17

### Milestone 5: Sets & AI ✅

- [x] **Phase 12**: Set recording + timeline + transition classification + replay UI. SET-01, SET-02, SET-03.
  - shipped 2026-04-17
- [x] **Phase 13**: Play-orders + PLAY IT solver + AI next-track suggester. PLAY-01, PLAY-02, PLAY-03, AI-01, AI-02.
  - shipped 2026-04-17
- [x] **Phase 14**: Voice commands - wake-word + Whisper + grammar intent. VOICE-01.
  - shipped 2026-04-17 (commits landed with Phase 14 fan-out)

### Milestone 6: Open Standard ✅

- [x] **Phase 15**: open-dj spec v0 + Rekordbox + djay adapters + reference parser. OPEN-01, OPEN-02a, OPEN-02b, OPEN-03 (partial).
  - shipped 2026-04-17
- [x] **Phase 16**: Serato + Traktor adapters + conformance corpus + publish spec. OPEN-02c, OPEN-02d, OPEN-03b (partial), OPEN-03c.
  - shipped 2026-04-17

### Milestone 7: Launcher ✅

- [x] **Phase 17**: cmd-K launcher v1 - Tauri cmd-K window + drag-drop to djay. LAUNCH-01.
  - shipped 2026-04-17
- [x] **Phase 18**: Drag-drop to Rekordbox / Serato / Traktor. LAUNCH-02b, LAUNCH-02c, LAUNCH-02d (LAUNCH-03 driver manager deferred to v2).
  - shipped 2026-04-17

---

## Future (unscheduled)

Items captured in the backlog but not yet routed to a phase.

**Priority override, 14 September 2026:** finish the release-testing infrastructure
for today's v1 before additional research. New T3/mobile, v3 cloud/QR/NFC guest
login, CoBeat extensions, hardware compatibility,10ms lyrics/VJ, v5 automated
OneLibrary research and microphone-mesh sound-engineering tasks are captured in
the deferred intake. These are
pending follow-ups, not implementation or verified vendor capabilities.

### v1 immediate

- [ ] **Website and README positioning**: ship the small public product surface, real screenshots/install path, current limitations, open-source position, and PRICE-06 backer/partner message. Target: Mon 14 Sep 2026.

### v1.1

- [ ] **Electron desktop shell cutover** (ELECTRON-08, ELECTRON-09; moved from v1
  Thu 1 Oct 2026): v1 ships on the signed Tauri DMG. The Electron shell
  finishes the updater and the cutover itself (signed,
  notarized DMG, `apps/desktop/src-tauri/` removed) as the first v1.1 item.
  ELECTRON-01 through ELECTRON-07 already shipped on the Tauri-plus-Electron-beside-it
  build and stay in v1.

### v2 candidates

- [ ] **Phase 19 (v2)**: Sync integrity gate. SYNC-07.
  - **Goal**: Wire the library-integrity guard into the sync apply write-path so a library with broken/missing track locations hard-fails instead of silently syncing.
  - **Mode**: mvp
  - **Success Criteria**:
    1. `apps/sync/playlist_apply.py` (and `apps/reconcile/apply.py`) call `apps.shared.library_integrity.assert_healthy()` before committing; abort when `broken_ratio` exceeds threshold unless an explicit `--allow-broken` flag is passed (mirrors the existing safety-gate pattern: backup, typed confirm, RB-running check).
    2. A regression test asserts the apply path aborts on a high-broken-ratio library.
    3. `make integrity` (i.e. `python -m apps.audit.library_integrity --strict`) wired as a pre-sync / CI gate.
  - **Depends on**: the library integrity guard, MUSIC_ROOTS, and the Length-seconds fix.
  - **Context**: surfaced 2026-06-04, when a home-folder migration left most tracks with broken locations that the sync path would have shipped silently; this gate prevents recurrence.
- [ ] **LAUNCH-03** (deferred from Phase 18): DJ deck driver manager (install/update/verify).
- [ ] **Booth output (v2)**: third audio output for DJ-booth monitor speakers.
  - **Goal**: Add a BOOTH output device slot beside MASTER / MAIN and HEADPHONE CUE, carrying the master mix at an independent booth level, pinned like master, with liveness detection and HTTP/IPC parity.
  - **Success Criteria**: independent booth level, sinks never steal each other, mapped BOOTH LEVEL knobs, loud failure on booth device death, agent endpoints.
  - **Context**: raised Mon 14 Sep 2026 during v1 testing; confirmed absent from `mixer-types.ts` and REQUIREMENTS.md. Not a v1 blocker.
- [ ] **Phase 20 (v2)**: Semantic segments, explainable transition lab, and DJ coaching.
  - **Goal**: Turn existing beatgrid, analysis, pairing-memory, set-timeline, and production-DSP work into an open, modder-friendly path from semantic track sections to hotcue templates, directional transition opportunities, human evaluation, and evidence-backed coaching.
  - **Mode**: staged experiment before learned product claims.
  - **Success Criteria**:
    1. A versioned segment timeline records phrase-aligned boundaries, functional labels, alternatives, confidence, and analyzer provenance. The shipped equal-bin `META-04` proposer remains the deterministic baseline; optional learned detectors run offline/out of process and report `UNAVAILABLE` honestly.
    2. User/modder cue-template packs compile semantic anchors into beat-relative cue proposals with names, colors, roles, slots, device limits, and fallback priorities. Manual anchors bypass failed detection; existing manual/vendor cues are never overwritten without an explicit scoped choice.
    3. Directional transition opportunities identify source/target segments, intent, render plan, factor evidence, vetoes, and uncertainty. A blind pairwise benchmark must beat the current deterministic baseline before learned ranking becomes a default.
    4. The beginner coach and agent-chat sidebar consume the same read-only evidence and typed suggestion contract; neither surface may invent reasons, mutate transport, or overwrite cues without the existing scoped confirmation path.
    5. Modder interfaces cover segment detectors, cue strategies/template packs, transition rankers, renderers, and consented feedback adapters through versioned CLI/HTTP schemas, with declared compute/network/licence requirements.
    6. The research report includes explicit adopt/experiment/set-aside/discard decisions, dataset provenance/licence constraints, genre/intent ablations, and a public-safe dataset card before community collection.
  - **Depends on / reuses**: `META-04`, CAT-03/PAIR evidence, SET-02 timelines, AI-01/AI-02 suggestion plumbing, production DSP rendering.
  - **Research**: `docs/research/dj-compatibility-sota-20260913.md`.
  - **Lexicon parity build order**: manual anchors and beat-relative template previews first; non-destructive cue review/device-capacity plans next; genre-aware anchor, mix-out and loop experiments after the existing baseline gates.
  - **v1 boundary**: existing manual cue editing, preservation and truthful errors are usability requirements. New templates, learned anchors, automated loops and provider features remain optional v2; building a preview now does not make it a v1 release gate.
  - **Lv1 / now (within v2)**: deterministic proposal-only templates and explicit human review; seed VibeMeter grouped quick tags and short suggestion prompts using the versioned taxonomy; collect opt-in pairwise preference observations without a public skill/hotness claim.
  - **Lv2 / next**: segment-aware review and directional opportunity cards; negotiate user taxonomy; evaluate practical mashup rules, stem-role collisions and recognisable vocals over heavier melodic/psychedelic techno with consented real renders.
  - **Lv3 / later**: calibrated personal/cohort ranking and optional trending/hotness ratings. Keep preference, popularity and DJ execution quality separate; any ELO-style trial needs exposure/order controls, uncertainty, consent and a listener/persona scope. No global popularity score masquerading as musical truth.
  - **Visual reference**: `docs/research/lexicon-parity-build-plan-20260914.md` indexes official UI images and distinguishes vendor references from captured acceptance evidence.
- [ ] **Versioned taxonomy, descriptions, and segment-aware search**: vendor-neutral claims, controlled/open vocabularies, audio-reference and free-text retrieval, and a locked supplier comparison.
- [ ] **Track families and quick audition**: originals, edits, remixes, instrumentals, acapellas and stems grouped with a compact section/energy-shape view.

### v3 candidates

- [ ] **10k-track library performance (LIBM-120)**: a re-measure
  (Thu 1 Oct 2026) found the 10k-track full-listing
  and playlist-detail paths well over the 1 s acceptable budget (L2 6.3-6.9 s,
  L4 16-18 s, L12 3.9-4.5 s), L8 readiness stalling, and six user-clock rows
  UNKNOWN for lack of a render harness.
- [ ] **User-owned music-intelligence plugins**: official API/MCP integrations and terms-compliant browser helpers, with visible quota/provenance and no dependency in the open core.
- [ ] **Cyanite partnership discovery**: confirm trial API capacity, limits, data terms and taxonomy contract, then assess an optional disclosed affiliate/reseller route.
- [ ] **Periodic headphone/speaker drift re-alignment**: quietly re-measure the cue/master offset every few minutes and nudge it inaudibly, so two independently clocked outputs stay within 10 ms over a full set.

### Cross-cutting: adversarial testing, scenarios, and evals

- [ ] **Eval harness for AI-driven features**: PLAY IT solver (PLAY-02), AI next-track
  suggester (AI-01/AI-02), and voice-command grammar (VOICE-01) all make judgment calls
  today with no scored regression suite - a solver or model change could silently get
  worse and nothing would catch it.
  - **Scope**: (1) a fixed reference-track-list + rated "good transition" corpus for PLAY
    IT and the suggester, scored the way the stems -dB quality signal was calibrated
    against a listener's taste; (2) an adversarial phrasing corpus for voice-command intent
    parsing (ambiguous, noisy, accented, contradictory commands) with a measured
    false-accept/false-reject rate; (3) once crowd requests (v3) ships, adversarial
    scenarios for its profanity filter and per-phone cooldown (unicode homoglyph bypass,
    rate-limit evasion, request-flooding).
  - **Why**: every one of these features fails silently today - a bad suggestion, a
    misheard voice command, or a filter bypass reads as normal operation, not a bug. This
    is the same "blinded eval rerun" discipline already standard for skills/prompts,
    extended to the shipped AI surfaces.
  - **Mode**: spike a scorer + reference corpus for one surface first - PLAY IT has the
    clearest good/bad signal - before generalizing to the suggester and voice grammar.
  - **Depends on**: nothing blocking. PLAY-01/02/03, AI-01/02, VOICE-01 already shipped
    (Milestone 5), so this can start any time; the crowd-requests scenarios wait on v3.

### v3 candidates

- [ ] **Crowd requests (CoBeat-style)**: QR -> phone web app -> audience votes or
  requests from a DJ-curated catalog (bounded, up to ~200 tracks); requests land in
  the open-dj UI with requester name attached; DJ load-or-ignore, session
  open/close toggle; guards: per-phone cooldown (~3 min), profanity filter on free
  text, emoji reactions on request items.
  - **Why**: AlphaTheta launched CoBeat on the CDJ-1500X (Wed 9 Jul 2026) and seeded
    the category at the budget tier - bars, mobile, home rigs - which is open-dj's
    audience. Research: `docs/research/cobeat-crowd-requests.md`.
  - **Mode**: spike first (quality signal: request-to-display latency + catalog gate
    UX), then mvp.
  - **Depends on**: CloudSync.
- [ ] **Mobile DJ ("DJ in the crowd")**: full DJing on phone/iPad with
  auto-stepping that takes over as much or as little as the DJ wants. Positioned
  for people who want to sound good, not people chasing technical brilliance;
  design must be more intuitive than rekordbox/djay mobile (their designs are
  rejected as reference). NOT a USB-stick emulator: phones cannot present as
  mass storage to players (see docs/research/phone-as-usb-drive-for-dj-decks.md),
  so this is a standalone playing surface, not a library stick.
  - **Open questions**: 4-channel feasibility on iPhone (screen/audio-engine
    limits); auto-mix quality signal calibration before build.
  - **Mode**: research + spike first.
- [ ] **SoundCloud Set -> tracks**: record a set and publish to SoundCloud with
  automatic tracklist markers. Natural fit with fingerprinting (v5) and the
  existing recording/cloudsync paths.
  - **Mode**: spike the SoundCloud upload API surface first.
- [ ] **Standalone lyrics bar (hardware)**: the lyrics-for-what-is-playing
  display as an independent device that just works without being the speaker.
  Build a proto; either sell the bar ourselves or sell the proto to whichever
  vendor wants to land it first. Depends on the lyrics alignment spike and a playback-state source
  (deck/engine IPC or mic input).

### v4 candidates

- [ ] **Phone → library WiFi transfer (QR hand-off + iOS app)**: QR code rendered in the web UI encodes a short-lived, tokenized LAN URL; the phone scans it and pushes files (tracks from the Files app / share sheet) to the machine over WiFi, landing in the Phase 9 acquisition queue before flowing through the existing analysis/import pipeline.
  - **Why**: the gig hand-off case - a guest, promoter, or your own phone has tracks that need to reach the library with no cable and no cloud round-trip. Proven category (LocalSend, PairDrop); open-dj can make it library-native instead of a generic file drop.
  - **Phasing**: P1 zero-install web - scan → Safari upload page (HTTP multipart or WebRTC), no app required; P2 optional companion iOS app/PWA with Share-Sheet extension for one-tap sends.
  - **Guards**: LAN-only (no internet path), one-time session token in the QR, audio-file allowlist, checksum verify before acquisition queue admission.
  - **Depends on**: web UI v1 (Phase 11) for the QR surface; acquisition queue (Phase 9) as the landing zone.

### v5 candidates

- [ ] **Music fingerprinting / recognition (KUVO-style)**: genre-segmented
  recognizer series (house/techno, DnB, hip-hop, pop-vocal, etc.) rather than
  one all-music model. Needed anyway for dedupe, play-history and track IDs on
  recorded sets; AlphaTheta now owns the KUVO recognition stack (DJ Monitor
  acquisition, May 2026) so this is also competitive parity. Corpus: volunteered
  libraries, analysis run IN PLACE in each owner's storage (only
  fingerprints leave it), subject to a legal review first. Research:
  `docs/research/music-fingerprinting-approach.md`.

---
*Roadmap created: 2026-04-16*
*Last updated: 2026-09-14 - added the v1 website task and v2/v3 music-enrichment, track-family, provider-plugin, and partnership candidates from the Cyanite ecosystem review; added v2 Phase 20 for semantic segments, hotcue templates, transition evaluation, coaching, and modder extension contracts (superseding a shorter stub). Phase 10.1 CAT-06 gap-fill remains in progress. Also Mon 14 Sep 2026: v3/v4/v5 candidate sections added (CoBeat crowd requests, mobile DJ, SoundCloud Set, lyrics bar, WiFi transfer, fingerprinting); cross-cutting eval-harness/adversarial-testing candidate added for the shipped AI surfaces.*
