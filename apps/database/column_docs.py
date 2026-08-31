"""Curated table/column descriptions merged into the generated AGENTS.md.

Plain dicts -- not a database, not a class hierarchy. :mod:`apps.database.
generate_agents_md` merges these against live sqlite introspection so
``<data-root>/state/AGENTS.md`` cannot silently drift from the schema it
describes: a live column with no entry here fails the generator loudly (see
:class:`apps.database.generate_agents_md.MissingColumnDocsError`), the drift
guard specs/cloudsync-spec.md D3 asks for.

Two dicts:
- ``TABLE_DOCS``: one short paragraph of purpose per table.
- ``COLUMN_DOCS``: ``table -> {column: one-line meaning}``.

Coverage: every table :mod:`apps.shared.state.schema` knows about --
``TABLES`` (the fourteen this module actually creates), the seven real
tables it declares in ``FOREIGN_AUTHORITY_TABLES`` but does not create
(``pairings``, ``smartlists``, ``play_orders``, ``play_order_entries``,
``play_orders_schema_meta``, ``tracks_fts``, ``tracks_frecency``), and
``schema_meta`` from ``INFRASTRUCTURE_TABLES`` -- twenty-two tables in all.
The five fts5 shadow tables in ``FOREIGN_AUTHORITY_TABLES``
(``tracks_fts_config/_content/_data/_docsize/_idx``) are deliberately NOT
documented here: :mod:`apps.database.generate_agents_md` excludes them
structurally, because they are sqlite's own opaque index storage for
``tracks_fts``, not application columns a person or agent should read about.

Column lists here are transcribed straight from the DDL that creates each
table (``apps/shared/state/schema.py``, ``apps/shared/pairings/
schema_sql.py``, ``apps/shared/play_orders/schema.py``,
``apps/launcher/scripts/bootstrap_db.py``), so a mismatch here means a typo,
not schema drift -- the generator's live introspection is what actually
guards against drift; this file only supplies the meaning.
"""

from __future__ import annotations

