# apps/database

Authored source of truth for `state.db`, the single local-first SQLite
database this repo's daemon and adapters read and write. This file is
hand-maintained and git-tracked. Its generated companion, produced fresh
next to the live DB on every machine, is `<data-root>/state/AGENTS.md` --
see "Where it lives per machine" and "Schema authority" below for how the
two relate.

Two-file pattern this repo follows (specs/cloudsync-spec.md section 2, D3):
this file carries WHY and narrative context; the generated file carries
WHAT, certified
against a specific machine's actual live columns at generation time. Nested
`AGENTS.md` is spec-conformant (agents.md: nearest-file-wins; OpenAI's own
monorepo ships 88 nested files this way).

## 0. Design intent

CloudSync needs one canonical database for library metadata, foreign-key
relationships, machine sync policy and provenance. A generated description
beside each database records its actual columns, while this authored file
explains the design and invariants. Per-machine policy distinguishes locally
pinned assets from cached, streamed or excluded assets; small analysis
metadata can sync independently of large audio and stem files.

Private founding correspondence is not included in this public copy.

## 1. What this database is

`state.db` is the canonical, local-first projection of the user's music
library: one SQLite file per machine holding every track, playlist,
analyzed field, sync-fleet policy, and piece of provenance the daemon and
webui need, with foreign keys enforced. It is derivative -- rebuildable from
vendor libraries (rekordbox, djay, Serato, Traktor) at any time -- not the
vendor libraries themselves. It is the first reference implementation of
the open-dj v0 strawman in this repo (apps/shared/state/__init__.py).

Historically the "where is everything" problem (the consolidation design
(cloudsync-spec.md section 1)) was that state was scattered across
this file, three other schema-writing modules, and half a dozen satellite
sqlite/JSON stores. This file, `apps/shared/state/schema.py`, and the
CLOUDSYNC consolidation (D2) are the answer: one database, one enforced
foreign-key graph, one generated description that cannot go stale.

## 2. Where it lives per machine

Per D1 (specs/cloudsync-spec.md), the canonical location is a per-machine
data root OUTSIDE any git checkout, resolved via the `MDT_DATA_DIR`
environment override (`apps/shared/platform_paths.py`):

- **macOS**: `~/Library/Application Support/music-dj-tools/` -- the
  platformdirs standard, and critically NOT iCloud-evictable the way
  `~/Documents` and `~/Desktop` are (a dataless placeholder there reads as a
  SILENTLY EMPTY sqlite file, never as an error). Time Machine covered.
