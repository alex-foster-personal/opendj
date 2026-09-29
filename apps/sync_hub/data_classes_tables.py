"""Data classes for every database table, on both schema ladders.

Covers ``apps.shared.state.schema.ALL_KNOWN_TABLES`` (the shared ladder plus
the foreign authorities that write into the same file),
``apps.engine_core.store.schema.ALL_TABLES`` (the consolidated engine
ladder), its ``ALL_CACHE_TABLES`` (``cache.db``) and its
``VENDOR_SIDECAR_TABLES`` (rekordbox ``master.db``). Coverage is enforced by
``tests/cloudsync/test_data_classes.py``, which derives the table sets from
that schema code, never from this file.
"""

from __future__ import annotations

from apps.sync_hub.data_classes_types import (
    DataClass,
    Location,
    fixed,
    fk,
    logical,
    state_tables,
)

_LIBRARY_RULE = (
    "Library data always fully syncs, so it is not configurable per machine: "
    "other synced rows reference it by foreign key, and excluding a parent "
    "while a child syncs reproduces the FK 409 wedge (sync_set.py)."
)

# ----- rows through the hub changelog (protocol_common.SYNC_TABLES) -----------

CHANGELOG_CLASSES: tuple[DataClass, ...] = (
    fixed(
        "library-tracks",
        "Tracks",
        state_tables("tracks"),
        "sync_hub_changelog",
        f"The anchor table: almost everything else points here. {_LIBRARY_RULE}",
        (),
    ),
    fixed(
        "lyric-verdicts",
        "Karaoke lyric verdicts",
        state_tables("lyric_verdict"),
        "sync_hub_changelog",
        "One row per track, keyed by stable_id both machines derive, so it "
        f"merges cleanly. {_LIBRARY_RULE}",
        (fk("library-tracks"),),
    ),
    fixed(
        "library-playlists",
        "Playlists and crates",
        state_tables("playlists"),
        "sync_hub_changelog",
        _LIBRARY_RULE,
        (),
    ),
    fixed(
        "playlist-memberships",
        "Playlist membership and order",
        state_tables("playlist_memberships"),
        "sync_hub_changelog",
        "Never pushed on its own: rides as a whole-playlist bundle attached to "
        "its playlists row and replaces the peer copy when that row wins LWW "
        f"(ADR-0004 c5). {_LIBRARY_RULE}",
        (fk("library-tracks"), fk("library-playlists")),
    ),
    fixed(
        "track-vendor-ids",
        "Vendor ids per track",
        state_tables("track_vendor_ids"),
        "sync_hub_changelog",
        _LIBRARY_RULE,
        (fk("library-tracks"),),
    ),
    fixed(
        "track-fields",
        "Track fields (tags, ratings, adapter analysis values)",
        state_tables("track_fields"),
        "sync_hub_changelog",
        "Ratings sync as the field 'rating'. Each adapter keeps its own opinion "
        f"per field. {_LIBRARY_RULE}",
        (fk("library-tracks"),),
    ),
    fixed(
        "track-locations",
        "Where each machine has each track's audio",
        state_tables("track_locations"),
        "sync_hub_changelog",
        "Rows carry machine_id, so every machine learns where the others hold "
        "a file. The audio bytes do NOT ride here (see the audio class). "
        f"{_LIBRARY_RULE}",
        (fk("library-tracks"), fk("machines")),
    ),
    fixed(
        "sync-policies",
        "Per-machine asset-kind modes",
        state_tables("sync_policies"),
        "sync_hub_changelog",
        "The per-machine policy itself syncs so every machine sees the whole "
        "fleet's settings. Recommended single policy authority (pending "
        "decision D4b, see the doc).",
        (fk("machines"),),
    ),
    fixed(
        "playlist-pins",
        "Per-machine playlist overrides",
        state_tables("playlist_pins"),
        "sync_hub_changelog",
        "A pin beats the machine's audio default for the tracks in that "
        "playlist (apps/cloud/hydration_core.py).",
        (fk("machines"), fk("library-playlists")),
    ),
    fixed(
        "feedback-pins",
        "Open DJ feedback pins",
        state_tables("feedback_pins"),
        "sync_hub_changelog",
        "The whole pin (text, page, anchor, status, and the rest) rides as "
        "one JSON doc per row with CloudSync's own row-level last-writer-wins "
        "on (updated_at, origin_device_id) (ADR-0013). comments.json stays "
        "the offline-first store of record; the table is just the sync channel.",
        (),
    ),
)

REGISTRY_CLASSES: tuple[DataClass, ...] = (
    fixed(
        "machines",
        "Fleet registry (machines)",
        state_tables("machines"),
        "sync_hub_registry",
        "Owner-scoped snapshot, not the changelog: a peer may insert unknown "
        "machines but only rewrite its own row (engine_machines.py).",
        (),
    ),
)

# ----- never leaves the machine -----------------------------------------------