TABLE_DOCS: dict[str, str] = {
    # ----- apps.shared.state.schema.TABLES (the schema authority) --------
    "tracks": (
        "Canonical per-track identity row: one per song, regardless of how "
        "many vendor libraries or playable locations reference it."
    ),
    "track_vendor_ids": (
        "Opaque vendor-library identifiers for a track, keyed by "
        "(stable_id, vendor). Lets a writer round-trip a change back to the "
        "vendor's own database (a rekordbox djmdContent id, a Serato crate "
        "entry, etc.) without a vendor id ever becoming the primary key."
    ),
    "track_fields": (
        "Provenance-wrapped analysed values, EAV-shaped: one row per "
        "(stable_id, field_name), e.g. bpm, key, energy. Mirrors the "
        "open-dj v0 strawman ProvenanceValue envelope "
        "(apps.shared.state.types) -- this is where per-value confidence "
        "and source live, separate from the vendor-sourced facts on tracks."
    ),
    "track_field_history": (
        "Append-only audit trail: every value a track_fields cell has ever "
        "held moves here once superseded. Machine-local -- design_decision_"
        "04.md's SYNC SET v1 explicitly excludes it, so it carries no "
        "updated_at / origin_device_id / deleted_at."
    ),
    "playlists": (
        "One row per vendor playlist, unique on (vendor, vendor_pl_id): "
        "re-ingesting the same playlist from the same vendor updates in "
        "place, but the same name in two vendors is deliberately two rows "
        "-- vendors do not share a playlist namespace."
    ),
    "playlist_memberships": (
        "Ordered track membership of a playlist. PK is (playlist_id, "
        "position), so reordering rewrites positions rather than moving "
        "rows between them. Synced as a whole-list replace, not row-level "
        "LWW (design_decision_04.md, PLAYLIST MEMBERSHIP) -- row-level "
        "merge on an ordered list produces interleaved garbage."
    ),
    "adapters": (
        "Last-run bookkeeping, one row per adapter (rekordbox, djay, "
        "serato, traktor, mik, ...). Not part of the sync set -- each "
        "machine's adapters reflect what actually ran on that machine."
    ),
    "events": (
        "Durable append-only event log backing the in-process EventBus "
        "(apps.shared.state.events), the INFRA-01 bus floor. Machine-local "
        "-- never rides hub sync (design_decision_04.md SYNC SET v1)."
    ),
    "track_locations": (
        "Every playable location a track's audio can be found at -- local "
        "disk, a remote URL, a specific venue's copy -- distinct from the "
        "single legacy tracks.file_path. Multiple rows per track are "
        "normal; role='primary' is the one the play path prefers. Rebuilt "
        "in v6 from an INTEGER AUTOINCREMENT key to a minted uuid4-hex "
        "location_id specifically because this is THE cross-machine table "
        "and an autoincrement key collides the instant two machines each "
        "mint a row 1 (design_decision_05.md, section 3)."
    ),
    "machines": (
        "Fleet registry: one row per machine that has ever synced. Part of "
        "the synced set itself (design_decision_04.md SYNC SET v1) so any "
        "machine can see the whole fleet, not just itself."
    ),
    "sync_policies": (
        "Per-machine, per-asset-kind policy: what this machine keeps "
        "pinned, caches on demand, streams, or excludes entirely. PK is "
        "(machine_id, asset_kind) -- the CloudSync config surface's quick "
        "toggles write here (cloudsync-spec.md D4/D5)."
    ),
    "playlist_pins": (
        "Per-machine, per-playlist pin override -- e.g. keep a gig "
        "playlist's audio always local on gig machines even though the "
        "machine's general audio policy is 'stream'. PK is (machine_id, "
        "playlist_id)."
    ),
    "sync_state": (
        "Machine-local watermarks for the hub sync protocol: how far this "
        "machine has pushed to, and pulled from, each peer. Never synced "
        "itself -- syncing your own sync watermarks would be incoherent "
        "(design_decision_04.md, CHANGE MODEL)."
    ),
    "hub_changelog": (
        "Hub-local (agentbox-only) monotonic log of every row accepted "
        "from a push, used to hand each spoke only the rows newer than its "
        "pull watermark. AUTOINCREMENT is correct here specifically "
        "because this table never crosses a machine boundary -- seq IS the "
        "pull watermark."
    ),
    # ----- apps.shared.state.schema.INFRASTRUCTURE_TABLES -----------------
    "schema_meta": (
        "Migration bookkeeping for apps.shared.state.schema.apply_"
        "migrations -- infrastructure, not domain data. One row per "
        "applied schema version."
    ),
    # ----- apps.shared.state.schema.FOREIGN_AUTHORITY_TABLES --------------
    # Real tables other modules write into this same file. D2
    # (specs/cloudsync-spec.md) plans to fold these into MIGRATIONS in a
    # later round; until then they are documented here so a
    # fully-provisioned state.db does not break the generator.
    "pairings": (
        "Pairing-memory edge graph (CAT-03): which tracks the user likes "
        "mixing into/out of which other tracks. Owned by "
        "apps.shared.pairings, not apps.shared.state.schema -- lives in "
        "the same file to avoid a second sqlite connection "
        "(apps/shared/pairings/schema_sql.py)."
    ),
    "smartlists": (
        "Saved smart-playlist rules (SMART-01/02): a JSON rule AST plus "
        "the last materialisation result, so the evaluator and the "
        "materialiser share one write surface (apps/shared/pairings/"
        "schema_sql.py)."
    ),
    "play_orders": (
        "A generated track sequence for a playlist (PLAY-01/03), e.g. an "
        "AI-suggested set order. Migrated by its own private "
        "play_orders_schema_meta counter so it does not race schema_meta "
        "(apps/shared/play_orders/schema.py)."
    ),
    "play_order_entries": (
        "One row per track position within a play_orders sequence, plus "
        "optional transition targets for that slot "
        "(apps/shared/play_orders/schema.py)."
    ),
    "play_orders_schema_meta": (
        "Private migration-version counter for play_orders, kept separate "
        "from schema_meta so the two migration frameworks never contend "
        "on the same counter (apps/shared/play_orders/schema.py)."
    ),
    "tracks_fts": (
        "FTS5 full-text index over a subset of tracks' searchable columns, "
        "maintained by the launcher quick-open palette "
        "(apps/launcher/scripts/bootstrap_db.py). Populated by an explicit "
        "INSERT INTO tracks_fts(rowid, ...) at backfill time rather than "
        "external-content triggers, so a missed backfill call leaves it "
        "silently stale rather than erroring."
    ),
    "tracks_frecency": (
        "Frequency+recency ranking signal for the launcher's quick-open "
        "palette: how often and how recently a track was played or "
        "dragged. No FK to tracks -- launcher code links the two tables "
        "softly rather than at the schema level "
        "(apps/launcher/scripts/bootstrap_db.py)."
    ),
}


