# Known issues and residual deferrals (v1.0)

*Last updated: 2026-04-17 (v1.0 ship sweep).*

This doc is the single place where contributors and operators can see
what has NOT shipped green in v1.0, what is code-complete-but-not-
live-validated, and what has been deliberately pushed to v2. It is
derived from three places, each of which is the authoritative source
for its slice:

- [`.planning/MAINTAINER-REVIEW-QUEUE.md`](../.planning/MAINTAINER-REVIEW-QUEUE.md)
  for P0 physical-action stops and per-phase defaults flagged for human
  review (D1..D11, plus dozens of P1/P2 rows).
- [`.planning/V1-SHIP-SUMMARY.md`](../.planning/V1-SHIP-SUMMARY.md)
  section "What is deferred to v1.x or v2".
- [`.planning/SESSION-REPORT-2026-04-17.md`](../.planning/SESSION-REPORT-2026-04-17.md)
  "Patterns that failed or need improvement".

If a row here disagrees with one of those, the upstream doc wins and
this file is stale. File an issue or a PR with the correction.

## How to read this

- **Status: LIVE** means the code path is wired AND has been exercised
  against real vendor data by a human operator. Entries here should
  not be in this file; they are listed only where a recent fix
  upgraded a row from `PENDING` to `LIVE`.
- **Status: CODE-COMPLETE** means unit + integration tests pass, but
  no human has run it against a real library / USB / cloud bucket /
  mic. Flip to LIVE by running the runbook in `Evidence`.
- **Status: PARTIAL** means some sub-feature ships LIVE but a named
  gap remains.
- **Status: DEFERRED-V2** means the feature is not in the v1.0 scope
  and will not be worked on before v1.0 ships.

## Hard-stop physical actions before v1.0 tag

These are the P0 rows from
[`.planning/MAINTAINER-REVIEW-QUEUE.md`](../.planning/MAINTAINER-REVIEW-QUEUE.md)
that need the operator's hands before the v1.0 annotated tag is cut.
None of them block using the toolkit today.

| ID  | Feature / scope                                    | Status         | Closing action                                                                                                                             |
|-----|----------------------------------------------------|----------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| D1  | djay cautious live ratings pass (SYNC-06)          | CODE-COMPLETE  | Run `python -m apps.sync.apply_ratings --cautious --tracks UUID1,UUID2,UUID3 --live` per `docs/phase-04-cautious-runbook.md`.            |
| D2  | Harvest real djay cue blobs + byte-compare before first live cue write | CODE-COMPLETE  | Run `scripts/harvest_djay_cues.py`, byte-compare 3 blobs against `djay_writer`, then `apply_cues --cautious`.                            |
| D3  | Phase 4 Probe O2 (VBR MPEG-frame recompute) and `rb_writer.write_cues` live wire-up | PARTIAL        | Run `docs/phase-04-probe-o2-runbook.md` on one VBR MP3. `apply_cues --live` currently emits reversal scaffolding but does not call `rb_writer.write_cues`. |
| D4  | Capture djay playlist fixture + confirm leaf `type` byte is `0x01` | CODE-COMPLETE  | `PLAYLIST_TYPE_LEAF = 0x01` is a best-guess default. Capture one fixture, confirm, override via `--leaf-type-byte` if different.         |
| D5  | Voice mic loop (Phase 14) end-to-end on real hardware | CODE-COMPLETE  | Install whisper.cpp + PortAudio + openWakeWord model, then run `python -m apps.voice run`. See [`operator-setup.md`](operator-setup.md) Voice section. |
| D6  | Litestream + R2 + Syncthing real pairing           | CODE-COMPLETE  | Provision R2 bucket, set Doppler secret, pair Syncthing, verify Litestream replay.                                                         |
| D7  | USB bulk live write against a real USB drive       | CODE-COMPLETE  | Plug a drive, run `python -m apps.sync.usb.apply --profile usb-profiles/sample.yaml --cautious`, then `--bulk`.                          |
| D8  | Phase 18 drag-probe matrix against real djay / RB / Serato / Traktor | PARTIAL | 12 rows at [`docs/m7b-drag-matrix.md`](m7b-drag-matrix.md) currently all pending. At minimum, one successful drag per vendor before v1 tag. |
| D9  | Phase 17 manual djay drag test once launcher builds locally | CODE-COMPLETE | `cd apps/launcher && pnpm install && pnpm tauri dev`, press Alt+Space, drag a track into djay.                                        |
| D10 | Phase 10.1 agentic Rekordbox export retry with `RB_AGENT_LIVE=1` | CODE-COMPLETE | All four fixes for the 5120x2160 resolution issue are merged. Close Rekordbox, set `RB_AGENT_LIVE=1`, run `writer_agent`, walk away. |
| D11 | Phase 10.1 hardware HITL (XDJ-700 or CDJ-3000)     | PARTIAL        | Acquire used XDJ-700 (~$400-600) or borrow CDJ-3000. Software validation via dual-parser cross-check is complete; hardware is not.    |

## Requirements not shipped in v1.0

Pulled from [`coverage-matrix.md`](../coverage-matrix.md) and
[`reqs.json`](../reqs.json).

