"""Column docs for the tables SIBLING APPS create in the shared state DB.

``apps/shared/state/schema.py`` is the schema authority, but it is not the
only writer of ``state.db``. :mod:`apps.analysis.store` and
:mod:`apps.spotify.state_aux` each run their own additive DDL on the shared
connection after the migration ladder has finished, so a live database
carries tables the ladder never declares.

One of these siblings is not even Python: ``apps/launcher/src-tauri/src/
state.rs`` creates ``launcher_meta`` in the same file on every launcher
start. It is documented here for the same reason as the rest -- the
generator documents every LIVE table, whatever language created it.

The generated AGENTS.md documents every LIVE table, so those tables need
curated descriptions or the generator raises
:class:`apps.database.generate_agents_md.MissingColumnDocsError`. Measured
Wed 9 Sep 2026 before these entries existed: regenerating AGENTS.md from a
state.db that had ever run a Spotify import or an analysis publish failed on
all five of these tables at once, and no test saw it because the generator's
own test provisions the migration ladder alone.

They live in their own module rather than in :mod:`apps.database.column_docs`
for two reasons: that file is already near the 600-line threshold in
``scripts/quality_gate.py``, and the ownership boundary is real -- nothing
here is maintained by the schema ladder, so a reader should not have to guess
which entries the ladder is responsible for.

``column_docs.py`` merges this dict into ``COLUMN_DOCS``, so
:mod:`apps.database.generate_agents_md` sees one flat mapping regardless of
which module a table's docs live in. The matching one-paragraph table
descriptions are in :mod:`apps.database.table_docs`.
"""

from __future__ import annotations

