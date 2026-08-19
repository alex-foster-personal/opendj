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
| `playlist_memberships` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | no index, O-10 |
| `adapters` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | no index, O-10 |
| `events` | `apps/shared/state/schema.py` | `apply_migrations` (v1) | name collides, O-2 |
| `track_locations` | `apps/shared/state/schema.py` | `apply_migrations` (v4/v5) | data backfill, O-14 |
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
| `fingerprints` | `apps/shared/fingerprints.py` | `FingerprintCache.__init__` | shape collides, O-3 |
| `file_hashes` | `apps/shared/hashing.py` | `HashCache._ensure_schema` | no migration path, O-12 |
| `tracks_fts` | `apps/launcher/scripts/bootstrap_db.py` | `apply_launcher_migration` | name collides, O-4 |
| `tracks_frecency` | `apps/launcher/scripts/bootstrap_db.py` | `apply_launcher_migration` | FK drift, O-5 |
| `schema_meta` | `apps/shared/state/schema.py` | `_ensure_meta` | infra, created by the runner |

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

**O-10. Tables with no index:** `adapters`, `file_hashes`, `play_orders_schema_meta`, `playlist_memberships`, `playlists`, `sets`, `settings`, `spotify_playlist_meta`, `track_fields`, `tracks_fts`. Most are covered by their PK. Two look like real gaps: `playlist_memberships` is queried by `stable_id` but has PK `(playlist_id, position)`, so "which playlists is this track in" is a full scan; and `spotify_playlist_meta` is reached from `playlists` by `vendor_pl_id` with no index on it.

**O-11. Inconsistent partial-index policy on the same concept.** Shared-state uses `... WHERE stable_id IS NOT NULL` (`idx_events_stable_id`); `apps/analysis/store.py` indexes the same nullable concept with no predicate (`idx_analysis_events_stable`).

**O-12. `file_hashes` has no migration path.** `HashCache._ensure_schema` reads `PRAGMA user_version` and on mismatch DROPs and recreates -- a silent full cache wipe. Also the only `CREATE TABLE` in the tree without `IF NOT EXISTS`.

**O-13. Hand-rolled ALTER outside any ladder.** `apps/dedup/schema.py:ensure_schema` probes `PRAGMA table_info(duplicate_clusters)` and issues `ALTER TABLE ... ADD COLUMN flagged_manual_review` if absent -- a migration with no version number, re-probed on every open.

**O-14. Data-bearing migration steps hide inside DDL lists.** Shared-state `_V4` ends with `INSERT OR IGNORE INTO track_locations ... SELECT ... FROM tracks`. A consolidation replaying only `CREATE` statements would build the right shape with the wrong contents; the runner replays it on adoption and a test proves it.

**O-15. `apps/shared/state/schema.py` has `_V5 = _V4`.** v5 re-runs v4 verbatim because a live agentbox DB was stamped at v4 out-of-band without the tables existing -- a repair step disguised as a version bump. This is why adoption verifies object existence rather than trusting the counter.

## Runner version numbering

Shares `schema_meta` but stamps at `VERSION_OFFSET = 1000`: rows 1..5 are legacy, `1000` is the `ADOPTION_VERSION` marker (this file predates consolidation), `1001` is consolidated v1. Fresh DBs get 1..5 stamped (so the legacy runner no-ops) plus 1001 and no 1000 row. `apply_migrations` refuses below `MIN_ADOPTABLE_LEGACY_VERSION = 3`.

## Open decision (integrator review)

The sidecar-file domains (`settings`, dedup tables, `fingerprints`, `file_hashes`) are currently in the consolidated state.db ladder, matching the inventory brief. If the rebuild intends those to stay in separate DB files, they move out of `_V1` into their own exported DDL groups -- a small change; the test's per-domain structure already supports it.
