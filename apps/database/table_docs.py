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

__all__ = ["TABLE_DOCS"]
