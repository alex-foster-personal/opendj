# apps/lyrics - agent instructions

Two layers share this package.

**LINE-level lyrics (shipped)**: fetch line-synced lyrics per track, cache them,
index them, search them. Issues #1191-#1194, #935, #1453.

**WORD-level alignment (landing)**: the EVAL layer - ground truth, onset
metrics, the ASR cross-check behind the bench pages' dot colors - plus the
calibrated product verdicts. Experiment logs:
`specs/karaoke-lyrics-alignment.md` (counters lyrics-align 0-5, oltf 1-2b,
novox 0-1, crate 1) and `specs/karaoke-lyrics-operational-plan.md` (the spike
-> feature plan, with the section-13 landing contract). Read them before
changing anything measured.

Pure stdlib, runs in the repo venv. The ALIGNER and ASR themselves are NOT
here and are NOT on this branch yet: they are Modal PEP 723 scripts
(`scripts/modal_align_spike.py`, `scripts/modal_asr_spike.py`) that land with
PR-3, per the heavy-deps house rule. Anything needing soundfile/numpy is also
a PEP 723 script (`scripts/lyrics_stem_coverage.py`, likewise PR-3).

## Landing status (PR-5 docs + runbook landed, section 13 D13.6)

This package holds the line-level layer, the eval + repair modules, and the
PR-2 storage layer. PR-5 (this wave) lands the operator docs, the lyrics KPI
ledger, and the v10 migration runbook (`scripts/lyrics_migrate_state_v10.py`).
PR-3 pipeline (batch driver, worker, register-stems, stems push/hydrate,
sources package without Genius) landed with issue #2078. Genius and the
undeclared ``syncedlyrics`` scrape dependency were dropped from the branch
port: neither is in ``pyproject.toml``, and no provider subcommand depends on
them. Still to land: PR-4
follow-ons beyond what is already on main. The live Mac DB migration is issue
#2081. Six branch-only scripts remain on
`cc/karaoke-lyrics-beat-grid-eae999--2026-09-09` only:
`eval_lyrics_version.py`, `lyrics_best_of_select.py`,
`lyrics_confidence_calibration.py`, `lyrics_oltf_stem_presence.py`,
`novox_stem_presence.py`, `spike_align_jamendo.py`. Modal spikes
(`scripts/modal_asr_spike.py`, `modal_align_spike.py`) run via
`uv run --with modal python`, not PEP 723.

## Modules - storage (PR-2)

- `store.py` - the ONE read/write surface for `lyric_verdict`, a SYNCED row at
  schema _V10. Every write stamps through `sync_stamp.stamped_transaction` +
  `stamp_and_log`, so it needs the cloudsync-style write connection, never a
  connection opened from a hardcoded DB path. Deletes do not exist: purge
  tombstones, and `upsert_verdict(..., resurrect=...)` is keyword-only with no
  default so reviving a purged track is always a named act. Readers filter the
  row's own tombstone AND the parent `tracks` tombstone.
- `karaoke_cache.py` - the persistent contract for WORD-level timings at
  `data/state/karaoke-cache/{stable_id}.json` (artifact kind `karaoke_words`),
  the deliberate sibling of `cache.py`. Owns `PIPELINE_VERSION`, the writer
  (which assigns `idx` positionally over the whole list) and the strict
  parser. `canonical_bytes` is the ONE serialisation `words_content_hash` is
  taken over, so the row's hash equals the file's and the R2 object's.
- `artifacts.py` - produce and load that artifact through the CloudSync tier.
  Deliberately NOT `hydration.apply_policy_after_produce`: no `track_locations`
  row is ever written (a words digest reaching audio resolution would poison
  playback), and `words_content_hash` on the verdict row is the only location
  record. Local mode touches no network; cloud mode obeys `resolve_policy`
  (pinned pushes and verifies, excluded stays local, cached/stream refused).
