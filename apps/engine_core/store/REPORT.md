# Store schema consolidation -- inventory and findings

Scope: every `CREATE TABLE` / `CREATE INDEX` / `CREATE VIRTUAL TABLE` reachable from `apps/`, folded into `apps/engine_core/store/schema.py`. Additive only -- no legacy module was modified. `tests/engine_core/test_store_schema.py` calls each legacy bootstrap directly and compares normalised `sqlite_master` output object-by-object, so this table is verified, not asserted.

## Table inventory

| Table | Legacy source file | Bootstrap entrypoint | Notes |
|---|---|---|---|
| `tracks` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | name collides, O-1 |
| `track_vendor_ids` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | |
| `track_fields` | `apps/shared/state/schema.py` | `apply_migrations` (v3 rebuild) | CHECK drift, O-7 |
| `track_field_history` | `apps/shared/state/schema.py` | `apply_migrations` (v2 rebuild) | |
| `playlists` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | no index, O-10 |
| `playlist_memberships` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | idx_playlist_memberships_item_id (legacy v13); reverse-lookup by stable_id still a scan, O-10 |
| `adapters` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | no index, O-10 |
| `events` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | name collides, O-2 |
| `track_locations` | `apps/shared/state/schema.py` | `apply_migrations` (v4/v5) | data backfill, O-14 |
| `lyric_verdict` | `apps/shared/state/schema.py` | `apply_migrations` (v10) | consolidated rung v4, O-16 |
| `path_availability` | `apps/shared/state/migrations_v18.py` | `apply_migrations` (v18) | consolidated rung v7, PERF-RB-01 |
| `analysis` | `apps/analysis/store.py` | `_ensure_analysis_tables` | |
| `analysis_events` | `apps/analysis/store.py` | `_ensure_analysis_tables` | index policy differs, O-11 |
| `pairings` | `apps/shared/pairings/schema_sql.py` | `ensure_phase08_tables` | shape collides, O-6 |
| `smartlists` | `apps/shared/pairings/schema_sql.py` | `ensure_phase08_tables` | redundant index, O-9 |
| `play_orders` | `apps/shared/play_orders/schema.py` | `apply_play_order_migrations` | |
| `play_order_entries` | `apps/shared/play_orders/schema.py` | `apply_play_order_migrations` | |
| `play_orders_schema_meta` | `apps/shared/play_orders/schema.py` | `_ensure_meta` | second counter, O-8 |
| `spotify_playlist_meta` | `apps/spotify/state_writer.py` | `ensure_aux_tables` | no index, O-10 |
| `pending_tracks` | `apps/spotify/state_writer.py` | `ensure_aux_tables` | |
| `sets` | `apps/sets/state.py` | `_ensure_schema` | no index, O-10 |
| `set_events` | `apps/sets/state.py` | `_ensure_schema(events_table='set_events')` | O-2 |
| `settings` | `apps/voice/settings.py` | `SettingsStore.__post_init__` | own DB file today |
| `duplicate_clusters` | `apps/dedup/schema.py` | `ensure_schema` | ad-hoc ALTER, O-13 |
| `track_aliases` | `apps/dedup/schema.py` | `ensure_schema` | own DB file today |
| `tag_provenance` | `apps/dedup/schema.py` | `ensure_schema` | own DB file today |
| `tracks_fts` | `apps/launcher/scripts/bootstrap_db.py` | `apply_launcher_migration` | name collides, O-4 |
| `tracks_frecency` | `apps/launcher/scripts/bootstrap_db.py` | `apply_launcher_migration` | FK drift, O-5 |
| `schema_meta` | `apps/shared/state/schema.py` | `_ensure_meta` | infra, created by the runner |

### Cache DB inventory (`apply_cache_migrations`, NOT the durable ladder)

| Table | Legacy source file | Bootstrap entrypoint | Notes |
|---|---|---|---|
| `fingerprints` | `apps/shared/fingerprints.py` | `FingerprintCache.__init__` | shape collides, O-3 |
| `file_hashes` | `apps/shared/hashing.py` | `HashCache._ensure_schema` | no migration path, O-12 |

### Deliberately excluded