- **Windows**: `%LOCALAPPDATA%\music-dj-tools\` for state; large
  stores (stems) may point elsewhere via per-machine sync policy.
- **DB file**: `<data-root>/state/state.db`. The generated AGENTS.md lives
  right beside it, at `<data-root>/state/AGENTS.md`.

Rejected locations, and why: inside the repo working tree (every worktree
gets an empty copy today -- the current failure mode this fixes); inside the
music library (mixes app state with user audio, different backup/relink
lifecycle); `~/Documents` (the iCloud dataless-placeholder hazard above).

The repo's own `data/` dir remains the fallback default for a fresh clone --
a machine only needs `MDT_DATA_DIR` set once it opts into the canonical
root. Worktrees on the same machine all point at the same root through that
one env var (FANOUT-CONVENTIONS.md already mandates `--data-dir`; this
makes it a single shell-profile setting instead of a per-invocation flag).

## 3. Schema authority

`apps/shared/state/schema.py` is the schema authority: `SCHEMA_VERSION`,
`MIGRATIONS`, and the three inventory tuples `TABLES` /
`FOREIGN_AUTHORITY_TABLES` / `INFRASTRUCTURE_TABLES` (merged into
`ALL_KNOWN_TABLES`) that this repo's own drift tests check a live DB
against. Current version is **v10**. Auth landed first and took **v6**
(`users`, `auth_sessions`); CLOUDSYNC's sync-safe schema
(specs/design_decision_05.md) yielded to **v7** (`machines`,
`sync_policies`, `playlist_pins`, `sync_state`, `hub_changelog`,
`local_changelog`); **v8** is analysis retention for audio we do not
have; **v9** is hub-authoritative machine enrollment
(specs/design_decision_12.md); **v10** is karaoke lyrics verdict
(specs/karaoke-lyrics-operational-plan.md D13.1). There is no reserved
next slot -- the next schema bump takes `SCHEMA_VERSION + 1`.

It is not YET the *only* schema authority, though D2 (cloudsync-spec.md)
plans to make it one. Three other modules create real tables in this same
SQLite file today, each for its own historical reason (usually: landing in
parallel with Phase 5 without wanting to touch its `MIGRATIONS` list and
cause merge churn):

- `apps/shared/pairings/schema_sql.py` -- `pairings`, `smartlists`
  (unversioned, `CREATE TABLE IF NOT EXISTS`, idempotent).
- `apps/shared/play_orders/schema.py` -- `play_orders`,
  `play_order_entries`, plus its own private `play_orders_schema_meta`
  version counter, so it never races `schema_meta`.
- `apps/launcher/scripts/bootstrap_db.py` -- `tracks_fts` (an fts5 virtual
  table, plus its own SQLite-managed shadow tables) and `tracks_frecency`,
  additive-only, ad hoc.

`schema.py` itself declares all of these in `FOREIGN_AUTHORITY_TABLES`
specifically so its own drift tripwire (a test that fails if any table
exists in a live DB that no schema authority declares) passes on a
fully-provisioned DB while still failing on a genuinely undeclared table.
Section 7 below documents every one of them too, for the same reason: an
agent reading this file should get the true, complete inventory, not an
aspirational one.

## 4. Sync semantics (summary)

Full contract: **specs/design_decision_04.md** -- read that file before
writing any code that touches a synced table. One-paragraph summary: sync
is hub-and-spoke over plain HTTP JSON, with agentbox as the always-on hub
(`/api/v1/sync/hello|push|pull`). It is state-based, not an oplog -- every
synced row carries `updated_at` + `origin_device_id`, and a plain per-row
last-writer-wins rule (`(updated_at, origin_device_id)` compared
lexicographically) resolves conflicts, both on the hub accepting a push and
on a spoke applying a pull. Deletes are soft (`deleted_at`, filtered by
every reader, never a real `DELETE`). Playlist membership syncs as a
whole-list replace, not row-level, because row-level LWW on an ordered list
produces interleaved garbage. Both sides verify convergence with a per-table
digest after every sync; a mismatch halts sync loudly rather than
self-repairing silently. Trust in v1 is tailnet membership only. See that
document's "Consequences the maintainer must accept" section for the sharp edges
(silent same-window conflict loss, clock-skew sensitivity, legacy rows
syncing as epoch-old until a seed push).

## 5. Invariants and gotchas

- **Foreign keys are always ON.** `apps/shared/state/db.py` sets `PRAGMA
  foreign_keys = ON` on every writable connection it hands out
  (`_RW_PRAGMAS`), and read-only connections set it too. Never open this
  file with a bare `sqlite3.connect` that skips that pragma -- an
  ad hoc connection with FKs off can silently insert orphan rows that the
  rest of the codebase assumes cannot exist.
- **Tombstone filtering is not optional.** Every table in the synced set
  (tracks, track_vendor_ids, track_fields, playlists,
  playlist_memberships, track_locations, plus the policy tables) has a
  `deleted_at` column and a delete is an ordinary UPDATE that sets it, not a
  `DELETE FROM`. Every reader MUST add `deleted_at IS NULL` or a deleted
  row on another machine reappears as a ghost on this one the moment sync
  runs. design_decision_04.md's own drift test greps for unfiltered
  readers; treat that as a standing tax on new code, not a one-time fix.
- **Honest denominators.** This repo's house rule (CLAUDE.md,
  docs/library-availability.md) applies with particular force to this
  database: never quote a coverage/match-rate figure against a denominator
  that silently includes rows that cannot possibly match. The sharpest
  example is `tracks.content_hash`: inspect the actual ingest implementation
  and available-audio denominator before reporting coverage. A count over
  all imported metadata rows can misrepresent a feature that requires local
  audio. Content-addressed operations must verify that the required hashes
  have actually been populated; schema presence alone does not establish
  content-hash availability.
- **Machine-local vs. hub-local vs. synced.** Three different scopes exist
  in this one file and mixing them up is the easiest mistake to make:
  - *Synced* (rides hub push/pull, carries `updated_at` /
    `origin_device_id` / `deleted_at`): tracks, track_vendor_ids,
    track_fields, playlists, playlist_memberships, track_locations,
    machines, sync_policies, playlist_pins.
  - *Machine-local* (exists on every machine independently, never leaves
    it): track_field_history, events, adapters, sync_state.
  - *Hub-local* (agentbox only; a spoke never has a live copy):
    hub_changelog.
  Writing hub-sync code against the wrong scope either leaks a machine's
  private audit trail to the fleet, or fails to converge data that was
  supposed to. design_decision_04.md's SYNC SET v1 is the authoritative
  list; this file's per-table prose below states each table's scope
  explicitly too.
- **`updated_at` is not `modified_at`, on the tables that have both.** On
  `track_fields`, `modified_at` is domain semantics (when the analyzed
  value changed) and `updated_at` is the sync protocol's conflict
  timestamp. A writer should stamp both together, but a reader asking
  "when did this value change" wants `modified_at`, and a reader asking
  "will this row win a sync conflict" wants `updated_at`.
- **v7 is the largest single migration this DB has had** -- one full
  table rebuild (`track_locations`, an INTEGER AUTOINCREMENT key that
  collides the instant two machines each mint a row 1, rebuilt to a minted
  uuid4-hex `location_id`) plus thirteen `ALTER TABLE ... ADD COLUMN`
  statements. Auth merged first and took **v6** (`users`, `auth_sessions`);
  CLOUDSYNC yielded and this rebuild lives in `_V7` (the temp table is
  still named `track_locations_v6` because the statements were written
  before the renumber). The live ladder is now **v10**; v6..v10 all run
  automatically, idempotently, on the next `open_rw()` against an older
  file. See design_decision_05.md for the consequence list, including
  that rows written before that migration have `NULL updated_at` and sync
  as epoch-old until a real edit or a seed push touches them.

## 6. See also

- specs/cloudsync-spec.md -- the CLOUDSYNC feature spec this file
  implements D3 of.
- specs/design_decision_04.md -- hub sync protocol (full detail for
  section 4 above).
- specs/design_decision_05.md -- migration v6 contract (full detail for
  the schema-authority claims in section 3 above).
- `apps/shared/state/schema.py` -- the actual DDL; read it "as committed"
  whenever this file and the code disagree, the code wins and this file is
  stale (regenerate the DB-side copy per section 2; file an issue against
  this file's own prose).
- `apps/database/column_docs.py` + `apps/database/generate_agents_md.py` --
  the generator that produces `<data-root>/state/AGENTS.md` beside the live
  DB. Run `python -m apps.database.generate_agents_md --data-dir
  <data-dir>` by hand any time; it is also invoked automatically after a
  successful migration when the data dir is writable.

## 7. Tables

One entry per table `apps/shared/state/schema.py` knows about -- its own
domain `TABLES` (v1 through v10), plus the seven real tables the other
three schema authorities write into this same file
(`FOREIGN_AUTHORITY_TABLES`, section 3 above), plus its own
migration-bookkeeping table (`INFRASTRUCTURE_TABLES`). Every column's
exact live type and the full curated per-column description are in the
generated file (`<data-root>/state/AGENTS.md`); this section is prose,
not a dictionary -- column-by-column detail lives in
`apps/database/column_docs.py`, the single source both files draw from.

### `tracks`

**Scope: synced.** Canonical per-track identity row -- one per song,
regardless of how many vendor libraries or playable locations reference it.
Primary key `stable_id` is a 40-char SHA-1 minted by
`apps.shared.state.ids.stable_id`, three-tiered by strength (`isrc` >
`fingerprint` > `inferred`; see `stable_id_tier`). Vendor-sourced facts
(title, artists_json, album, isrc, duration_ms) live directly on the row;
analyzed/inferred facts live in `track_fields` instead, with provenance.
`file_path` is the legacy single playable path; `track_locations` is the
real multi-location model and should be preferred by new code. `updated_at`
/ `origin_device_id` / `deleted_at` are the sync trio (v6). `content_hash`
is optional; inspect actual row availability before reporting coverage --
see the honest-denominators gotcha above.

### `track_vendor_ids`

**Scope: synced.** Opaque vendor identifiers for a track, keyed by
`(stable_id, vendor)`, so a change can round-trip back into the vendor's
own database without a vendor ID ever becoming this repo's primary key.
FK to `tracks(stable_id)` ON DELETE CASCADE.

### `track_fields`

**Scope: synced.** Provenance-wrapped analyzed values, EAV-shaped: one row
per `(stable_id, field_name)`. Mirrors the open-dj v0 strawman
`ProvenanceValue` envelope (`apps.shared.state.types`) -- `source` is
CHECK-constrained to the `SOURCES` vocabulary (mik, rekordbox, djay,
serato, traktor, open-dj-tool, manual, inferred, webui) and `confidence` is
an optional 0-1 score. FK to `tracks(stable_id)` ON DELETE CASCADE.

### `track_field_history`

**Scope: machine-local.** Append-only audit trail: every value a
`track_fields` cell has ever held moves here once superseded, keyed by a
surrogate AUTOINCREMENT `id` (the original composite PK could silently
collide when two rewrites landed in the same clock tick -- migration v1 to
v2). Deliberately excluded from the sync set (design_decision_04.md SYNC
SET v1): a full field-change history is a lot of row churn to ship around a
fleet for a view nothing currently needs cross-machine.

### `playlists`

**Scope: synced.** One row per vendor playlist, UNIQUE on `(vendor,
vendor_pl_id)` -- the same playlist re-ingested from the same vendor
updates in place; the same name from two different vendors is deliberately
two rows, since vendors do not share a playlist namespace. `updated_at`
changing is what triggers a whole-list membership replace on sync (see
`playlist_memberships`).

### `playlist_memberships`

**Scope: synced, whole-list replace.** Ordered track membership of a
playlist. PK is `(playlist_id, position)`, so a reorder rewrites positions
rather than moving rows between them. FKs to `playlists(playlist_id)` and
`tracks(stable_id)`, both ON DELETE CASCADE. Synced as a whole-list replace
keyed off `playlists.updated_at`, not row-level LWW -- row-level merge on an
ordered list produces interleaved garbage (design_decision_04.md).

### `adapters`

**Scope: machine-local.** Last-run bookkeeping, one row per adapter
(rekordbox, djay, serato, traktor, mik, ...): `last_run_at`, `last_ok`,
free-text `notes`. Reflects what actually ran on THIS machine, so it is not
part of the sync set.

### `events`

**Scope: machine-local.** Durable append-only event log backing the
in-process `EventBus` (`apps.shared.state.events`) -- the INFRA-01 bus
floor. `stable_id` is intentionally not FK-enforced (an event must survive
the track it concerns being deleted).

### `track_locations`

**Scope: synced.** Every playable location a track's audio can be found
at -- local disk, a remote URL, a venue-specific copy -- separate from the
single legacy `tracks.file_path`. Multiple rows per track are normal;
`role='primary'` is the one the play path prefers. Rebuilt in v6 from an
INTEGER AUTOINCREMENT `id` to a minted 32-hex-char `location_id`
specifically because this is THE cross-machine table and an autoincrement
key collides the instant two machines each mint a row 1
(design_decision_05.md section 3). `location_id` carries a
`DEFAULT (lower(hex(randomblob(16))))` so even a writer that forgets to
name the column cannot insert an unsyncable NULL-keyed row. FK to
`tracks(stable_id)` ON DELETE CASCADE; two partial UNIQUE indexes prevent
duplicate local paths / remote URLs per track.

### `users`

**Scope: machine-local identity, not authorization.** One Google account
that has signed in to the webui, keyed on the OIDC `sub` claim rather
than email -- an email can be reassigned, a sub cannot. Sign-in is
identity only: the app behaves identically whether or not anyone is
signed in. Landed as migration **v6** (auth took that slot; CloudSync
yielded to v7).

### `auth_sessions`

**Scope: machine-local.** One browser session for one user. Stores the
sha256 of the bearer session token, never the token itself, so a stolen
database cannot be replayed as a live cookie; also holds the Google
refresh/access token pair server-side so the browser never sees a Google
credential. FK to `users(google_sub)` ON DELETE CASCADE.

### `machines`

**Scope: synced.** Fleet registry -- one row per machine that has ever
synced, itself part of the synced set so any machine can see the whole
fleet, not just itself. `machine_id` (the PK) is minted once and stored
OUTSIDE this table, in a plain file at `<data-dir>/machine-id`
(`apps.shared.state.machine_identity`), specifically so a DB restored from
Litestream onto a different physical machine cannot inherit the old
machine's identity and impersonate it in `hub_changelog`. `name` is
UNIQUE and human-chosen (what the CloudSync config UI shows); `is_hub`
marks the one machine (agentbox) acting as sync hub.

### `sync_policies`

**Scope: synced.** Per-machine, per-asset-kind policy -- what this machine
keeps pinned, caches on demand, streams, or excludes entirely. PK is
`(machine_id, asset_kind)`; `asset_kind` is one of audio, stem_bundle,
anlz_cache, vocal_cache, lyrics_cache, karaoke_words (the last two joined
the CHECK in the schema v10 table rebuild; the list lives once, in
`apps/shared/state/migrations_v10.ASSET_KIND_CHECK_VALUES`); `mode` is one
of pinned, cached, stream, excluded.
FK to `machines(machine_id)` ON DELETE CASCADE. This is what the CloudSync
config surface's quick toggles read and write (cloudsync-spec.md D4/D5).

### `playlist_pins`

**Scope: synced.** Per-machine, per-playlist override of `sync_policies` --
e.g. a gig playlist stays pinned locally on gig machines even though the
machine's general audio policy is `stream`. PK is `(machine_id,
playlist_id)`; same `mode` vocabulary as `sync_policies`. FKs to
`machines(machine_id)` and `playlists(playlist_id)`, both ON DELETE
CASCADE.

### `sync_state`

**Scope: machine-local.** This machine's own push/pull watermarks against
each sync peer (`last_push_seq`, `last_pull_seq`, `last_sync_at`), keyed by
`peer`, plus `peer_generation` -- the generation token that peer reported at
the last completed sync. A different token next time means that peer's
database moved backwards (a Litestream restore), so both watermarks reset
and this machine re-offers its library; keying that on the token rather than
on the peer's `MAX(seq)` is what stops a routine changelog prune from
looking like a restore. Never itself synced -- syncing your own sync
watermarks would be incoherent.

### `hub_changelog`

**Scope: hub-local (agentbox only).** Monotonic log of every row the hub
has accepted from a push, used to hand each spoke only the rows newer than
its pull watermark. AUTOINCREMENT on `seq` is correct here specifically
because this table never crosses a machine boundary -- `seq` IS the pull
watermark, unlike the collision `track_locations` had before v6.

### `local_changelog`

**Scope: machine-local (spoke).** Twin of `hub_changelog`
(design_decision_08.md point 3): same shape, appended by
`apps.shared.state.sync_stamp` on every write to a synced table.
`sync_state.last_push_seq` fences against this table's `seq`, replacing
the wall-clock push watermark that lost rows under clock skew. Never
crosses a machine boundary and never rides hub sync itself. Landed in
**v7** with the rest of the CloudSync ladder.

### `sync_write_tokens`

**Scope: machine-local (v21).** One row per digested sync table
(`table_name`, `token`), where `token` is a 16-byte random blob that an
AFTER INSERT/UPDATE/DELETE trigger on that table replaces on every row
write (`apps.shared.state.migrations_v21`). It is the key that lets
`apps.sync_hub.digest_gate` reuse a CloudSync digest on a no-op sync
(issue #4396). The token moves even for a write that bypassed the
changelog. It is random rather than a counter, so a rolled-back write can
never hand its value to a later one. A missing or altered trigger, or a
missing row, disables the gate and every digest is walked in full. Never
synced: tokens describe this file's writes, not library content.

### `track_availability`

**Scope: machine-local (v8).** One row per `stable_id` recording whether
its audio is present on disk right now, distinct from the analyzed values
that stay in `track_fields` regardless. Safe-default views
(`tracks_available`, `tracks_unavailable`, `track_fields_available`) read
this as a dimension so an aggregate cannot silently include unplayable
rows. See the honest-denominators gotcha above.

### `path_availability`

**Scope: machine-local (v18).** A cache of stat answers for rekordbox
library paths (materialized size, NULL for not on disk, and `checked_at`),
keyed by `(resolver_namespace, logical_path)`. The namespace hashes the
active path map plus this machine's id, so a path-map change or another
machine never reads these rows. Listing hydration serves it under a
per-request stat budget and a background refresher updates it (issue
#1037, PERF-RB-01). Not a verdict like `track_availability`: a row older
than the TTL is stale, and a path with no row is unprobed, never absent.
The refresher only writes into a state.db that already exists.

### `unmatched_source_analysis`

**Scope: machine-local (v8).** Staging for an analyzed source row (MIK,
rekordbox, ...) that matches no `tracks` row at all, so there is no
`stable_id` to hang a `track_fields` row on. `unmatched_reason` records
why; `promoted_stable_id` is the one-way door into `track_fields` once a
match is later found (`docs/analysis-retention.md`).

### `track_energy_segments`

**Scope: machine-local (v8).** Time-series destination for a source's
energy-over-time data (e.g. MIK's ZENERGYSEGMENT), in milliseconds per
the repo's time-series-vs-scalar convention. `track_fields` is one row
per `(stable_id, field_name)` and cannot hold a series.

### `analysis_field_verification`

**Scope: machine-local (v8).** How each field was verified, stored next
to the values it produced: cross-source agreement, a one-sided
single-source probe, or unverified. Written by
`apps.mik.load.record_verification` from
`apps.shared.equivalence.EquivalenceGate.provenance_rows`.

### `machine_owners`

**Scope: hub-authoritative, outside the sync set (v9, specs/design_decision_12.md).**
Which user owns which machine, one live row per machine. Ownership is
asserted only by a call that carried a live credential to the hub holding
the row, so a restored or hostile spoke cannot push itself an owner under
last-writer-wins. Separate from `machines` so the machines snapshot peers
exchange cannot carry a forged ownership claim.

### `enrollment_grants`

**Scope: hub-local (v9).** Short-lived single-use credentials for the DEV
enrollment path: an operator with an authenticated session on the hub
mints one, carries it to the machine that is joining, and that machine
spends it at `POST /api/v1/sync/enroll`. Only the sha256 is stored, never
the redeemable value, on the same reasoning as
`auth_sessions.session_token_sha256`.

### `lyric_verdict`

**Scope: synced (v10, specs/karaoke-lyrics-operational-plan.md D13.1).**
One row per track holding the karaoke lyrics verdict (vocal, sparse,
no-lyrics or unknown), the human override that survives a recompute, the
alignment provenance, and the sha256 of the track's karaoke_words
artifact. Deletes are tombstones only (the licensing purge sets
`deleted_at`, it never `DELETE`s).

### `schema_meta`

**Scope: infrastructure, not domain data.** Migration bookkeeping for
`apps.shared.state.schema.apply_migrations` -- one row per applied schema
version, with the timestamp it was applied. Deliberately excluded from
`TABLES` (schema.py's own `INFRASTRUCTURE_TABLES`).

### `pairings` (owned by `apps/shared/pairings/schema_sql.py`)

**Scope: not yet declared by design_decision_04.md's sync set** (predates
it; D2 will fold this authority into the single schema, at which point its
sync scope becomes an explicit decision rather than an omission). Pairing-
memory edge graph (CAT-03): which tracks the user likes mixing into/out of
which other tracks, `direction` and `source` CHECK-constrained,
`from_stable_id`/`to_stable_id` not FK-enforced (this module predates the
shared FK convention).

### `smartlists` (owned by `apps/shared/pairings/schema_sql.py`)

**Scope: not yet in the sync set** (see `pairings` above). Saved smart-
playlist rules (SMART-01/02): a JSON rule AST (`rule`) plus the last
materialisation result (`last_evaluated_at`,
`last_materialized_track_ids`), so the evaluator and the materialiser share
one write surface. `referenced_fields` is a JSON list of
`track_fields.field_name` values the rule reads, letting a field-changed
event know which smartlists to re-evaluate without parsing the rule AST
itself.

### `play_orders` (owned by `apps/shared/play_orders/schema.py`)

**Scope: not yet in the sync set** (see `pairings` above). A generated
track sequence for a playlist (PLAY-01/03), e.g. an AI-suggested set order.
UNIQUE on `(playlist_id, name)` -- a playlist can have several named
orders. Migrated by its own private `play_orders_schema_meta` counter so it
never races `schema_meta`.

### `play_order_entries` (owned by `apps/shared/play_orders/schema.py`)

**Scope: not yet in the sync set.** One row per track position within a
`play_orders` sequence, plus optional transition targets for that slot
(`target_key`, `target_tempo`, `key_sync`, `transition_hint`). FK to
`play_orders(id)` ON DELETE CASCADE; UNIQUE on `(play_order_id, position)`.

### `play_orders_schema_meta` (owned by `apps/shared/play_orders/schema.py`)

**Scope: infrastructure, not domain data.** Private migration-version
counter for `play_orders`, kept separate from `schema_meta` specifically so
the two migration frameworks never contend on the same counter.

### `tracks_fts` (owned by `apps/launcher/scripts/bootstrap_db.py`)

**Scope: not yet in the sync set; regenerable.** fts5 full-text index over
a subset of `tracks`' searchable columns (title, artist, album, genre,
key, tags), maintained by the launcher's quick-open palette. Populated by
an explicit `INSERT INTO tracks_fts(rowid, ...)` at backfill time rather
than an external-content trigger, so a missed backfill call leaves it
silently stale rather than erroring -- worth remembering the next time a
launcher search looks wrong after a bulk write. `tags` is currently
backfilled as an empty string, reserved for the Phase 6 tag-unification
work. SQLite itself also creates several shadow tables alongside this one
(`tracks_fts_data`, `_idx`, `_docsize`, `_config`, `_content`) purely for
its own index storage; they are declared in `schema.py`'s
`FOREIGN_AUTHORITY_TABLES` for the drift tripwire's sake but carry no
application-meaningful columns, so the generated AGENTS.md omits them.

### `tracks_frecency` (owned by `apps/launcher/scripts/bootstrap_db.py`)

**Scope: not yet in the sync set; regenerable.** Frequency+recency ranking
signal for the launcher's quick-open palette -- `plays`, `drags`,
`last_played_at`, `last_dragged_at` (the latter two are Unix-epoch
integers, not RFC 3339, unlike the rest of this database). Ranked by drags
first (`idx_frecency_drags`: `ORDER BY drags DESC, last_dragged_at DESC`).
No FK to `tracks` -- the launcher module links the two softly in code
rather than at the schema level, because it does not own `tracks` and
cannot add a `REFERENCES` clause to a table a different schema authority
creates.