- `purge.py` - the licensing lever, `purge --source <prefix>`: tombstone the
  row, unlink the local artifact, delete the R2 object, with honest per-store
  counts and one `stamped_transaction` per row so an interrupted purge is
  resumable.
- `legacy_words.py` - the one-shot conversion of the branch-era
  `lyric_verdict` / `lyric_word` tables (renamed aside by the D13.6 runbook)
  into artifacts + 16-column rows. Drops the legacy tables only on a clean
  sweep (`drop_legacy=True`, the CLI default). The runbook keeps them.
- `ingest_state.py` - load a finished bench run into state.db. Its own module,
  not `__main__`, because the PR-3 batch driver imports it. Unmatched tracks
  are reported with a reason, never silently dropped.

## Modules - line level

- `cache.py` - the persistent contract for line-synced lyrics at
  `data/state/lyrics-cache/{stable_id}.json`. Timestamps are integer
  milliseconds at the cache boundary. It carries NO word timestamps by
  design, and is not to be widened: word timings are a separate artifact
  (operational-plan D13.2).
- `asr_hallucination.py` - LYRICS-12: drops Whisper hallucination lines
  ("Thank you.", subtitle credits) from ASR line lyrics and classifies a
  transcript with fewer than two real lines as no-lyrics. Applied when the
  ASR fetch caches and when `GET /tracks/{id}/lyrics` serves; cache files
  are filtered on read, never rewritten.
- `service.py` - the shared fetch/cache path (LRCLIB, the sole permitted
  free keyless source) used by the CLI and API readers. PR-3 also ships
  ``sources/lrclib.py`` for the batch candidate rail; both clients are
  acceptable and intentionally separate (line cache vs word-candidate fetch).
- `search_contract.py` - the typed lyric-search data boundary, including the
  honest denominator: coverage is cached-lyrics over resolvable audio rows,
  never over the whole library.
- `search_index.py` + `search_index_schema.py` + `search_index_batch.py` -
  the durable checkpointed FTS5 index at `data/state/lyrics-index.db`, one
  bounded batch per call. Split three ways for the 600-line file gate.
- `index_job.py` - the pausable reconcile job the daemon and the `index` CLI
  both drive, so "yield to the UI" has one implementation.
- `search_snippet.py` - matched-phrase-in-context snippets, re-read from the
  original cached lines because the index's `searchable_text` is lossy on
  purpose.

## Modules - word level (eval + repair)

- `jamendo.py` - JamendoLyrics ground-truth loader (79 songs, per-word
  onsets). Dataset at `data/datasets/jamendolyrics/` (gitignored; the puller
  `scripts/pull_jamendolyrics.py` lands with PR-3, so until then point
  `--dataset-dir` at an existing pulled copy). Every dataset-dependent test
  SKIPS without it - and a skip is not a pass.
- `metrics.py` - AAE/median/p95/@tolerance onset metrics. The verifier page's
  numbers come from HERE via the manifest generator, so page and CLI can
  never disagree. Distinct from `scripts/lyrics_alignment/scorer.py`, which
  is the ratified Ship-tier scorer (medae/pco_300ms/catastrophe_rate with an
  explicit unplaced-word denominator) that #1515 LYR-01 reports against.
- `crosscheck.py` - the independent ASR witness. Three products: version
  similarity, round-4a triage flags (`flagged_indices`), and the round-5
  6-class `witness_verdicts` (agree/drift/contradict/lost/unheard/
  unmatchable) that paint the listen page's dot colors. WITNESS_* thresholds
  live here, the ONE canonical home - the manifest generator imports them and
  the listen page mirrors + refuses drift. Local-first matching with an
  ambiguity gate; the why is in the module docstring and the spec's round-5
  autopsy.
- `annotations.py` + public `annotations/*.yaml` - failure-mode span
  fixtures. `jamendolyrics.yaml` tags the public benchmark. Private corpus
  annotations are unavailable in this public copy. Word indices are 0-based
  inclusive and validated against word counts - a drifted span fails tests.