| Req       | Scope                                   | Status      | Notes                                                                                                                     |
|-----------|-----------------------------------------|-------------|---------------------------------------------------------------------------------------------------------------------------|
| CAT-06    | Pioneer CDJ USB export                  | PARTIAL     | Spike complete across three parallel writers (`rbox`, `rekordcrate`, agentic). Hardware HITL is D11. Not promoted to LIVE until the CDJ / XDJ validation closes. |
| SMART-04  | Dedicated web rule editor for smartlists | DEFERRED-V2 | CLI covers the use case; the Phase 11 SvelteKit SPA has partial scaffolding.                                              |
| LAUNCH-03 | Launcher-driven DJ driver manager       | DEFERRED-V2 | Install + updater + verifier for third-party DJ drivers. Deferred per decision C6 in `MAINTAINER-REVIEW-QUEUE.md`.               |

## Known defects + structural gaps (non-P0)

### Serato GEOB cue write (Phase 16)

Serato cue writes through `mutagen` GEOB frames were plumbed end-to-end
late in the v1 window (PR #65) and the `03-8-hot-cues` fixture now
round-trips. Earlier v1 RC notes described this as a silent drop; that
RC note is obsolete.

- Read path: `apps/adapters/serato/` preserves subcrate playlist
  membership on read (PR #65).
- Write path: `SeratoAdapter.write()` emits GEOB via mutagen.
- Remaining caution: only the `03-8-hot-cues` fixture exercises the
  write path in-repo; a real Serato library round-trip is still
  category D2-adjacent and has not been run.

### `coverage-matrix.md` churn

The pytest `reqs` plugin regenerates `coverage-matrix.md` on every
`make test` run. It is intentionally committed, but sibling agents
during the v1 fanout repeatedly left it dirty, masking real WIP. A
pre-commit hook that either commits or resets this file is tracked in
[`.planning/SESSION-REPORT-2026-04-17.md`](../.planning/SESSION-REPORT-2026-04-17.md)
as a v1.1 candidate. Until then: after `make test`, stage the diff or
`git checkout coverage-matrix.md` before committing unrelated work.

### `coverage-matrix` label drift

15 shipped requirements have zero `@pytest.mark.requirement` markers
despite their tests passing. The coverage dashboard understates
coverage because of this. Tracked in the session report; fix is a
sweep through `tests/` to add markers, not a code change.

### Unresolved stashes + non-canonical author identities

Historical only. 15 stashes and 21 non-canonical author commits
accumulated during the v1 fanout. Both are documented in
[`.planning/HEALTH-2-2026-04-17.md`](../.planning/HEALTH-2-2026-04-17.md)
and
[`docs/git-author-convention.md`](git-author-convention.md). No new
commits should add to either count. For agents: match the canonical
author identity from `docs/git-author-convention.md` before committing.

### Intra-repo links

The `apps/adapters/_shim/` package was retired during phase 16-15 (see
commits `0d22335` and `6a31672`). Two historical Markdown references
pointed at the retired path and were fixed in the broken-link audit
([`.planning/BROKEN-LINKS-2026-04-17.md`](../.planning/BROKEN-LINKS-2026-04-17.md)).
If new docs reference `apps/adapters/_shim/`, treat it as stale; the
canonical home is `apps.open_dj` (schema, registry, canon).

## Things deferred to v2 (not in scope for any v1.x)

These are explicitly NOT worked on before v1.0 ships and are called
out here so operators do not wait for them.

- **SMART-04** dedicated web rule editor (above).
- **LAUNCH-03** DJ driver manager (above).
- **CROSS-01 / CROSS-02** Windows + Linux support. v1 is macOS-only.
  The Tauri launcher is cross-platform-ready; the Python side uses
  macOS-specific paths (`~/Library`, `diskutil`) in several places.
- **AI-03 / AI-04** further LLM features beyond the AI-01 / AI-02
  structured suggester.
- **CloudKit coherence**: patching `cloudKit_record_cloudKit` blobs
  to avoid djay re-upload after playlist writes.

## Reporting a new issue

Open an issue at
<https://github.com/former-work-account/music-dj-tools/issues> with:

1. Which phase / app module the issue sits in.
2. The exact CLI invocation or API call that triggered it.
3. Whether any of the six rails fired (backup file path, reversal
   script path, readback error).
4. Vendor app versions for any DB you touched (Rekordbox / djay /
   Serato / Traktor).

Security-sensitive reports go to the contacts in
[`SECURITY.md`](../SECURITY.md) instead.

## Related documents

- [`.planning/MAINTAINER-REVIEW-QUEUE.md`](../.planning/MAINTAINER-REVIEW-QUEUE.md)
- [`.planning/V1-SHIP-SUMMARY.md`](../.planning/V1-SHIP-SUMMARY.md)
- [`.planning/SESSION-REPORT-2026-04-17.md`](../.planning/SESSION-REPORT-2026-04-17.md)
- [`.planning/BROKEN-LINKS-2026-04-17.md`](../.planning/BROKEN-LINKS-2026-04-17.md)
- [`coverage-matrix.md`](../coverage-matrix.md)
- [`RELEASE-NOTES-v1.0-rc3.md`](../RELEASE-NOTES-v1.0-rc3.md)