| Table | Source | Why |
|---|---|---|
| `rb_hot_cue_reversal`, `rb_hot_cue_slot_revision` | `apps/webui/server/rb_vendor.py` | written into the rekordbox vendor `master.db`, not state.db. Still declared as `VENDOR_SIDECAR_DDL` + `ensure_vendor_sidecar_tables()` so the DDL has one home; covered by its own test |
| `search_meta` | `apps/webui/server/search_index.py` | lives only in the derived `state-fts.db`, dropped and recreated on every rebuild |
| `tracks` (launcher `SCHEMA`) | `apps/launcher/scripts/bootstrap_db.py` | standalone launcher DB, different columns (O-1) |
| `tracks` (bench) | `apps/launcher/scripts/latency_check.py` | throwaway benchmark scratch DB (O-1) |
| `pairings` (projection) | `apps/webui/server/routes/copilot.py` | `:memory:` projection built per request (O-6) |
| `fingerprints` (sync) | `apps/sync/fingerprint.py` | divergent second cache (O-3) |
| `tracks_fts` (search) | `apps/webui/server/search_index.py` | different columns, different DB file (O-4) |

## Oddities

**O-1. Three disagreeing `tracks` tables.** Shared-state owns the canonical one (`stable_id`, `stable_id_tier`, `artists_json`). `bootstrap_db.SCHEMA` declares a completely different `tracks` (`path`, `artist`, `genre`, `key`, `bpm`, `source`) for the launcher's standalone DB; `latency_check.py` declares a third. Nothing names them apart, so a reader cannot tell from a query which shape is being read.

**O-2. Two disagreeing `events` tables.** Shared-state's is the durable domain log (`ts`/`kind`/`stable_id`/`payload_json`/`actor`); `apps/sets/state.py` `SCHEMA_SQL` declares an `events` that is a per-set timeline. Legacy already noticed and added `_PHASE5_SCHEMA_SQL` renaming it to `set_events` on the shared-DB path, but the colliding definition is still live on the standalone path. Consolidation takes `set_events` unconditionally.