- `nonlexical.py` - round-4b NEGATIVE result (kept, flag off everywhere).
- `vocal_presence.py` - THE canonical no-lyrics bands. `coverage_verdict()`
  maps stem vocal coverage to no-lyrics / sparse / vocal.
  NO_LYRICS_MAX_COVERAGE is 12.5. Private calibration inputs are unavailable
  in this public copy; their acceptance cannot be reproduced here.
  Never hardcode these bands anywhere else - import them.
- `verdict.py` - text-only version classification (matched /
  structure_mismatch / needs_acoustic_check / wrong_song / unverifiable) from
  a sheet-vs-ASR diff, with per-block findings.
- `arbitrate.py` - acoustic arbitration of findings a text diff cannot
  adjudicate. The private repeat-audit research module is unavailable in
  this public copy.
- `repair.py` - stage-4 repair: apply an ARBITRATED verdict's findings as
  sheet edits. Never acts on its own judgement; `unverifiable`, `wrong_song`,
  and `needs_acoustic_check` are refused, not edited. Every repaired word
  carries its source index (or None when inserted) so scoring keeps honest
  denominators.
- `asr_match.py` - word normalization + matching shared by verdict, repair
  and perturbation.
- `perturb.py` + `cases.py` - the synthetic version-damage generator and the
  case builder that turn ground-truth songs into a labelled eval set for the
  verdict/repair loop.
- `lines.py` - THE canonical word -> line grouping and per-line quality band,
  derived server-side so every surface agrees. Typed against the structural
  `WordRow` protocol, deliberately not against a storage class: word timings
  live in the per-track `karaoke_words` artifact (landed in PR-2, see
  `karaoke_cache.py`), never in a table.
- `sources_config.py` - operator-configurable source ORDER and titles. The
  `sources/` package that consumes it lands in a later PR; until then this
  module has no in-repo caller.
- `fixtures/novox-ear-r0.json` - MTG-Jamendo benchmark ratings with provenance
  (question asked, method, corpus, coverage signal). The private follow-up
  corpus and its dependent calibration test are withheld from this public
  copy; private calibration acceptance is unavailable here.

## CLI (`python -m apps.lyrics ...`)

One parser, one dispatch, both families in `__main__.py`.

- `fetch <stable_id>` - fetch and cache one track's line-synced lyrics.
- `index [--once] [--batch N] [--interval S] [--force-rebuild]` - build or
  resume the checkpointed search index. `--force-rebuild` is the operator's
  reachable recovery path for the bulk-removal guard, and applies only to the
  first batch of a drain so the guard re-arms.
- `stats` - dataset summary.
- `score --pred DIR [--by-tag]` - onset metrics vs ground truth, optionally
  pooled per failure-mode tag.
- `crosscheck --pred DIR --asr DIR` - round-4a flag precision/recall/lift.
- `witness-eval --pred DIR --asr DIR [--local-window S]` - calibration of the
  witness classes vs ground truth: P(error|class), false-red rate, green
  error rate. `--local-window 0` is the round-4 diff-only ablation. Run this
  after ANY change to crosscheck matching or thresholds; guards ratchet per
  the spec's experiment-log convention.

The three PR-2 storage subcommands, whose bodies live in their own modules so
`__main__.py` stays under the 600-line gate:

- `ingest-state --manifest FILE [--coverage FILE] [--match-by vendor-id|
  file-path] [--db-path FILE] [--write]` - load a bench run into state.db.
  Omit `--write` for a dry run that matches and reports only.
- `migrate-legacy-words [--db-path FILE] [--dry-run]` - convert the
  renamed-aside branch-era tables.
- `purge --source PREFIX [--db-path FILE] [--dry-run]` - remove one provider's
  lyrics from the row, the disk and R2.

`--dataset-dir` belongs to the four eval subcommands, not to the parser, so
`fetch` and `index` never advertise a flag they ignore.

