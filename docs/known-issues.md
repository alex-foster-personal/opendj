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

Pulled from [`reqs.json`](../reqs.json) and the generated
`coverage-matrix.md` (gitignored -- run `make test`, or download the
`coverage-matrix` artifact from CI).

| Req       | Scope                                   | Status      | Notes                                                                                                                     |
|-----------|-----------------------------------------|-------------|---------------------------------------------------------------------------------------------------------------------------|
| CAT-06    | Pioneer CDJ USB export                  | PARTIAL     | Spike complete across three parallel writers (`rbox`, `rekordcrate`, agentic). Hardware HITL is D11. Not promoted to LIVE until the CDJ / XDJ validation closes. |
| LAUNCH-03 | Launcher-driven DJ driver manager       | DEFERRED-V2 | Install + updater + verifier for third-party DJ drivers. Deferred per decision C6 in `MAINTAINER-REVIEW-QUEUE.md`.               |

## Known defects + structural gaps (non-P0)

### Serato GEOB cue write (Phase 16)

Serato cue writes through ID3 GEOB frames were plumbed end-to-end
late in the v1 window (PR #65) and the `03-8-hot-cues` fixture now
round-trips. Earlier v1 RC notes described this as a silent drop; that
RC note is obsolete.

- Read path: `apps/adapters/serato/` preserves subcrate playlist
  membership on read (PR #65).
- Write path: `SeratoAdapter.write()` emits GEOB via the in-house
  `apps.shared.id3v2` (mutagen until Thu 1 Oct 2026).
- Remaining caution: only the `03-8-hot-cues` fixture exercises the
  write path in-repo; a real Serato library round-trip is still
  category D2-adjacent and has not been run.

### `coverage-matrix.md` churn -- RESOLVED 2026-08-16

`coverage-matrix.md` is no longer tracked. It is a build artifact, fully
derived from `reqs.json` × `@pytest.mark.requirement` markers, and the
pytest `reqs` plugin rewrites it on **every** run -- including partial
ones, whose totals are scoped to that run. A committed copy was therefore
stale the moment anyone ran a subset, which is why sibling agents kept
having to `git checkout --` it.

`.gitignore` had listed the file since `80bb80a5`, but it was committed
first (`19d91fed`) and a tracked file ignores `.gitignore`, so the rule
sat inert. `git rm --cached` completed the decision.

Where to read it now:

- **Locally:** run `make test` (or `pytest tests`) and open the generated
  file. It is gitignored, so it will not dirty the tree.
- **CI:** the full-suite copy is published as the `coverage-matrix`
  artifact on every run (`.github/workflows/ci.yml`).

Each generated file now states its own run scope in the header, and a
narrowed run (`-k`, `-m`, an explicit path) or one with collection errors
is stamped with a PARTIAL RUN warning. Quoting a coverage figure without
that header is the honest-denominator failure described in `CLAUDE.md`:
`pytest tests/sync` alone reports 9/57 (15.8%), against a real full-suite
figure of 48/57 (84.2%).

### `coverage-matrix` label drift

Re-measured on a full `pytest tests` run, 2026-08-16: 48/57 covered,
9 uncovered. Six of those nine are legitimately unbuilt (`AI-03`, `AI-04`,
`CROSS-01`, `CROSS-02` are v2; `LAUNCH-03` and `SMART-04` are
DEFERRED-V2). Only three are genuine label drift -- shipped requirements
whose tests exist but carry no marker:

| Req        | Shipped as                                          |
|------------|-----------------------------------------------------|
| `OPEN-02a` | Rekordbox open-dj adapter (read + write)            |
| `OPEN-02b` | djay Pro open-dj adapter (read + write)             |
| `OPEN-03a` | Spec prose + reference CLI + JSON Schema            |

Fix is a sweep through `tests/` to add markers, not a code change. The
earlier "15 shipped requirements" figure predates the marker sweep and
the orphan fix below.

### Orphan requirement markers -- RESOLVED 2026-08-16

Nine markers referenced IDs that were not in `reqs.json`, so they traced
to nothing while reading as coverage. `git log -S` confirmed none had
ever existed in `reqs.json` -- all were invented, not renamed: area
prefixes that are not categories (`ANALYSIS-03`, `TAGS-02`,
`SMARTLISTS-02`, `SPOTIFY-02`, `USB-03`), CHANGELOG audit labels
(`P1-A`, `P1-B`), a phase number read as a requirement number
(`INFRA-05`), and a documented future-backlog placeholder (`VOICE-02`).

All nine were retargeted to the requirement each test actually defends.
`tests/test_requirement_markers.py` now fails the suite on any marker ID
absent from `reqs.json`, so this cannot silently recur.

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
- `coverage-matrix.md` (generated + gitignored; `make test` locally, or the
  `coverage-matrix` CI artifact)
- [`RELEASE-NOTES-v1.0-rc3.md`](../RELEASE-NOTES-v1.0-rc3.md)
