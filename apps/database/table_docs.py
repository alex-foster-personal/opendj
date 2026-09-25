"""Per-table one-paragraph descriptions, merged into the generated AGENTS.md.

Split out of :mod:`apps.database.column_docs` (round 2 hardening, the
file-size review gate) so that module stays under the 600-line threshold in
``scripts/quality_gate.py``. ``column_docs.py`` re-exports ``TABLE_DOCS``, so
``from apps.database.column_docs import TABLE_DOCS`` (the existing import
shape used by :mod:`apps.database.generate_agents_md` and its tests) is
unaffected.

See :mod:`apps.database.column_docs` for the coverage rules this dict must
satisfy (which tables, why the fts5 shadow tables are excluded).
"""

from __future__ import annotations

from apps.database.enrollment_table_docs import ENROLLMENT_TABLE_DOCS

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
    "track_availability": (
        "One row per stable_id recording whether its audio is present on "
        "disk right now, distinct from the analysed values that stay in "
        "track_fields regardless. Safe-default views (tracks_available, "
        "tracks_unavailable, track_fields_available) read this as a "
        "dimension so an aggregate cannot silently include unplayable rows."
    ),
    "path_availability": (
        "Resolver-namespaced cache of disk-truth answers for rekordbox "
        "library paths (FolderPath / track file_path strings before "
        "containment mapping). Keys are (resolver_namespace, logical_path); "
        "materialised_size is NULL when the path is missing or a dataless "
        "stub. Written by bounded listing hydration and a background "
        "refresher (issue #1037, PERF-RB-01); survives process restarts."
    ),
    "unmatched_source_analysis": (
        "Staging for an analysed source row (MIK, rekordbox, ...) that "
        "matches no tracks row at all, so there is no stable_id to hang a "
        "track_fields row on. unmatched_reason records why; "
        "promoted_stable_id is the one-way door into track_fields once a "
        "match is later found (see docs/analysis-retention.md)."
    ),
    "track_energy_segments": (
        "Time-series destination for a source's energy-over-time data "
        "(e.g. MIK's ZENERGYSEGMENT), in milliseconds per the repo's "
        "time-series-vs-scalar convention. track_fields is one row per "
        "(stable_id, field_name) and cannot hold a series."
    ),
    # ----- native-analysis v1 (specs/native-analysis-v1.md section 3) ----
    "analysis_canonical": (
        "Which analysis row is canonical for one track and one selection lane. "
        "Recomputed from every row on each write by one rule (highest producer "
        "semver, tie to in-app over backfill, bench candidates never eligible), "
        "so the answer depends on what was produced and never on write order."
    ),
    "analysis_projection": (
        "Own-analysis scalars read at query time when a lane's source is `own`, "
        "rebuilt whenever that track's canonical pointer moves. The ONLY place "
        "own values live: they are never written into track_fields, so nothing "
        "here reaches track_field_history or the sync path. Read it through "
        "apps.analysis.selection.effective_fields, never directly."
    ),
    "analysis_source_default": (
        "The persisted per-lane source (rbx or own), one row per selection lane, "
        "absent until that lane is promoted. This is what a PROMOTION writes and "
        "the only half of the source selection that survives a relaunch; the "
        "PARITY-02 dev toggle is in-memory and is deliberately not stored."
    ),
    "analysis_queue_batch": (
        "One enqueue call on the native-analysis v1 backfill queue, plus the "
        "admission decision it was planned under: the worker count and band "
        "the spec section 4 memory rule chose, and the measured memory model "
        "those numbers came from. Written by apps.analysis.queue."
    ),
    "analysis_queue_item": (
        "One (track, lane) of backfill work and its state: pending, running, "
        "done, skipped, failed, refused or cancelled. The record write and "
        "the move to done are ONE transaction, which is what makes a resume "
        "after a process kill re-run an interrupted item exactly once and "
        "never re-run a completed one."
    ),
    "analysis_stale": (
        "Records whose DEPENDENCY moved underneath them (today only key -> "
        "beatgrid). The row keeps its record but stops being eligible for "
        "analysis_canonical until it is recomputed, so a key computed "
        "against a superseded beatgrid never reads as canonical. Consulted "
        "by apps.analysis.canonical._eligible_rows."
    ),
    "analysis_field_verification": (
        "How each field was verified, stored next to the values it "
        "produced: cross-source agreement, a one-sided single-source "
        "probe, or unverified. Written by apps.mik.load.record_verification "
        "from apps.shared.equivalence.EquivalenceGate.provenance_rows; "
        "overridden records a human forcing a write past a non-passing "
        "verdict."
    ),
    # ----- v12 (FBSYNC-01, ADR-0013) ----------------------------------------
    "feedback_pins": (
        "One row per Open DJ feedback comment pin, carrying the whole pin "
        "as JSON (text, page, anchor, x/y, status, replies, author, build "
        "and environment stamps, attachment metadata). Synced through "
        "CloudSync so every machine enrolled to the same hub sees the same "
        "pins; the per-machine feedback/comments.json store is reconciled "
        "into and out of it. Archive is a tombstone (deleted_at), never a "
        "DELETE."
    ),
    # ----- v10 (specs/karaoke-lyrics-operational-plan.md D13.1) ------------
    "lyric_verdict": (
        "One row per track holding the karaoke lyrics verdict (vocal, "
        "sparse, no-lyrics or unknown), the human override that survives a "
        "recompute, the alignment provenance, and the sha256 of the "
        "track's karaoke_words artifact. Synced, so a verdict computed on "
        "one machine is a verdict everywhere; deletes are tombstones only "
        "(the licensing purge sets deleted_at, it never DELETEs), which is "
        "also what makes an accidental re-ingest of purged lyrics a "
        "no-op."
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
    "users": (
        "One Google account that has signed in to the webui, keyed on the "
        "OIDC 'sub' claim rather than email -- an email can be reassigned, "
        "a sub cannot. Sign-in is identity only, never authorization: the "
        "app behaves identically whether or not anyone is signed in "
        "(migration v6 comment, apps/shared/state/schema.py)."
    ),
    "auth_sessions": (
        "One browser session for one user. Stores the sha256 of the "
        "bearer session token, never the token itself, so a stolen "
        "database cannot be replayed as a live cookie; also holds the "
        "Google refresh/access token pair server-side so the browser "
        "never sees a Google credential. Cascades on user delete."
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
    "local_changelog": (
        "Spoke-local twin of hub_changelog (design_decision_08.md point 3): "
        "same shape, appended by apps.shared.state.sync_stamp on every "
        "write to a synced table. sync_state.last_push_seq fences against "
        "this table's seq, replacing the wall-clock push watermark that "
        "lost rows under clock skew (round 1 finding 3). Never crosses a "
        "machine boundary and never rides hub sync itself."
    ),
    # ----- apps.shared.state.schema.INFRASTRUCTURE_TABLES -----------------
    "schema_meta": (
        "Migration bookkeeping for apps.shared.state.schema.apply_"
        "migrations -- infrastructure, not domain data. One row per "
        "applied schema version."
    ),
    "schema_meta_markers": (
        "Durable completion markers for one-time migration repairs "
        "(for example the v15 track_fields stamp backfill). One row per "
        "repair name; repeat opens check the marker instead of rescanning."
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
    "playlist_sets": (
        "SET-05 performance object for a playlist: a named set with its own "
        "play count and entry snapshot, distinct from PLAY-01 play_orders "
        "orderings and SET-01 recorded sessions "
        "(apps/shared/playlist_sets/schema.py)."
    ),
    "playlist_set_entries": (
        "One row per track position within a playlist_sets snapshot at "
        "create time; independent of later playlist_memberships edits "
        "(apps/shared/playlist_sets/schema.py)."
    ),
    "playlist_set_runs": (
        "Practice or performance run history for a playlist set; only "
        "kind='performance' increments play_count "
        "(apps/shared/playlist_sets/schema.py)."
    ),
    "playlist_sets_schema_meta": (
        "Private migration-version counter for playlist_sets, kept separate "
        "from schema_meta (apps/shared/playlist_sets/schema.py)."
    ),
    "tracks_fts": (
        "FTS5 full-text index over a subset of tracks' searchable columns, "
        "maintained by the launcher quick-open palette "
        "(apps/launcher/scripts/bootstrap_db.py). Populated by an explicit "
        "INSERT INTO tracks_fts(rowid, ...) at backfill time rather than "
        "external-content triggers, so a missed backfill call leaves it "
        "silently stale rather than erroring."
    ),
    "launcher_meta": (
        "Tiny key/value store the desktop launcher keeps for cross-run UI "
        "state that is too small to justify a migration of its own. Created "
        "by apps/launcher/src-tauri/src/state.rs on first access (idempotent "
        "CREATE TABLE IF NOT EXISTS), in whichever database get_db_path "
        "resolves -- the shared state.db whenever it exists. Its only entry "
        "today is the first-run notification flag read by commands::hotkey."
    ),
    "tracks_frecency": (
        "Frequency+recency ranking signal for the launcher's quick-open "
        "palette: how often and how recently a track was played or "
        "dragged. No FK to tracks -- launcher code links the two tables "
        "softly rather than at the schema level "
        "(apps/launcher/scripts/bootstrap_db.py)."
    ),
    "sync_identity_remap": (
        "Spoke-side bookkeeping for CloudSync content-identity collapses: "
        "loser_pk -> survivor_pk, remembered across the batched HTTP push "
        "so a later batch's playlist can still resolve a track PK a prior "
        "batch already remapped. Not in the sync set itself -- carries no "
        "updated_at/origin_device_id/deleted_at -- and does not drop the "
        "loser's tracks row (apps/sync_hub/engine_identity_map.py)."
    ),
    # ----- apps/shared/pairings/schema_sql.py, its SECOND ladder ---------
    # apply_pairing_capture_migrations, applied lazily by the webui capture
    # routes on the daemon's writable state.db. Column docs live in
    # apps/database/column_docs_pairing_capture.py.
    "pairing_capture_schema_meta": (
        "Version counter for the pairing-capture ladder in "
        "apps/shared/pairings/schema_sql.py, SEPARATE from the shared-state "
        "ladder's own schema_meta because a webui route applies it rather "
        "than apply_migrations. One row per applied version; v1 is the only "
        "one so far."
    ),
    "pairing_sync_snapshots": (
        "One captured sync relationship between two decks: which side was "
        "tempo master, at what tempo ratios, and where each playhead was, "
        "so a pairing can be reproduced later. Written by "
        "apps.shared.pairings.capture_repo.PairingCaptureRepo through the "
        "POST routes in apps/webui/server/routes/pairing_capture.py. Both "
        "track references are soft: neither stable_a nor stable_b is "
        "FK-declared, so a capture outlives its tracks."
    ),
    "pairing_alignments": (
        "A named alignment mark between two tracks: the anchor point on "
        "each side (a hot cue or a raw millisecond offset) that should line "
        "up when they are played together. Sibling of "
        "pairing_sync_snapshots -- that one records a sync that HAPPENED, "
        "this one records an alignment a human ASSERTS -- written by the "
        "same repo and routes, and equally FK-free."
    ),
    # ----- sibling apps on the shared connection ------------------------
    # apps/analysis/store.py and apps/spotify/state_aux.py run additive DDL
    # on the same connection after the ladder finishes. Column docs live in
    # apps/database/column_docs_sibling_apps.py.
    "analysis": (
        "One backend's analysis result for one track: bpm, key, energy, and "
        "provenance, one row per (stable_id, backend, backend_version) so "
        "results from different analysis backends, or different versions of "
        "one backend, coexist instead of overwriting each other. Owned by "
        "apps.analysis.store, which runs its own additive DDL on the shared "
        "state.db connection after apps.shared.state's migrations. The flat "
        "columns are a queryable projection of what record_json carries in "
        "full, including fields with no column of their own (onsets_s, "
        "downbeats_s, rms_peaks_s, features_blob) -- see "
        "apps.analysis.record.AnalysisRecord."
    ),
    "analysis_events": (
        "Append-only event log for the analysis domain, written by "
        "apps.analysis.store.publish() alongside every analysis upsert. "
        "Every accepted event is ALSO mirrored into the shared events table "
        "(kind=event_type, actor='apps.analysis') and fanned out on the "
        "in-process apps.shared.state.events.EventBus, so this table is a "
        "domain-scoped duplicate of a subset of events, not the only copy. "
        "It is kept because tests and downstream tooling read it by name "
        "(apps/analysis/store.py module docstring)."
    ),
    "pending_tracks": (
        "Spotify acquisition queue: one row per Spotify playlist track that "
        "apps.spotify.state_writer.write_playlist_and_pending could not "
        "match to a local library track at import time. Alongside the "
        "pending row the importer also inserts a synthetic tracks row "
        "(file_path = the Spotify URI) and gives it a membership on both "
        "the Spotify vendor playlist and its linked ODJ twin, so the "
        "browser renders the unmatched track inline rather than leaving a "
        "gap. apps.spotify.rematch.rematch_playlist re-tries matching these "
        "rows against the local library and promotes hits to "
        "status='resolved', swapping the synthetic placeholder for the real "
        "track in place."
    ),
    "spotify_playlist_meta": (
        "Per-Spotify-playlist import bookkeeping: one row per imported "
        "Spotify playlist, keyed on the state-layer playlist_id. Exists so "
        "write_playlist_and_pending can short-circuit a re-import of an "
        "unchanged playlist by comparing Spotify's own snapshot_id, and so "
        "the webui can show import stats without re-deriving them. The "
        "match and pending counts are a snapshot of the import moment, NOT "
        "a live view: apps.spotify.rematch never writes here, so "
        "matched_count and pending_count do not reflect pending_tracks rows "
        "that later resolve or get abandoned. Query pending_tracks directly "
        "for a current count."
    ),
    "spotify_playlist_links": (
        "Durable link between a Spotify vendor playlist and the ODJ (webui) "
        "playlist that mirrors it: one row per vendor playlist, keyed on "
        "Spotify's own id. Importing an unlinked Spotify playlist creates a "
        "same-name ODJ twin and registers the pair here, so a later "
        "re-import writes into the SAME twin instead of spawning a second "
        "one. apps.spotify.state_aux.link_odj_playlist is the only writer, "
        "and it refreshes updated_at in place when the twin is unchanged. "
        "Machine-local bookkeeping: it carries no origin_device_id and is "
        "not in SYNC_TABLES, so it does not ride hub sync."
    ),
    # Migration v9 enrollment tables (ADR 12), in their own module for the
    # same 600-line reason this file was split out of column_docs.py.
    **ENROLLMENT_TABLE_DOCS,
}

__all__ = ["TABLE_DOCS"]