Pipeline (PR-3, bodies in `cli_pipeline.py` + `stems_sync.py`):

- `batch run|resume --tracks FILE --corpus NAME [--live] [--state-dir]`
- `jobs work [--once] [--state-dir]` (loop recipe: `just lyrics-jobs-worker`)
- `register-stems --corpus {crate,own-crate,oltf,batch1,batch2,all} [--write]`
- `stems push --missing [--dry-run] [--data-dir]`
- `stems hydrate <stable_id> --manifest PATH [--dry-run] [--data-dir]`

## Runbook (PR-5)

Migrate a branch-era `state.db` to schema v10 and convert legacy words:

```sh
uv run --no-sync python -m scripts.lyrics_migrate_state_v10 \
  --db-path data/state/state.db          # dry-run (default)
uv run --no-sync python -m scripts.lyrics_migrate_state_v10 \
  --db-path data/state/state.db --live   # write
```

Backs up to `state.db.bak-YYYY-MM-DD` first, renames branch-era tables aside,
drops `idx_lyric_verdict_red`, applies the ladder to v10, converts legacy rows,
and keeps `*_legacy` tables. Full operator detail: `docs/lyrics-storage.md`.

## sync_policies cells

Before any cloud produce, PUT `karaoke_words` and `stem_bundle` for this machine
(see `docs/lyrics-storage.md` for commands). Local mode does not need them.

## Product surface

