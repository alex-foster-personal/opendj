BEGIN TRANSACTION;
CREATE TABLE adapters (
        adapter_id   TEXT PRIMARY KEY,
        last_run_at  TEXT,
        last_ok      INTEGER NOT NULL DEFAULT 0,
        notes        TEXT
    );
CREATE TABLE events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        kind         TEXT NOT NULL,
        stable_id    TEXT,
        payload_json TEXT,
        actor        TEXT
    );
CREATE TABLE playlist_memberships (
        playlist_id  TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
        stable_id    TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        PRIMARY KEY (playlist_id, position)
    );
CREATE TABLE playlists (
        playlist_id   TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        vendor        TEXT NOT NULL,
        vendor_pl_id  TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (vendor, vendor_pl_id)
    );
CREATE TABLE schema_meta (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT INTO "schema_meta" VALUES(1,'2026-09-01T00:00:00+00:00');
INSERT INTO "schema_meta" VALUES(2,'2026-09-01T00:00:00+00:00');
INSERT INTO "schema_meta" VALUES(3,'2026-09-01T00:00:00+00:00');
INSERT INTO "schema_meta" VALUES(4,'2026-09-01T00:00:00+00:00');
INSERT INTO "schema_meta" VALUES(5,'2026-09-01T00:00:00+00:00');
CREATE TABLE "track_field_history" (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stable_id     TEXT NOT NULL,
        field_name    TEXT NOT NULL,
        value_json    TEXT NOT NULL,
        source        TEXT NOT NULL,
        confidence    REAL,
        modified_at   TEXT NOT NULL,
        superseded_at TEXT NOT NULL
    );
CREATE TABLE "track_fields" (
        stable_id    TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        field_name   TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source       TEXT NOT NULL CHECK (source IN
                       ('mik','rekordbox','djay','serato','traktor',
                        'open-dj-tool','manual','inferred','webui')),
        confidence   REAL CHECK (confidence IS NULL OR
                                 (confidence >= 0 AND confidence <= 1)),
        modified_at  TEXT NOT NULL,
        PRIMARY KEY (stable_id, field_name)
    );
CREATE TABLE track_locations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stable_id     TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('local', 'remote')),
        role          TEXT NOT NULL DEFAULT 'alternate'
                        CHECK (role IN ('primary', 'alternate')),
        file_path     TEXT,
        remote_url    TEXT,
        venue_key     TEXT,
        venue_rank    INTEGER,
        available     INTEGER NOT NULL DEFAULT 0,
        probed_at     TEXT,
        content_hash  TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        CHECK (
            (file_path IS NOT NULL AND file_path != '')
            OR (remote_url IS NOT NULL AND remote_url != '')
        )
    );
CREATE TABLE track_vendor_ids (
        stable_id  TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        vendor     TEXT NOT NULL,
        vendor_id  TEXT NOT NULL,
        PRIMARY KEY (stable_id, vendor)
    );
CREATE TABLE tracks (
        stable_id       TEXT PRIMARY KEY,
        stable_id_tier  TEXT NOT NULL CHECK
                          (stable_id_tier IN ('isrc','fingerprint','inferred')),
        title           TEXT,
        artists_json    TEXT,
        album           TEXT,
        isrc            TEXT,
        duration_ms     INTEGER,
        file_path       TEXT,
        content_hash    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
CREATE INDEX idx_tracks_isrc ON tracks(isrc) WHERE isrc IS NOT NULL;
CREATE INDEX idx_tracks_file_path ON tracks(file_path);
CREATE INDEX idx_track_vendor_ids_vendor ON track_vendor_ids(vendor, vendor_id);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_stable_id ON events(stable_id) WHERE stable_id IS NOT NULL;
CREATE INDEX idx_track_field_history_lookup ON track_field_history(stable_id, field_name, superseded_at);
CREATE INDEX idx_track_locations_stable ON track_locations(stable_id);
CREATE UNIQUE INDEX idx_track_locations_path ON track_locations(stable_id, kind, file_path) WHERE file_path IS NOT NULL;
CREATE UNIQUE INDEX idx_track_locations_url ON track_locations(stable_id, kind, remote_url) WHERE remote_url IS NOT NULL;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('track_field_history',0);
INSERT INTO "sqlite_sequence" VALUES('track_locations',0);
COMMIT;
