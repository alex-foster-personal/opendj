"""Curated table/column descriptions merged into the generated AGENTS.md.

Plain dicts -- not a database, not a class hierarchy. :mod:`apps.database.
generate_agents_md` merges these against live sqlite introspection so
``<data-root>/state/AGENTS.md`` cannot silently drift from the schema it
describes: a live column with no entry here fails the generator loudly (see
:class:`apps.database.generate_agents_md.MissingColumnDocsError`), the drift
guard specs/cloudsync-spec.md D3 asks for.

Two dicts:
- ``TABLE_DOCS`` (:mod:`apps.database.table_docs`, re-exported here): one
  short paragraph of purpose per table.
- ``COLUMN_DOCS`` (this module): ``table -> {column: one-line meaning}``.

``TABLE_DOCS`` moved to its own module in round 2 hardening so this file
stays under the 600-line review threshold; the split is purely mechanical,
``from apps.database.column_docs import TABLE_DOCS`` still works. The four
analysis-retention tables' entries (PR #383) moved the same way into
:mod:`apps.database.column_docs_analysis_retention`, and schema v10's
``lyric_verdict`` into :mod:`apps.database.column_docs_lyrics`; both are
merged into ``COLUMN_DOCS`` below, for the same reason.

Coverage: every table :mod:`apps.shared.state.schema` knows about --
``TABLES`` (the fourteen this module actually creates), the seven real
tables it declares in ``FOREIGN_AUTHORITY_TABLES`` but does not create
(``pairings``, ``smartlists``, ``play_orders``, ``play_order_entries``,
``play_orders_schema_meta``, ``tracks_fts``, ``tracks_frecency``), and
``schema_meta`` and ``schema_meta_markers`` from ``INFRASTRUCTURE_TABLES`` -- twenty-three tables in all.
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

from apps.database.column_docs_analysis_retention import ANALYSIS_RETENTION_COLUMN_DOCS
from apps.database.column_docs_feedback import FEEDBACK_COLUMN_DOCS
from apps.database.column_docs_lyrics import LYRICS_COLUMN_DOCS
from apps.database.column_docs_native_analysis import NATIVE_ANALYSIS_COLUMN_DOCS
from apps.database.column_docs_pairing_capture import PAIRING_CAPTURE_COLUMN_DOCS
from apps.database.column_docs_sibling_apps import SIBLING_APP_COLUMN_DOCS
from apps.database.column_docs_sync_gate import SYNC_GATE_COLUMN_DOCS
from apps.database.enrollment_table_docs import ENROLLMENT_COLUMN_DOCS
from apps.database.table_docs import TABLE_DOCS

__all__ = ["COLUMN_DOCS", "TABLE_DOCS"]


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
        "audio_hash": (
            "Tag-independent SHA-256 of the decoded container audio payload; "
            "used beside content_hash for CloudSync identity merges."
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
        "forbid_duplicates": (
            "When 1, reject extra copies of an already-present stable_id on "
            ":add and duplicate stable_ids on PUT; default 0 allows repeats."
        ),
    },
    "playlist_memberships": {
        "item_id": "Stable membership row id; added in v13.",
        "order_key": (
            "Fractional sort key string; added in v13. Integer position is "
            "computed on read."
        ),
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
        "machine_id": (
            "FK -> machines(machine_id). Added in the v6 PK rebuild "
            "(design_decision_08.md point 1): joins the natural key so "
            "each machine's view of where a file lives -- file_path, "
            "available, probed_at, venue_* -- is its own row rather than "
            "one global row two machines can mint different location_id "
            "values for (round 1 findings 1 and 7a). DDL-nullable, not "
            "NOT NULL, for the same reason origin_device_id is: pure-SQL "
            "migration cannot know this machine's id, so it is backfilled "
            "immediately after migration by "
            "apps.shared.state.sync_stamp.backfill_local_machine_id and "
            "the NOT NULL invariant is enforced by a tripwire test, not "
            "DDL."
        ),
    },
    "users": {
        "google_sub": (
            "Primary key. Google's OIDC 'sub' claim -- the only "
            "identifier Google guarantees is stable and never reused, "
            "unlike email."
        ),
        "email": "Google account email at last sign-in. UNIQUE, but not the key -- see google_sub.",
        "name": "Google profile display name, or NULL if Google did not return one.",
        "avatar_url": "Google profile picture URL, or NULL if Google did not return one.",
        "created_at": "RFC 3339 UTC timestamp of this account's first sign-in.",
        "updated_at": "RFC 3339 UTC timestamp of the most recent profile refresh.",
    },
    "auth_sessions": {
        "session_token_sha256": (
            "Primary key. sha256 hex digest of the bearer session token "
            "the browser holds; the raw token itself is never stored, so "
            "a stolen database cannot be replayed as a live cookie."
        ),
        "google_sub": "FK -> users(google_sub), ON DELETE CASCADE.",
        "refresh_token": (
            "The Google OAuth refresh token that produced this session. "
            "Stays server-side and is never exposed over the API."
        ),
        "access_token": "The current Google OAuth access token, if one is cached.",
        "access_expires_at": (
            "RFC 3339 UTC expiry of access_token, or NULL if none is cached."
        ),
        "created_at": "RFC 3339 UTC timestamp this session was created (sign-in time).",
        "last_seen_at": "RFC 3339 UTC timestamp of this session's most recent authenticated request.",
        "expires_at": "RFC 3339 UTC timestamp this session stops being valid, regardless of activity.",
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
        "asset_kind": (
            "CHECK IN ('audio', 'stem_bundle', 'anlz_cache', 'vocal_cache', "
            "'lyrics_cache', 'karaoke_words') -- widened from four kinds to "
            "six by the schema v10 table rebuild "
            "(apps/shared/state/migrations_v10.ASSET_KIND_CHECK_VALUES is the "
            "one source of that list)."
        ),
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
    "path_availability": {
        "resolver_namespace": (
            "SHA-256 fingerprint of the active path-map entries plus this "
            "machine's machine-id file. Rows from an old namespace are ignored "
            "after a path-map change."
        ),
        "logical_path": (
            "Library path string before containment resolution (rekordbox "
            "FolderPath, tracks.file_path, or track_locations path)."
        ),
        "materialised_size": (
            "Materialised st_size in bytes from fs_residency, or NULL when "
            "the resolved path is missing, not a file, or a dataless stub."
        ),
        "checked_at": (
            "RFC 3339 UTC timestamp when this row was last probed or written."
        ),
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
        "peer_generation": (
            "The generation token this peer reported at the last completed "
            "sync (apps/sync_hub/generation.py). A DIFFERENT token on the "
            "next hello means the peer's database moved backwards -- a "
            "Litestream point-in-time restore -- so both watermarks reset to "
            "0 and this machine re-offers its library. Keying restore "
            "detection on the token rather than on the peer's MAX(seq) going "
            "backwards is what stops a routine changelog prune from looking "
            "like a restore. NULL until the first sync completes."
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
    "local_changelog": {
        "seq": (
            "Primary key, AUTOINCREMENT. Monotonically increasing "
            "spoke-local sequence number; sync_state.last_push_seq is the "
            "floor into this table (design_decision_08.md point 3)."
        ),
        "table_name": "Which synced table the logged row belongs to.",
        "row_pk": (
            "The logged row's primary key, as text -- composite keys are "
            "serialised by the sync layer, not this table."
        ),
        "updated_at": (
            "The logged row's own updated_at at the moment this machine "
            "wrote it, copied here for audit/debugging -- not itself a "
            "watermark."
        ),
        "origin_device_id": "This machine's machine_id.",
        "received_at": (
            "RFC 3339 UTC timestamp this machine's own write was appended "
            "here. Same field name as hub_changelog.received_at for shape "
            "symmetry, but there is no remote hop to distinguish it from "
            "updated_at at this end of the pipe."
        ),
    },
    "schema_meta": {
        "version": "Primary key. Schema version this row records as applied.",
        "applied_at": "RFC 3339 UTC timestamp this version was applied.",
    },
    "schema_meta_markers": {
        "marker": "Primary key. Durable name of a one-time migration repair.",
        "applied_at": "RFC 3339 UTC timestamp the repair completed.",
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
        "name": "Smartlist display name. UNIQUE among live rows; tombstones rewrite name to free it.",
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
        "deleted_at": "Tombstone timestamp; NULL means live.",
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
    **ANALYSIS_RETENTION_COLUMN_DOCS,
    **NATIVE_ANALYSIS_COLUMN_DOCS,
    **PAIRING_CAPTURE_COLUMN_DOCS,
    **SIBLING_APP_COLUMN_DOCS,
    # Migration v9 enrollment tables (ADR 12).
    **ENROLLMENT_COLUMN_DOCS,
    # Migration v10 lyric_verdict (specs/karaoke-lyrics-operational-plan.md D13.1).
    **LYRICS_COLUMN_DOCS,
    # Migration v12 feedback_pins (FBSYNC-01, ADR-0013).
    **FEEDBACK_COLUMN_DOCS,
    # Migration v21 sync_write_tokens (issue #4396).
    **SYNC_GATE_COLUMN_DOCS,
}


__all__ = ["COLUMN_DOCS", "TABLE_DOCS"]