Line search is served by `apps/webui/server/routes/lyrics_search.py` at
`/api/v1/lyrics/search`. The word-level endpoints live in
`apps/webui/server/routes/lyrics_words.py` (PR-4a, #2158, Fri 11 Sep 2026),
mounted under `/api/v1`: `/tracks/{stable_id}/lyrics/words[?include=lines]`,
`/lyrics/summary`, `/lyrics` (triage listing),
`/tracks/{stable_id}/lyrics/override` (PUT), `/lyrics/config` (GET + PUT),
`/lyrics/jobs` (GET + POST), `/lyrics/purge` (POST), `/bench/lyrics-kpi`.
Agent-native parity is a house requirement, so every UI surface below has one
of those routes behind it.

UI surfaces on main (PR-4a #2158 plus the rest of PR-4 in #2162, both Fri 11
Sep 2026):
- Track page: `lib/components/LyricsPanel.svelte` (lines + verdict +
  override), mounted by `routes/track/[stable_id]/+page.svelte`, which also
  opens `lib/components/lyrics/StageOverlay.svelte` on `?stage=1`. The
  overlay itself is mounted once in `routes/+layout.svelte`.
- Waveform: `rb/wave/LyricLanes.svelte` picks `WordLane.svelte` when
  `lyrics_global && lyrics_waveform_overlay` AND the track really has aligned
  words (`wave/word-lane-state.ts::selectWordLane`), else the line lane
  `LyricsLane.svelte` renders exactly as before.
- Deck: `rb/deck/DeckLyricLine.svelte` IS mounted in `rb/Deck.svelte`, driven
  by that component's `presentedPositionSec()` over `deck.position_ms` and
  gated by `lyrics_deck_line && lyrics_global`.
- Library browser: listing rows carry `lyrics` (`LyricsRowSummaryOut`), plus
  `is_remix` / `is_radio_edit`, hydrated in
  `apps/webui/server/rb_vendor_pkg/track_rows.py` by ONE
  `lyric_store.bulk_verdicts` read per page. `browser/LyricColumn.svelte` and
  its `browser/LyricTip.svelte` hover render them over the pure formatters in
  `browser/lyric-column.ts`; the `lyrics` sort key (`lyricsSortValue`) and the
  Remixes / Vocals filter checkboxes (Vocals = `n_lines >= 6`,
  `VOCALS_FILTER_MIN_LINES`) read the same field.
  `lib/components/lyrics/ScrubLyricStrip.svelte` sits under both preview
  strips (`browser/PreviewStrip.svelte`, `deck/StripWaveform.svelte`) behind
  `lyrics_hover_scrub`.
- Client word/line model: `lib/rb/lyrics/{types,build-track,cursor}.ts`,
  fetched through `lib/api-karaoke.ts` and held by
  `lib/lyrics/lyrics-cache.svelte.ts`.
- /admin: the four `routes/admin/` panels `LyricSourceOrder.svelte`,
  `LyricJobs.svelte`, `LyricTriage.svelte` and `LyricsKpiPanel.svelte`, over
  `routes/admin/lyrics-api.ts` (which validates every response against
  `$lib/api` types before it renders).
- Six `lyrics_*` prefs live in `lib/rb/lyrics-prefs.ts`; `remixes_filter` /
  `vocals_filter` are localStorage-only, in `lib/rb/library-filter-prefs.ts`
  alongside `next_only_filter`.
- Proof: `just lyrics-e2e` (Playwright, real engine on 127.0.0.1:8706 + vite
  5328, seeded verdict + words artifact via
  `tests/e2e/support/lyrics_words_fixture.py` into its own disposable data
  dir). The spec writes its screenshot to
  `apps/webui/frontend/test-results/lyrics-words-panel.png`; the config sets
  no `outputDir`, so everything else Playwright keeps lands in
  `apps/webui/frontend/test-results/`.

Coverage numbers will come from `scripts/lyrics_stem_coverage.py --stems DIR
--out FILE [--envelopes]` (PR-3), one implementation for every corpus,
reusing `scripts/vocal_region_worker.py`'s pure maths by path so this layer,
the library vocal-cache and the bench widgets cannot disagree.

## Data layout (all gitignored)

`data/datasets/jamendolyrics/` is the ground truth (not carried by any
worktree; point `--dataset-dir` at a pulled copy). Eval corpora live under
`data/state/lyrics-eval/`, each with the same shape (`audio/ stems/ asr*/
pred*/ vocal-presence.json`). Private operator corpora are unavailable in
this public copy. `novox/` is the public MTG-Jamendo calibration corpus
(373 tracks with external human voice/instrumental labels).
JamendoLyrics predictions sit in `round*/`.
The bench pages and manifest generators are in `scripts/bench/` (see its
CLAUDE.md for serving rules and mounts). None of this ships in the repo, so a
missing corpus is a skipped test, and a skip is not a pass - say so.

## Non-obvious things that have burned people here

- Self-confidence (MMS log-prob) is meaningless once the alignment path
  detaches - it may NEVER pick a dot color, only the count (round 3a/4d).
- difflib's global diff collapses under chorus repeats and orphans
  verbatim-transcribed verses; that is why witness matching is local-first
  (round 5). Conversely, local matching WITHOUT the uniqueness gate launders
  detached words into green - both failure modes are pinned by tests.
- Quote witness numbers only from a fresh `witness-eval` run, never from
  memory (honest-denominators house rule).
- Prediction filenames come back from APFS in NFD while `JamendoLyrics.csv`
  holds NFC. Join song names through `__main__._song_key`, never raw: the
  raw join silently loses the 11 accented songs of 79.
- Whisper is run-to-run nondeterministic on hard tracks (temperature
  fallback); asr-r4a used vad_filter=False - the lyrics-version fork measured
  VAD-off hallucination loops on 11/79 stems. VAD-on re-transcription is the
  queued round-5b lever; do not mix it into a matcher round
  (attributability).
- Whisper's AUTO-DETECT language can produce hallucinations. Pin the
  language from the (lingua-detected) lyric text rather than relying on
  automatic detection. Private corpus experiments are unavailable here.
- Coverage bands and witness thresholds have exactly one home each
  (`vocal_presence.py`, `crosscheck.py`). The bench pages MIRROR them and
  refuse a manifest whose numbers disagree - so a threshold change means
  editing the module, regenerating manifests, and re-running the fixtures.