LOCAL_TABLE_CLASSES: tuple[DataClass, ...] = (
    fixed(
        "sync-bookkeeping",
        "Sync watermarks, changelogs and identity remaps",
        state_tables(
            "sync_state",
            "hub_changelog",
            "local_changelog",
            "sync_identity_remap",
            "sync_write_tokens",
        ),
        "machine_local",
        "Syncing your own sync watermarks would be incoherent (migrations_v6_v8.py). "
        "sync_identity_remap (engine_identity_map.py) holds identity-collapse remaps "
        "across batched hub_apply calls on the shared connection; additive bookkeeping "
        "for this machine's own apply, not part of the sync set itself. "
        "sync_write_tokens (migrations_v20.py) gates this machine's own digest walk.",
        (),
    ),
    fixed(
        "schema-bookkeeping",
        "Migration bookkeeping",
        state_tables(
            "schema_meta",
            "schema_meta_markers",
            "pairing_capture_schema_meta",
            "play_orders_schema_meta",
            "playlist_sets_schema_meta",
        ),
        "machine_local",
        "Records which migrations ran on THIS file, and which one-shot repairs "
        "have completed on it (schema_meta_markers, v16); each machine migrates "
        "and repairs itself.",
        (),
    ),
    fixed(
        "sign-in-identity",
        "Signed-in users and auth sessions",
        state_tables("users", "auth_sessions"),
        "machine_local",
        "auth_sessions holds a Google refresh token: it must never leave the "
        "machine (ADR-0004 point 8). Note: the unused Litestream config would "
        "replicate it (see litestream-replica).",
        (),
    ),
    fixed(
        "sync-credentials",
        "Per-machine sync credential",
        state_tables("machine_credentials"),
        "machine_local",
        "Only the sha256 of the bearer credential is stored (ADR-0012 "
        "Amendment X5, migration v11). Hub-local, never synced; one live "
        "credential per machine_id, and re-minting replaces it.",
        (fk("machines"),),
    ),
    fixed(
        "enrollment-authority",
        "Machine ownership and enrollment grants",
        state_tables("machine_owners", "enrollment_grants"),
        "machine_local",
        "Hub-authoritative: a restored or hostile spoke must not push ownership "
        "(migrations_v9.py).",
        (fk("machines"), fk("sign-in-identity")),
    ),
    fixed(
        "event-log",
        "Engine event bus",
        state_tables("events"),
        "machine_local",
        "Append-only record of what happened on this machine (ADR-0004 point 8).",
        (),
    ),
    fixed(
        "track-field-history",
        "Track field audit trail",
        state_tables("track_field_history"),
        "machine_local",
        "Per-machine audit trail of field changes (ADR-0004 point 8).",
        (logical("track-fields"),),
    ),
    fixed(
        "adapter-registry",
        "Vendor adapter run state",
        state_tables("adapters"),
        "machine_local",
        "Each machine's last run against its own vendor libraries (ADR-0004 point 8).",
        (),
    ),
    fixed(
        "track-availability",
        "Is this track playable here",
        state_tables("track_availability"),
        "machine_local",
        "A verdict about THIS machine's disk (the path checked and when); "
        "syncing it would tell another machine its files are present.",
        (fk("library-tracks"),),
    ),
    fixed(
        "path-availability-index",
        "Library path disk-truth index",
        state_tables("path_availability"),
        "machine_local",
        "Stat answers for rekordbox library paths on THIS machine's disk, keyed "
        "by a resolver namespace that includes the machine id (migrations_v18.py, "
        "issue #1037). Syncing it would tell another machine its files are present.",
        (),
    ),
    fixed(
        "launcher-derived",
        "Launcher search index and frecency",
        state_tables(
            "tracks_fts",
            "tracks_fts_config",
            "tracks_fts_content",
            "tracks_fts_data",
            "tracks_fts_docsize",
            "tracks_fts_idx",
            "tracks_frecency",
            "launcher_meta",
        ),
        "machine_local",
        "Derived and cheap to rebuild from tracks (apps/database/descriptions.py).",
        (logical("library-tracks"),),
    ),
    fixed(
        "daemon-settings",
        "Daemon key/value settings",
        state_tables("settings"),
        "machine_local",
        "Machine-local by nature; CloudSync must not blindly replicate it.",
        (),
    ),
    fixed(
        "engine-analysis",
        "Engine audio analysis rows",
        state_tables(
            "analysis",
            "analysis_events",
            "analysis_canonical",
            "analysis_projection",
        ),
        "machine_local",
        "Derived from audio and recomputable (engine ladder only). Each machine "
        "recomputes; the file-level twin is anlz-cache.",
        (logical("library-tracks"),),
    ),
    fixed(
        "analysis-backfill-queue",
        "Native-analysis backfill queue and stale records",
        state_tables(
            "analysis_queue_batch",
            "analysis_queue_item",
            "analysis_stale",
        ),
        "machine_local",
        "Per-machine job queue for native-analysis backfill (apps.analysis.queue). "
        "A batch, its items, and records whose dependency moved: another machine "
        "re-enqueues its own work and does not inherit this queue.",
        (logical("engine-analysis"),),
    ),
    fixed(
        "file-fingerprint-cache",
        "Fingerprint and file-hash cache",
        (Location("cache_db_table", "fingerprints"), Location("cache_db_table", "file_hashes")),
        "machine_local",
        "Lives in the regenerable cache.db: deleting it is a legal recovery move.",
        (logical("audio"),),
    ),
    fixed(
        "rekordbox-hot-cue-sidecar",
        "Rekordbox hot-cue reversal log",
        (
            Location("vendor_db_table", "rb_hot_cue_reversal"),
            Location("vendor_db_table", "rb_hot_cue_slot_revision"),
        ),
        "machine_local",
        "Keyed by rekordbox ContentID; only meaningful next to this machine's "
        "own rekordbox master.db.",
        (logical("rekordbox-plain-db"),),
    ),
)