**O-3. Two disagreeing `fingerprints` tables.** `apps/shared/fingerprints.py` (canonical, per `apps/dedup/schema.py`'s own docstring): 8 columns, `NOT NULL` payload, `stable_id` link, `bitrate`, an index. `apps/sync/fingerprint.py`: 6 nullable columns, no `stable_id`, no `bitrate`, no index, `computed_at` as `REAL` epoch seconds not `TIMESTAMP`. Its docstring documents a *four*-column shape the code does not create -- docstring drift on top of schema drift.

**O-4. Two disagreeing `tracks_fts` virtual tables.** Launcher: `title, artist, album, genre, key, tags` in `state.db`. `search_index.py`: `stable_id UNINDEXED, title, artist, genre, comments, notes, tags` in `state-fts.db`. Same name, same tokenizer, different columns, different files.

**O-5. `tracks_frecency` FK drift inside one file.** `bootstrap_db.SCHEMA` declares `REFERENCES tracks(stable_id)`; `bootstrap_db.apply_launcher_migration` declares the same table with no FK (with a comment explaining why). Same module, two referential-integrity contracts. Consolidation takes the no-FK state.db variant.

**O-6. `pairings` shape and vocabulary mismatch.** `schema_sql.py`: 8 columns, composite PK, two CHECKs. `routes/copilot.py`: 4-column `:memory:` projection, no PK, no CHECK. The direction vocabularies also disagree -- the table CHECK accepts `('into','out_of','either')` while `StateBackend` yields `'->'`/`'<->'` and copilot translates at the boundary, raising `AssertionError` on anything else.

**O-7. CHECK drift, the `webui` source enum.** `track_fields.source` originally accepted 8 sources; the webui daemon writes `'webui'`, added only by shared-state v2 -> v3, which had to REBUILD the table. Any DB that never ran that step silently rejects every webui edit. This is why adoption probes `sqlite_master` for `'webui'` and raises rather than adopting.

**O-8. Two independent schema counters.** `schema_meta` (1..5) and `play_orders_schema_meta` (1) exist separately by design so two phases could land in either order; the other ~15 bootstraps have no counter at all. Consolidation keeps and *stamps* both so the legacy runners short-circuit.

**O-9. `idx_smartlists_name` is redundant** -- `smartlists.name` is already `NOT NULL UNIQUE`, so SQLite already has an autoindex. Reproduced verbatim; deletion candidate.

**O-10. Tables with no index:** `adapters`, `file_hashes`, `play_orders_schema_meta`, `playlists`, `sets`, `settings`, `spotify_playlist_meta`, `track_fields`, `tracks_fts`. Most are covered by their PK. Two look like real gaps: `playlist_memberships` is queried by `stable_id` but has PK `(playlist_id, position)`, so "which playlists is this track in" is a full scan (the legacy v13 `idx_playlist_memberships_item_id` indexes `item_id`, not `stable_id`); and `spotify_playlist_meta` is reached from `playlists` by `vendor_pl_id` with no index on it.

**O-11. Inconsistent partial-index policy on the same concept.** Shared-state uses `... WHERE stable_id IS NOT NULL` (`idx_events_stable_id`); `apps/analysis/store.py` indexes the same nullable concept with no predicate (`idx_analysis_events_stable`).

**O-12. `file_hashes` has no migration path.** `HashCache._ensure_schema` reads `PRAGMA user_version` and on mismatch DROPs and recreates -- a silent full cache wipe. Also the only `CREATE TABLE` in the tree without `IF NOT EXISTS`.

**O-13. Hand-rolled ALTER outside any ladder.** `apps/dedup/schema.py:ensure_schema` probes `PRAGMA table_info(duplicate_clusters)` and issues `ALTER TABLE ... ADD COLUMN flagged_manual_review` if absent -- a migration with no version number, re-probed on every open.

**O-14. Data-bearing migration steps hide inside DDL lists.** Shared-state `_V4` ends with `INSERT OR IGNORE INTO track_locations ... SELECT ... FROM tracks`. A consolidation replaying only `CREATE` statements would build the right shape with the wrong contents; the runner replays it on adoption and a test proves it.

**O-15. `apps/shared/state/schema.py` has `_V5 = _V4`.** v5 re-runs v4 verbatim because a live agentbox DB was stamped at v4 out-of-band without the tables existing -- a repair step disguised as a version bump. This is why adoption verifies object existence rather than trusting the counter.

**O-16. Legacy `_V10` mirrors as a table, not as a rebuild (karaoke lyrics, Wed 9 Sep 2026).** Legacy step 8 -> 9 (`apps/shared/state/migrations_v10.py`) does two things: it creates `lyric_verdict` + `idx_lyric_verdict_red`, and it REBUILDS `sync_policies` to widen the `asset_kind` CHECK from four kinds to six (`'lyrics_cache'` and `'karaoke_words'` join `'audio','stem_bundle','anlz_cache','vocal_cache'`). This module mirrors the OUTCOME of both, not the mechanism:

- the new table and its index become the `lyrics` domain (`_LYRICS`) and consolidated rung `_V4`, following the `native_analysis_v1` precedent -- its own rung rather than an append to `_V1`, because an already-stamped database never re-runs a rung it has passed;
- the widened `asset_kind` CHECK is written straight into `_SYNC_INFRA`'s `sync_policies` DDL. There is no `sync_policies_v10` / `DROP` / `RENAME` dance here: this module declares end shapes, and the parity gate compares the legacy ladder's FINAL stored text against ours.

Two divergences from the legacy text, both deliberate and neither a shape change:

1. legacy writes a bare `CREATE TABLE lyric_verdict` and a bare `CREATE INDEX idx_lyric_verdict_red` so that a stale index left behind on a renamed-aside branch-era table fails the migration loudly (`migrations_v10.py` reading 2). This module cannot: the adoption path replays every statement against already-provisioned databases, so `IF NOT EXISTS` is mandatory here. `normalize_object_sql` strips the flag before comparing, so the gate is unaffected.
2. `LEGACY_SHARED_STATE_VERSION` is NOT bumped, and that is a recorded hazard rather than an oversight. `scripts/sync_drift_rules.MIRROR_VERSION_DEBT` pins the mirror at 7 until the consolidated ladder also builds v8's three views, and lifting that is a schema decision on a dormant consolidation target (D-06 in `scripts/sync_drift_lint.py`), i.e. the maintainer's call. What it costs is concrete: a database BORN consolidated is stamped at legacy 7, so the legacy runner would replay `_V8` and `_V9` (harmless, every statement is `IF NOT EXISTS`) and then `_V10`, whose bare `CREATE TABLE lyric_verdict` fails LOUDLY against the table this module already created. Loud, not silent: the failure lands before the `sync_policies` rebuild runs, so no populated table is dropped. Bumping the mirror to 10 is step (3) of that entry's UNBLOCK ORDER and pays this off with it.

## Runner version numbering

Shares `schema_meta` but stamps at `VERSION_OFFSET = 1000`: rows below the offset are legacy (1..`LEGACY_SHARED_STATE_VERSION`, today 9), `1000` is the `ADOPTION_VERSION` marker (this file predates consolidation), and `1000 + n` is consolidated `n` -- `1003` at the time of writing. Fresh DBs get the legacy range stamped (so the legacy runner no-ops) plus `1000 + SCHEMA_VERSION` and no 1000 row. `apply_migrations` refuses below `MIN_ADOPTABLE_LEGACY_VERSION = 3`.

### Known cosmetic readout: `schema version: 1001 (target 5)` (R3 #4)

After the consolidated runner has stamped a DB, the legacy CLI prints

```
schema version: 1001 (target 5)
```

which reads as a DB nine hundred and ninety-six versions past its own target. It is cosmetic -- nothing branches on it -- but it is confusing enough to be worth naming.

Cause: `apps/shared/state/schema.py:255` `_current_version` takes an **unfiltered** `SELECT COALESCE(MAX(version), 0) FROM schema_meta`, so it reads the offset rows the consolidated runner writes. This module already reads its own counter with a range filter for exactly this reason (`_legacy_version` uses `WHERE version < VERSION_OFFSET`, `consolidated_version` uses `>=`), and the offset was chosen so the two counters could never be confused for one another -- which is what makes the readout wrong rather than the design wrong.

Deliberately NOT fixed here: `apps/shared/state` is frozen for this tranche, and the honest fix is one line in the legacy module, not a contortion in this one. The consolidated runner already stamps nothing that belongs in the legacy counter's range, so there is nothing to correct on our side of the ownership line.

Post-parity fix, for whoever unfreezes `apps/shared/state`: give `_current_version` the same `WHERE version < VERSION_OFFSET` filter its consolidated counterpart uses. The print site is `apps/shared/state/cli.py:32`.

## Unledgered tables in live DBs (R3, blocks a clean adoption once branches land)

The consolidated ladder is built from what the merged tree reaches. Real state DBs hold more than that, because unmerged branches have already shipped DDL into them. Measured against `state.db` (9,986 tracks) on Wed 19 Aug 2026: **six tables and three views** exist in the live file that the ladder does not declare.

From `adddff8a` (`feat(mik): retain analysis for audio we do not have`), created by that branch's `_V4`:

| Object | Type |
|---|---|
| `track_availability` | table |
| `unmatched_source_analysis` | table |
| `track_energy_segments` | table |
| `analysis_field_verification` | table |
| `tracks_available` | view |
| `tracks_unavailable` | view |
| `track_fields_available` | view |

From `6f39ab99` (`origin/codex--checkpoint-dirty-routing-20260730`), `apps/shared/pairings/capture_repo.py`:

| Object | Type |
|---|---|
| `pairing_alignments` | table |
| `pairing_sync_snapshots` | table |

R3 attributed all six tables to `adddff8a`; the split above is what the live file and `git log -S` actually show. Two branches, one symptom.

Three consequences, all real today:

1. **The ladder must adopt these when those branches merge.** Until then adoption leaves them alone -- the shape audit only inspects objects the ladder itself creates -- which is correct behaviour but means the consolidated schema is knowingly incomplete against production files.

2. **Version number 4 means two different things.** `adddff8a` stamps `schema_meta` v4 for the availability tables; the merged ladder stamps v4 for `track_locations` plus its backfill. The live DB carries both sets of objects and a single `4` row (applied Tue 28 Jul 2026), so the counter cannot say which one ran. This is the mechanism behind O-15's `_V5 = _V4` repair step: a DB stamped at 4 by the other branch still owed the `track_locations` work, and re-running v4 as v5 was how it got it. It is also the concrete argument for reading live shapes rather than trusting counters -- on this file the counter is not evidence of anything.

3. **Views are not hypothetical here.** The three `*_available` views are why `existing_objects()` had to stop filtering `type = 'table'` (R3 #6): a view sharing a ladder table's name silently absorbs `CREATE TABLE IF NOT EXISTS`, and this DB is proof the namespace is already shared in production.

## Durability split (decided, R3 recommendation, adopted by the integrator)

The open question was whether the sidecar-file domains (`settings`, the dedup tables, `fingerprints`, `file_hashes`) belong in the consolidated state.db ladder. It is now settled, and the line is **regenerable vs durable**, not "which file did legacy happen to use".

**Out of the ladder** -- `fingerprints` and `file_hashes` move to `apply_cache_migrations(conn)`, intended for a separate `cache.db`.

Rationale: both are pure derived data. Every row is recomputable from the audio file it describes, so **wiping the file is a legal recovery move**, and legacy already treats it that way -- `HashCache._ensure_schema` DROPs and recreates its whole table on a `PRAGMA user_version` mismatch (O-12). A durable ladder must never wipe. Keeping a domain whose legal recovery move IS a wipe inside the durable ladder makes that promise false: either the ladder eventually grows a wipe path (and durable rows become collateral), or the cache can never be reset without a migration it does not deserve. Splitting the file splits the semantics, and the ladder's claim about itself becomes honest.

**Stays in the ladder** -- `settings`, `duplicate_clusters`, `track_aliases`, `tag_provenance`.

Rationale: these hold **judgment, not derivation**. A duplicate cluster's canonical pick, an alias mapping, a tag's chosen provenance and the daemon's settings are decisions -- some of them the user's -- that cannot be recomputed from the audio. Losing them is data loss, not a cache miss. They were in separate files for historical reasons (each phase created its own sidecar), which is a fact about how the code grew, not about what the rows mean.