COLUMN_DOCS: dict[str, dict[str, str]] = {
    "tracks": {
        "stable_id": (
            "Primary key. 40-char lowercase hex SHA-1 minted by "
            "apps.shared.state.ids.stable_id from ISRC, "
            "fingerprint+duration+size, or path+mtime, in that preference "
            "order (see stable_id_tier)."
        ),
        "stable_id_tier": (
            "Which of the three stable_id derivations produced this row's "
            "key: 'isrc' (strongest; two tracks sharing an ISRC "
            "intentionally collide -- that is the archival property), "
            "'fingerprint', or 'inferred' (path+mtime, last resort, "
            "re-keyed by dedup when a better signal appears)."
        ),
        "title": "Track title, vendor-sourced.",
        "artists_json": (
            "JSON-encoded list of artist name strings; not a normalised artist table."
        ),
        "album": "Album name, vendor-sourced.",
        "isrc": (
            "Raw ISRC as read from the vendor library, pre-normalisation "
            "-- normalisation only happens inside ids.stable_id."
        ),
        "duration_ms": "Track duration in milliseconds.",
        "file_path": (
            "Legacy primary playable path, pre-dating track_locations. New "
            "code should prefer a track_locations row with role='primary'; "
            "this column is not removed because ingest still writes it and "
            "migration v4 seeded track_locations from it."
        ),
        "content_hash": (
            "Content-addressed hash of the audio file, for cross-machine "
            "dedup and future R2 keying. NULL on all 9,986 rows as of "
            "Fri 28 Aug 2026 (apps/shared/state/ingest/rekordbox.py:273 "
            "does not populate it) -- this blocks every content-addressed "
            "sync operation until the CLOUDSYNC content-hash-backfill "
            "phase runs (cloudsync-spec.md section 4)."
        ),
        "created_at": "RFC 3339 UTC timestamp of first insert. Never rewritten.",
        "updated_at": (
            "RFC 3339 UTC timestamp of the most recent write to this row. "
            "Also this table's LWW sync timestamp (design_decision_04.md, "
            "CONFLICT RULE)."
        ),
        "origin_device_id": (
            "machine_id of the machine that made the most recent write. "
            "NULL on rows written before migration v6 -- such rows sync as "
            "epoch-old until a real edit or the seed push touches them "
            "(design_decision_04.md consequence 7)."
        ),
        "deleted_at": (
            "RFC 3339 UTC tombstone timestamp; NULL means live. A synced "
            "delete is an ordinary update that sets this column and "
            "propagates like any other write -- readers MUST filter "
            "deleted_at IS NULL or ghosts reappear (design_decision_04.md, "
            "DELETES)."
        ),
    },
    "track_vendor_ids": {
        "stable_id": "FK -> tracks(stable_id), ON DELETE CASCADE.",
        "vendor": (
            "Vendor name, e.g. 'rekordbox', 'djay', 'serato', 'traktor', "
            "'mik'. Free text, not CHECK-constrained (compare to "
            "track_fields.source, which is)."
        ),
        "vendor_id": (
            "The vendor's own opaque identifier for this track (its "
            "native primary key or equivalent)."
        ),
        "updated_at": "Sync LWW timestamp; added in v6, NULL on pre-v6 rows.",
        "origin_device_id": "Writing machine's machine_id; added in v6.",
        "deleted_at": "Tombstone timestamp; added in v6. NULL = live.",
    },
    "track_fields": {
        "stable_id": "FK -> tracks(stable_id), ON DELETE CASCADE.",
        "field_name": (
            "Analysed field name, e.g. 'bpm', 'key', 'energy'. Free text "
            "-- the vocabulary lives in application code, not a CHECK "
            "constraint."
        ),
        "value_json": (
            "The field's value, JSON-encoded so it can hold a number, string, or structured value."
        ),
        "source": (
            "Which system produced this value. CHECK-constrained to "
            "apps.shared.state.types.SOURCES: mik, rekordbox, djay, "
            "serato, traktor, open-dj-tool, manual, inferred, webui."
        ),
        "confidence": (
            "Optional 0-1 confidence score for inferred/analysed values; "
            "NULL for vendor facts that carry no confidence signal."
        ),
        "modified_at": (
            "RFC 3339 UTC timestamp this specific field value was last "
            "written. Superseded values move to track_field_history."
        ),
        "updated_at": (
            "Sync LWW timestamp; added in v6. Distinct from modified_at -- "
            "modified_at is domain semantics (when the value changed), "
            "updated_at is the sync protocol's conflict-resolution "
            "timestamp; in practice a writer stamps both together."
        ),
        "origin_device_id": "Writing machine's machine_id; added in v6.",
        "deleted_at": "Tombstone timestamp; added in v6. NULL = live.",
    },
    "track_field_history": {
        "id": (
            "Surrogate AUTOINCREMENT primary key. The original PK "
            "(stable_id, field_name, superseded_at) could silently "
            "collide when two rewrites landed in the same clock tick "
            "(migration v1 -> v2, schema.py comment [I1]); id makes "
            "history truly append-only."
        ),
        "stable_id": (
            "The track this history row concerns. Not a live FK to tracks "
            "-- history intentionally outlives a track's current field "
            "row."
        ),
        "field_name": "Which field's value this history row records.",
        "value_json": "The value as it stood before being superseded.",
        "source": "Who produced that value, at the time.",
        "confidence": "Confidence score at the time, if any.",
        "modified_at": "When that value was originally written.",
        "superseded_at": "When it was overwritten by the next value.",
    },
    "playlists": {
        "playlist_id": "Primary key, minted at ingest.",
        "name": "Playlist display name.",
        "vendor": "Owning vendor: rekordbox, djay, serato, traktor, etc.",
        "vendor_pl_id": ("The vendor's own playlist identifier. UNIQUE together with vendor."),
        "created_at": "First-ingest timestamp.",
        "updated_at": (
            "Most recent write; also the sync LWW timestamp and, per "
            "design_decision_04.md PLAYLIST MEMBERSHIP, the trigger for a "
            "whole-list membership replace on sync."
        ),
        "origin_device_id": "Writing machine's machine_id; added in v6.",
        "deleted_at": "Tombstone timestamp; added in v6. NULL = live.",
    },
    "playlist_memberships": {
        "playlist_id": "FK -> playlists(playlist_id), ON DELETE CASCADE.",
        "stable_id": "FK -> tracks(stable_id), ON DELETE CASCADE.",
        "position": (
            "Ordinal within the playlist, vendor-native ordering. Part of "
            "the primary key together with playlist_id."
        ),
        "updated_at": (
            "Added in v6. Membership rows are replaced wholesale on sync, "
            "so this column matters less than playlists.updated_at, which "
            "is the actual sync trigger."
        ),
        "origin_device_id": "Writing machine's machine_id; added in v6.",
        "deleted_at": "Tombstone timestamp; added in v6. NULL = live.",
    },
    "adapters": {
        "adapter_id": "Adapter name, primary key.",
        "last_run_at": (
            "RFC 3339 UTC timestamp of the adapter's most recent run, or NULL if it has never run."
        ),
        "last_ok": (
            "1 if the last run succeeded, 0 otherwise. Defaults to 0 -- an "
            "adapter that has never run has not proven itself ok."
        ),
        "notes": "Free-text detail from the last run, e.g. an error message.",
    },
    "events": {
        "id": "Surrogate AUTOINCREMENT primary key.",
        "ts": "RFC 3339 UTC timestamp the event was emitted.",
        "kind": "Event type/name, free text (e.g. 'track.field_written').",
        "stable_id": (
            "The track the event concerns, if any. Not FK-enforced -- "
            "events must survive a track's own deletion."
        ),
        "payload_json": "Event payload, JSON-encoded.",
        "actor": ("Who/what emitted the event (adapter name, 'webui', a user, etc.), if known."),
    },
    "track_locations": {
        "location_id": (
            "Primary key. 32-char lowercase hex, DEFAULT "
            "(lower(hex(randomblob(16)))) so even a writer that forgets to "
            "name the column cannot insert an unsyncable NULL-keyed row "
            "(design_decision_05.md, reading 2)."
        ),
        "stable_id": "FK -> tracks(stable_id), ON DELETE CASCADE.",
        "kind": "'local' (a file path on this machine) or 'remote' (a URL).",
        "role": (
            "'primary' (the play path prefers this one) or 'alternate'. Defaults to 'alternate'."
        ),
        "file_path": (
            "Filesystem path, when kind='local'. Exactly one of "
            "file_path/remote_url must be non-empty (table CHECK)."
        ),
        "remote_url": (
            "URL, when kind='remote'. Exactly one of file_path/remote_url "
            "must be non-empty (table CHECK)."
        ),
        "venue_key": (
            "Optional identifier for the venue/machine context this "
            "location is scoped to (a gig laptop's local copy, for "
            "instance)."
        ),
        "venue_rank": (
            "Optional ordering hint among multiple venue-scoped locations for the same track."
        ),
        "available": (
            "1 if this location was last probed as reachable/playable, 0 "
            "otherwise. Defaults to 0 -- unprobed is not assumed "
            "available."
        ),
        "probed_at": (
            "RFC 3339 UTC timestamp of the last availability probe, or NULL if never probed."
        ),
        "content_hash": (
            "Content-addressed hash of the file at this location, for "
            "cross-machine dedup and future R2 keying."
        ),
        "created_at": "First-insert timestamp.",
        "updated_at": "Most recent write; also the sync LWW timestamp.",
        "origin_device_id": "Writing machine's machine_id; added in v6.",
        "deleted_at": "Tombstone timestamp; added in v6. NULL = live.",
    },
    "machines": {
        "machine_id": (
            "Primary key. uuid4 hex minted once at first run and stored "
            "OUTSIDE this table, in a plain file at <data-dir>/machine-id "
            "(apps.shared.state.machine_identity) -- so a DB restored from "
            "Litestream onto a different physical machine cannot inherit "
            "the old machine's identity and start impersonating it in "
            "hub_changelog."
        ),
        "name": (
            "Friendly, human-chosen machine name (e.g. 'silver', "
            "'agentbox'). UNIQUE -- this is what a human picks in the "
            "CloudSync config UI; hostname stays display-only elsewhere."
        ),
        "platform": "CHECK IN ('macos', 'windows', 'linux').",
        "is_hub": (
            "1 if this machine is the sync hub (agentbox, per "
            "design_decision_04.md), 0 otherwise. Defaults to 0."
        ),
        "data_root": (
            "The data directory this machine reported at registration (its "
            "MDT_DATA_DIR-resolved root), for operator visibility only -- "
            "never used to resolve a path on a different machine."
        ),
        "first_seen": "RFC 3339 UTC timestamp of this machine's first registration.",
        "last_seen": (
            "RFC 3339 UTC timestamp of this machine's most recent registration/heartbeat."
        ),
    },
    "sync_policies": {
        "machine_id": "FK -> machines(machine_id), ON DELETE CASCADE.",
        "asset_kind": ("CHECK IN ('audio', 'stem_bundle', 'anlz_cache', 'vocal_cache')."),
        "mode": (
            "CHECK IN ('pinned', 'cached', 'stream', 'excluded') -- "
            "pinned is always-local, cached is opportunistic with LRU "
            "eviction, stream fetches only on play, excluded never "
            "touches this machine."
        ),
        "cache_budget_mb": (
            "Optional cap on local cache size for this asset_kind on this "
            "machine, in megabytes. NULL means unbounded."
        ),
        "updated_at": "Sync LWW timestamp.",
        "origin_device_id": (
            "Writing machine's machine_id -- note this is the machine "
            "that SET the policy, not necessarily the machine the policy "
            "is FOR (that is machine_id, the FK column)."
        ),
        "deleted_at": "Tombstone timestamp. NULL = live.",
    },
    "playlist_pins": {
        "machine_id": "FK -> machines(machine_id), ON DELETE CASCADE.",
        "playlist_id": "FK -> playlists(playlist_id), ON DELETE CASCADE.",
        "mode": (
            "CHECK IN ('pinned', 'cached', 'stream', 'excluded'), same "
            "vocabulary as sync_policies.mode. A playlist_pins row "
            "overrides the machine's general sync_policies row for tracks "
            "in that playlist."
        ),
        "updated_at": "Sync LWW timestamp.",
        "origin_device_id": "Writing machine's machine_id.",
        "deleted_at": "Tombstone timestamp. NULL = live.",
    },
    "sync_state": {
        "peer": (
            "Primary key -- identifies the sync peer these watermarks are "
            "against (currently always the hub, e.g. 'hub' or the hub's "
            "machine_id)."
        ),
        "last_push_seq": (
            "Highest hub_changelog.seq this machine has successfully "
            "pushed past, i.e. its push watermark. Defaults to 0."
        ),
        "last_pull_seq": (
            "Highest hub_changelog.seq this machine has pulled, i.e. its "
            "pull watermark. Defaults to 0."
        ),
        "last_sync_at": (
            "RFC 3339 UTC timestamp of the last successful sync with this "
            "peer, or NULL if never synced."
        ),
    },
    "hub_changelog": {
        "seq": (
            "Primary key, AUTOINCREMENT. Monotonically increasing hub "
            "sequence number; the watermark spokes pull against."
        ),
        "table_name": "Which synced table the logged row belongs to.",
        "row_pk": (
            "The logged row's primary key, as text -- composite keys are "
            "serialised by the sync layer, not this table."
        ),
        "updated_at": (
            "The logged row's own updated_at at the moment it was "
            "accepted, copied here for audit/debugging -- not itself a "
            "watermark."
        ),
        "origin_device_id": "The machine that originated the logged row.",
        "received_at": (
            "RFC 3339 UTC timestamp the hub accepted this push. Distinct "
            "from updated_at (when the source machine wrote it) -- this is "
            "when the hub saw it, which is what clock skew "
            "(design_decision_04.md consequence 2) makes the two "
            "potentially very different."
        ),
    },
    "schema_meta": {
        "version": "Primary key. Schema version this row records as applied.",
        "applied_at": "RFC 3339 UTC timestamp this version was applied.",
    },
    "pairings": {
        "from_stable_id": (
            "The track being mixed from. Not FK-enforced from this module "
            "-- the owning module predates the shared FK convention."
        ),
        "to_stable_id": "The track being mixed into/out of.",
        "direction": (
            "CHECK IN ('into', 'out_of', 'either') -- which direction the pairing preference runs."
        ),
        "source": ("CHECK IN ('manual', 'learned', 'ai') -- how this pairing was captured."),
        "notes": "Optional free-text note.",
        "confidence": ("Optional 0-1 confidence, for 'learned'/'ai' sourced pairings."),
        "created_at": "First-insert timestamp.",
        "modified_at": "Most recent write timestamp.",
    },
    "smartlists": {
        "id": "Primary key.",
        "name": "Smartlist display name. UNIQUE.",
        "rule": "The rule, as a JSON AST.",
        "rule_schema_version": (
            "Version of the rule AST shape this row was written under, "
            "for forward migration of the rule format."
        ),
        "referenced_fields": (
            "JSON list of track_fields.field_name values the rule reads, "
            "so a field-changed event can know which smartlists to "
            "re-evaluate without parsing the rule."
        ),
        "order_by": (
            "Sort expression applied when materialising, e.g. the default 'added_date desc'."
        ),
        "last_evaluated_at": (
            "RFC 3339 UTC timestamp of the last evaluation, or NULL if never evaluated."
        ),
        "last_materialized_track_ids": (
            "JSON list of stable_ids from the last materialisation, "
            "cached so the UI can render without re-evaluating."
        ),
        "created_at": "First-insert timestamp.",
        "modified_at": "Most recent write timestamp.",
    },
    "play_orders": {
        "id": "Surrogate AUTOINCREMENT primary key.",
        "playlist_id": (
            "The playlist this order was generated for. Not FK-enforced "
            "from this module. UNIQUE together with name."
        ),
        "name": ("Display name for this ordering -- a playlist can have several named orders."),
        "created_at": "First-insert timestamp.",
        "updated_at": "Most recent write timestamp.",
        "generated_by": ("What produced this order (an algorithm name, 'manual', etc.), if known."),
        "goal_json": (
            "Optional JSON description of the generation goal/constraints that produced this order."
        ),
        "schema_version": (
            "Version of the play_order_entries shape this row's entries "
            "were written under. Defaults to 1."
        ),
    },
    "play_order_entries": {
        "id": "Surrogate AUTOINCREMENT primary key.",
        "play_order_id": "FK -> play_orders(id), ON DELETE CASCADE.",
        "stable_id": ("The track at this position. Not FK-enforced from this module."),
        "position": ("0-based ordinal within the play order. UNIQUE together with play_order_id."),
        "target_key": ("Optional target musical key for the transition into this slot."),
        "target_tempo": ("Optional target tempo (BPM) for the transition into this slot."),
        "key_sync": (
            "Optional flag/marker for whether key-sync is intended at this slot (1/0/NULL)."
        ),
        "transition_hint": ("Optional free-text hint about how to mix into this slot."),
    },
    "play_orders_schema_meta": {
        "version": "Primary key. Applied migration version number.",
        "applied_at": "RFC 3339 UTC timestamp this version was applied.",
    },
    "tracks_fts": {
        "title": "Indexed copy of the track title.",
        "artist": "Indexed copy of the (first/primary) artist name.",
        "album": "Indexed copy of the album name.",
        "genre": "Indexed copy of the genre.",
        "key": "Indexed copy of the musical key.",
        "tags": (
            "Indexed tag text; reserved for the Phase 6 tag-unification "
            "work and currently backfilled as an empty string "
            "(apps/launcher/scripts/bootstrap_db.py)."
        ),
    },
    "tracks_frecency": {
        "stable_id": "The track this ranking row concerns. Primary key.",
        "plays": "Play count. Defaults to 0.",
        "drags": (
            "Drag-to-deck (or equivalent 'used it') count. Defaults to 0. "
            "Ranking is ORDER BY drags DESC, last_dragged_at DESC "
            "(idx_frecency_drags) -- drags outweighs plays as the "
            "frecency signal."
        ),
        "last_played_at": (
            "Unix-epoch timestamp (integer, not RFC 3339 -- inconsistent "
            "with the rest of this DB) of the last play, or NULL."
        ),
        "last_dragged_at": "Unix-epoch timestamp of the last drag, or NULL.",
    },
}


__all__ = ["COLUMN_DOCS", "TABLE_DOCS"]