SIBLING_APP_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "http_pairings": {
        "pairing_id": (
            "Primary key. Client- or server-minted UUID for the PAIR-04 HTTP "
            "pairing entity (apps.webui.server.pairings_sqlite)."
        ),
        "from_stable_id": (
            "The from-side track. NOT FK-declared despite pointing at "
            "tracks(stable_id); the HTTP layer enforces both sides exist."
        ),
        "to_stable_id": "The to-side track, under the same rules as from_stable_id.",
        "direction": (
            "CHECK IN ('->', '<->'). One-way mix into the to-side, or "
            "bidirectional. Mirrored into the CAT-03 pairings graph as "
            "into/either."
        ),
        "source": (
            "CHECK IN ('manual', 'learned', 'ai'). How the pairing was "
            "captured: explicit Capture, learned from play, or suggested."
        ),
        "notes": "Optional free-text note on the pairing. NULL when unset.",
        "snapshot_json": (
            "Optional JSON snapshot of open-time deck state at capture. "
            "NULL when the client sent none."
        ),
        "created_at": "RFC 3339 UTC timestamp the HTTP pairing row was created.",
        "updated_at": "RFC 3339 UTC timestamp the HTTP pairing row was last written.",
    },
    "launcher_meta": {
        "key": (
            "Setting name. Primary key. The only key in use today is "
            "'first_run_notification_shown' "
            "(apps/launcher/src-tauri/src/state.rs "
            "FIRST_RUN_NOTIFICATION_KEY), which commands::hotkey sets once "
            "so the post-install 'press Alt+Space' notification does not "
            "re-fire on every launch."
        ),
        "value": (
            "The setting's value, as text. NOT NULL. Untyped by design: the "
            "table exists so a one-off UI bit needs no migration, and each "
            "reader parses its own key."
        ),
    },
    "analysis": {
        "stable_id": (
            "The analyzed track. NOT FK-declared (the DDL carries no "
            "REFERENCES clause) despite pointing at tracks(stable_id). Part "
            "of the composite primary key with backend and backend_version."
        ),
        "backend": (
            "Which analysis backend produced this row, e.g. 'librosa-only' "
            "or 'librosa+madmom'. Part of the composite primary key."
        ),
        "backend_version": (
            "Version string for backend, e.g. "
            "'librosa==0.10.2.post1+downbeats=inferred'. Pins the exact "
            "dependency build and feature flags so a later re-run on a "
            "different build lands a new row instead of silently "
            "overwriting the old one. Part of the composite primary key."
        ),
        "analyzed_at": (
            "RFC 3339 UTC timestamp the analysis was produced, from "
            "AnalysisRecord.analyzed_at. Only advances on a semantically "
            "changed re-analysis: apps.analysis.store._semantic_equal "
            "excludes this column when comparing a new record to the stored "
            "one, so a re-run reproducing the same result is a no-op and "
            "analyzed_at keeps its original value."
        ),
        "duration_s": (
            "Audio duration analyzed, in SECONDS. Different unit from "
            "tracks.duration_ms, which is milliseconds."
        ),
        "sample_rate": "Sample rate of the audio as analyzed, in Hz (e.g. 44100).",
        "bpm": "Estimated tempo in beats per minute.",
        "bpm_confidence": (
            "Backend-reported confidence in the bpm estimate. No CHECK "
            "constraint enforces a range; observed values fall in 0 to 1."
        ),
        "key_camelot": (
            "Estimated musical key in Camelot wheel notation (e.g. '8B'), "
            "for DJ software that uses that convention."
        ),
        "key_openkey": (
            "The same estimated key in Open Key / Traktor notation (e.g. "
            "'8d'). A second ENCODING of key_camelot, not an independently "
            "estimated value (apps.analysis.backends.librosa._estimate_key)."
        ),
        "key_confidence": (
            "Backend-reported confidence in the key estimate. No CHECK "
            "constraint enforces a range; observed values fall in 0 to 1."
        ),
        "energy": (
            "Integer energy rating 1-10. "
            "apps.analysis.backends.librosa._energy_from_rms_dbfs bins a "
            "mean RMS-dBFS reading into that range. What it MEANS depends "
            "on energy_source: computed locally when 'inferred', passed "
            "through from an external reading when 'mik'."
        ),
        "energy_source": (
            "Where energy came from: 'mik' (a Mixed In Key reading passed "
            "through) or 'inferred' (computed locally by the backend). Free "
            "text at the DB layer, with no CHECK constraint, but "
            "constrained in application code to "
            "apps.analysis.record.EnergySource."
        ),
        "record_json": (
            "Full JSON-encoded AnalysisRecord, duplicating the flat columns "
            "above plus fields with no column of their own: onsets_s, "
            "downbeats_s, rms_peaks_s (time-series arrays) and "
            "features_blob. fetch_records_by_ids reconstructs a full "
            "AnalysisRecord from this; the flat columns exist for indexed "
            "and aggregate queries that record_json cannot support."
        ),
    },
    "analysis_events": {
        "id": "Surrogate AUTOINCREMENT primary key.",
        "ts": (
            "RFC 3339 UTC timestamp the event was appended, from "
            "apps.analysis.store._now_iso() at publish time. Distinct from "
            "analysis.analyzed_at, which is when the analysis itself was "
            "produced -- a batch driver may publish well after analyzing."
        ),
        "event_type": (
            "Event type name, free text. 'analyze' is the only value the "
            "current tree writes (one row per analysis upsert); "
            "publish_event() is a generic passthrough, so other values are "
            "expected from future consumers."
        ),
        "stable_id": (
            "The track the event concerns, if any. Nullable and not "
            "FK-enforced, mirroring the shared events table's own stable_id "
            "column so an event can outlive the track's deletion."
        ),
        "payload_json": (
            "JSON-encoded event payload. For event_type='analyze', "
            "apps.analysis.store.upsert_record builds it from {stable_id, "
            "backend, backend_version, analyzed_at, bpm, key_camelot, "
            "energy, inserted}."
        ),
    },
    "pending_tracks": {
        "pending_id": "Surrogate AUTOINCREMENT primary key.",
        "playlist_id": (
            "FK -> playlists(playlist_id), ON DELETE CASCADE. The "
            "state-layer Spotify playlist id (format "
            "'spotify:<vendor_pl_id>'), NOT the ODJ twin's playlist id."
        ),
        "position": (
            "0-based ordinal within the Spotify playlist's snapshot order, "
            "and the position key of this track's playlist_memberships row. "
            "The synthetic placeholder occupies that (playlist_id, "
            "position) until rematch swaps in the matched local track in "
            "place (apps.spotify.rematch._promote_pending_row)."
        ),
        "spotify_uri": (
            "The track's Spotify URI (format 'spotify:track:<id>'). Also "
            "becomes the synthetic tracks.file_path for the placeholder row "
            "(apps.spotify.state_writer_tracks._upsert_synthetic_track), "
            "which is how the browser recognizes it as streaming rather "
            "than a broken local link."
        ),
        "isrc": (
            "International Standard Recording Code as reported by Spotify, "
            "or NULL when Spotify returned none (common for user-uploaded "
            "local files). Indexed via idx_pending_tracks_isrc (partial, "
            "WHERE isrc IS NOT NULL) for matching against tracks.isrc."
        ),
        "title": "Track title, as reported by Spotify.",
        "artist": (
            "Comma-and-space-joined artist names "
            "(apps.spotify.client.SpotifyTrack.artists_joined), not a "
            "normalized artist reference."
        ),
        "album": "Album name as reported by Spotify; NULL if Spotify returned none.",
        "duration_ms": (
            "Track duration in milliseconds as reported by Spotify; NULL if "
            "unavailable."
        ),
        "suggested_sources_json": (
            "JSON object with exactly the 5 fixed keys in "
            "apps.spotify.state_writer_tracks.SUGGESTED_SOURCE_KEYS "
            "(beatport, bandcamp, qobuz, apple_music, discogs), mapping to "
            "generated search-query URLs built from the track's title and "
            "artist. These are search links for a human to click, NOT "
            "confirmed purchase matches."
        ),
        "status": (
            "CHECK IN ('pending','purchased','resolved','abandoned'). "
            "'pending' is the insert default. 'resolved' is set by "
            "apps.spotify.rematch.rematch_playlist when a later match "
            "succeeds, together with resolved_stable_id and resolved_at. "
            "'abandoned' is set by "
            "apps.spotify.state_aux.mark_pending_abandoned. 'purchased' is "
            "declared in the CHECK constraint but NO code path in the "
            "current tree sets it -- reserved for a future manual "
            "mark-as-purchased action, not dead."
        ),
        "added_at": (
            "Timestamp this row was inserted, in apps.shared.state."
            "sync_stamp's canonical format (fixed 6-digit microseconds, "
            "explicit '+00:00' offset), from canonical_now() at import "
            "time. Note this is NOT the 'Z'-suffixed shape several other "
            "tables in this DB use."
        ),
        "resolved_stable_id": (
            "FK -> tracks(stable_id), ON DELETE SET NULL. The local track "
            "this row was matched to, set with status='resolved'. If that "
            "track is later deleted this reverts to NULL but status is NOT "
            "reverted to 'pending', so a resolved-then-deleted row is not "
            "retried automatically."
        ),
        "resolved_at": (
            "Timestamp status last moved away from 'pending' (to 'resolved' "
            "or 'abandoned'), same canonical '+00:00' format as added_at. "
            "NULL while status='pending'."
        ),
    },
    "spotify_playlist_meta": {
        "playlist_id": (
            "Primary key. FK -> playlists(playlist_id), ON DELETE CASCADE. "
            "The state-layer id for this Spotify playlist (format "
            "'spotify:<vendor_pl_id>')."
        ),
        "vendor_pl_id": (
            "Spotify's own playlist id, duplicated from "
            "playlists.vendor_pl_id for direct lookup: "
            "already_imported_snapshot queries this table by vendor_pl_id, "
            "not by playlist_id."
        ),
        "snapshot_id": (
            "Spotify's own snapshot id for this playlist version. Compared "
            "against an incoming import's snapshot_id to skip re-writing an "
            "unchanged playlist, unless force=True."
        ),
        "track_count": (
            "Total tracks in the playlist at the moment of this import, "
            "equal to matched_count + pending_count."
        ),
        "matched_count": (
            "How many of those tracks matched an existing local track at "
            "import time. Frozen at import time; not updated when "
            "pending_tracks rows later resolve."
        ),
        "pending_count": (
            "How many of those tracks had no local match and were written "
            "to pending_tracks at import time. Frozen for the same reason "
            "as matched_count."
        ),
        "last_import_at": (
            "Timestamp of this row's most recent import write, in "
            "apps.shared.state.sync_stamp's canonical '+00:00' format (see "
            "pending_tracks.added_at)."
        ),
    },
    "playlist_sets": {
        "id": "Surrogate primary key for one SET-05 performance set.",
        "playlist_id": (
            "Parent playlist this set belongs to. NOT FK-enforced; keyed by "
            "opaque playlist_id like play_orders."
        ),
        "name": (
            "Human-readable set name, UNIQUE per playlist. Shown in the "
            "library browser tab strip with play_count."
        ),
        "play_count": (
            "Denormalized performance count. Only incremented by "
            "kind='performance' runs; practice runs do not touch it."
        ),
        "source_play_order_id": (
            "Optional provenance: play_orders.id copied at create time when "
            "from_play_order was supplied. Not FK-enforced."
        ),
        "created_at": "RFC 3339 UTC timestamp when the set was created.",
        "updated_at": (
            "RFC 3339 UTC timestamp of the last set mutation or run record."
        ),
    },
    "playlist_set_entries": {
        "id": "Surrogate primary key for one entry row.",
        "set_id": "FK -> playlist_sets(id), ON DELETE CASCADE.",
        "stable_id": "Track stable_id at snapshot time.",
        "position": "0-based position within this set's snapshot.",
    },
    "playlist_set_runs": {
        "id": "Surrogate primary key for one run record.",
        "set_id": "FK -> playlist_sets(id), ON DELETE CASCADE.",
        "kind": (
            "Run kind: 'practice' or 'performance'. Only 'performance' "
            "increments playlist_sets.play_count."
        ),
        "created_at": "RFC 3339 UTC timestamp when the run was recorded.",
    },
    "playlist_sets_schema_meta": {
        "version": (
            "Applied playlist_sets schema version. Primary key, one row per "
            "completed migration step."
        ),
        "applied_at": "RFC 3339 UTC timestamp when this version was applied.",
    },
    "spotify_playlist_links": {
        "vendor_pl_id": (
            "Primary key. Spotify's own playlist id, so a re-import can find "
            "the existing link before it knows any state-layer id."
        ),
        "spotify_playlist_id": (
            "FK -> playlists(playlist_id), ON DELETE CASCADE. The state-layer "
            "row for the Spotify playlist itself (format "
            "'spotify:<vendor_pl_id>'), the source side of the link."
        ),
        "odj_playlist_id": (
            "FK -> playlists(playlist_id), ON DELETE CASCADE. The ODJ (webui) "
            "twin the Spotify playlist is mirrored into, the destination side "
            "of the link. Indexed by idx_spotify_playlist_links_odj for the "
            "reverse lookup."
        ),
        "created_at": (
            "When the link was first registered, in apps.shared.state."
            "sync_stamp's canonical '+00:00' format. Preserved across "
            "re-links of the same twin."
        ),
        "updated_at": (
            "When link_odj_playlist last touched the row, same format as "
            "created_at. Local bookkeeping only: this is NOT the sync "
            "layer's updated_at, because the table is not in SYNC_TABLES."
        ),
    },
    "sync_identity_remap": {
        "loser_pk": (
            "Primary key. The content-identity duplicate's stable_id that "
            "lost the LWW comparison. Its tracks row is held out of the "
            "sync offer, not deleted (apps/sync_hub/engine_identity_map.py)."
        ),
        "survivor_pk": (
            "The stable_id every loser_pk's children get remapped onto "
            "before the digest is computed, so a spoke's local digest "
            "matches the hub's remapped bundle (ADR 04 c6)."
        ),
    },
}

__all__ = ["SIBLING_APP_COLUMN_DOCS"]