# ----- should move between machines, nothing moves it yet -----------------------

_OUTSIDE_SYNC_SET = (
    "Lives in state.db but outside SYNC_TABLES and without the sync trio, so "
    "an edit on one machine is invisible on the others. Owner decision D2/D4 "
    "(fold into the shared ladder, then add to SYNC_TABLES in FK order)."
)

UNBUILT_TABLE_CLASSES: tuple[DataClass, ...] = (
    fixed(
        "smartlists",
        "Smartlists (saved rules)",
        state_tables("smartlists"),
        "not_yet_built",
        f"User-authored. {_OUTSIDE_SYNC_SET}",
        (logical("library-tracks"),),
    ),
    fixed(
        "pairings",
        "Track pairings",
        state_tables("pairings"),
        "not_yet_built",
        f"User-authored relationships between tracks. {_OUTSIDE_SYNC_SET}",
        (logical("library-tracks"),),
    ),
    fixed(
        "pairing-capture",
        "Pairing capture snapshots and alignments",
        state_tables("pairing_sync_snapshots", "pairing_alignments"),
        "not_yet_built",
        f"Captured pairing evidence (PAIR-01). {_OUTSIDE_SYNC_SET}",
        (logical("pairings"),),
    ),
    fixed(
        "play-orders",
        "Play orders",
        state_tables("play_orders", "play_order_entries"),
        "not_yet_built",
        f"User-authored performance orderings. {_OUTSIDE_SYNC_SET}",
        (logical("library-playlists"), logical("library-tracks")),
    ),
    fixed(
        "playlist-sets",
        "Playlist performance sets",
        state_tables("playlist_sets", "playlist_set_entries", "playlist_set_runs"),
        "not_yet_built",
        "User-authored performance sets within a playlist (SET-05, ADR-0016). "
        f"{_OUTSIDE_SYNC_SET}",
        # playlist_sets.playlist_id carries no SQL REFERENCES clause (unlike
        # playlist_memberships/playlist_pins, which genuinely reference
        # playlists(playlist_id)) -- same as its sibling "play-orders" above,
        # which declares both of its deps as logical for the same reason.
        (logical("library-playlists"), logical("library-tracks")),
    ),
    fixed(
        "analysis-retention",
        "Retained analysis for missing audio (v8)",
        state_tables(
            "unmatched_source_analysis",
            "track_energy_segments",
            "analysis_field_verification",
        ),
        "not_yet_built",
        "Arrived in v8 with no stated sync reason; excluded implicitly because "
        f"it lacks origin_device_id. {_OUTSIDE_SYNC_SET}",
        (fk("library-tracks"),),
    ),
    fixed(
        "analysis-source-promotions",
        "Promoted analysis source per lane",
        state_tables("analysis_source_default"),
        "not_yet_built",
        "A promotion is a decision, not derived data, so each machine would "
        f"otherwise re-decide it. {_OUTSIDE_SYNC_SET}",
        (logical("engine-analysis"),),
    ),
    fixed(
        "recorded-sets",
        "Recorded DJ sets",
        (*state_tables("sets", "set_events"), Location("file", "<data>/sets/**")),
        "not_yet_built",
        f"A performance record the user would expect everywhere. {_OUTSIDE_SYNC_SET}",
        (logical("library-tracks"),),
    ),
    fixed(
        "dedup-judgments",
        "Duplicate clusters, aliases and tag provenance",
        state_tables("duplicate_clusters", "track_aliases", "tag_provenance"),
        "not_yet_built",
        f"Human judgment, not recomputable. {_OUTSIDE_SYNC_SET}",
        (logical("library-tracks"),),
    ),
    fixed(
        "spotify-links",
        "Spotify playlist links and acquisition queue",
        state_tables("spotify_playlist_meta", "spotify_playlist_links", "pending_tracks"),
        "not_yet_built",
        f"User-linked playlists and a wanted-tracks queue. {_OUTSIDE_SYNC_SET}",
        (fk("library-playlists"), fk("library-tracks")),
    ),
)

TABLE_CLASSES: tuple[DataClass, ...] = (
    *CHANGELOG_CLASSES,
    *REGISTRY_CLASSES,
    *LOCAL_TABLE_CLASSES,
    *UNBUILT_TABLE_CLASSES,
)
