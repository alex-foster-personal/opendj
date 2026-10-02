"""Equivalence gate for the consolidated store schema.

The consolidated module (apps/engine_core/store/schema.py) must produce
BYTE-SEMANTICALLY the same tables and indexes as the ~17 legacy bootstraps
it replaces. This test proves that by building two databases:

* ``fresh``  -- one call to the consolidated ``apply_migrations``.
* ``legacy`` -- every legacy bootstrap function imported and CALLED, on
  temp DBs, exactly as production calls it.

Then it compares normalised ``sqlite_master`` SQL, object by object.

Direction of the gate: where the two disagree, the CONSOLIDATED module is
wrong and gets fixed. This is consolidation, not redesign -- the legacy
definition is the specification.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Everything below ``consolidated`` is a LEGACY bootstrap, imported from the
# module that owns it today. The test calls each one the way production does.
from apps.analysis.store import _ensure_analysis_tables
from apps.dedup.schema import ensure_schema as dedup_ensure_schema
from apps.engine_core.store import schema as consolidated
from apps.launcher.scripts.bootstrap_db import apply_launcher_migration
from apps.sets.state import _ensure_schema as sets_ensure_schema
from apps.shared.fingerprints import FingerprintCache
from apps.shared.hashing import HashCache
from apps.shared.pairings.schema_sql import (
    _PAIRINGS_DDL,
    _SMARTLISTS_DDL,
    ensure_phase08_tables,
)
from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.state.schema import INFRASTRUCTURE_TABLES
from apps.shared.state.schema import apply_migrations as legacy_state_migrations
from apps.spotify.state_writer import ensure_aux_tables
from apps.voice.settings import SettingsStore

# --- sqlite_master normalisation ------------------------------------------

# The normaliser lives in the schema module, not here: the runner's
# pre-adoption shape audit compares live objects against the ladder's own
# output and has to mean exactly what this gate means by "same shape". One
# definition, two callers.
_normalise = consolidated.normalize_object_sql


def _shadow_prefixes(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Name prefixes of FTS5 shadow tables, derived from the live vtabs.

    An ``fts5`` virtual table silently spawns ``<name>_data``, ``_idx``,
    ``_content``, ``_docsize`` and ``_config``. They are implementation
    detail of the one statement that made them, so they are excluded from
    the comparison rather than enumerated in the schema module.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND sql LIKE '%USING fts5%'"
    ).fetchall()
    return tuple(f"{r[0]}_" for r in rows)


def objects(conn: sqlite3.Connection) -> dict[str, str]:
    """``name -> normalised SQL`` for every real table/index in ``conn``.

    Excludes SQLite internals (``sqlite_sequence``, autoindexes) and FTS5
    shadow tables. Rows with NULL sql are implicit objects with no DDL.
    """
    shadows = _shadow_prefixes(conn)
    result: dict[str, str] = {}
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index')"
    ).fetchall()
    for name, sql in rows:
        if sql is None or str(name).startswith("sqlite_"):
            continue
        if any(str(name).startswith(prefix) for prefix in shadows):
            continue
        result[str(name)] = _normalise(str(sql))
    return result


# --- database builders ----------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def build_consolidated(tmp_path: Path) -> sqlite3.Connection:
    """A DB created solely by the consolidated runner (fresh-DB path)."""
    conn = _connect(tmp_path / "consolidated.db")
    version = consolidated.apply_migrations(conn)
    assert version == consolidated.SCHEMA_VERSION
    return conn


def build_legacy(tmp_path: Path) -> dict[str, str]:
    """Run every legacy bootstrap and merge what each one created.

    Bootstraps that legacy code points at the SHARED state.db run against one
    connection, in the order production runs them. Bootstraps that legacy
    code points at their own sidecar file (voice settings, dedup fallback,
    fingerprint cache, hash cache) run against their own file -- calling them
    the way production does is the whole point, so their DB layout is not
    rewritten to suit the test.
    """
    merged: dict[str, str] = {}

    # --- shared state.db path --------------------------------------------
    state_path = tmp_path / "state.db"
    state = _connect(state_path)
    legacy_state_migrations(state)          # apps/shared/state/schema.py
    _ensure_analysis_tables(state)          # apps/analysis/store.py
    ensure_phase08_tables(state)            # apps/shared/pairings/schema_sql.py
    apply_play_order_migrations(state)      # apps/shared/play_orders/schema.py
    from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
    apply_playlist_set_migrations(state)    # apps/shared/playlist_sets/schema.py
    ensure_aux_tables(state)                # apps/spotify/state_writer.py
    sets_ensure_schema(state, events_table="set_events")  # apps/sets/state.py
    state.commit()
    merged.update(objects(state))
    state.close()

    # apps/launcher/scripts/bootstrap_db.py -- reads the file back off disk,
    # so it has to run after the state connection has committed and closed.
    apply_launcher_migration(state_path)
    state = _connect(state_path)
    merged.update(objects(state))
    state.close()

    # --- sidecar files ----------------------------------------------------
    # apps/voice/settings.py -- SettingsStore bootstraps in __post_init__.
    SettingsStore(path=tmp_path / "settings.sqlite")
    settings = _connect(tmp_path / "settings.sqlite")
    merged.update(objects(settings))
    settings.close()

    # apps/dedup/schema.py -- returns its own open connection.
    dedup = dedup_ensure_schema(tmp_path / "phase7.sqlite")
    merged.update(objects(dedup))
    dedup.close()

    return merged


def build_legacy_caches(tmp_path: Path) -> dict[str, str]:
    """Run the REGENERABLE-cache bootstraps only.

    Split from ``build_legacy`` because these two domains left the durable
    ladder (F5): they are compared against ``apply_cache_migrations``, not
    against ``apply_migrations``. Each still runs against its own sidecar
    file, exactly as production calls it.
    """
    merged: dict[str, str] = {}

    # apps/shared/fingerprints.py -- FingerprintCache bootstraps in __init__.
    FingerprintCache(tmp_path / "fingerprints.sqlite")
    fingerprints = _connect(tmp_path / "fingerprints.sqlite")
    merged.update(objects(fingerprints))
    fingerprints.close()

    # apps/shared/hashing.py -- HashCache bootstraps in __init__.
    hashes = HashCache(tmp_path / "hashes.sqlite")
    merged.update(objects(hashes._conn))
    hashes.close()

    return merged


# --- the equivalence gate -------------------------------------------------


@pytest.fixture
def fresh(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = build_consolidated(tmp_path / "fresh")
    yield conn
    conn.close()


@pytest.fixture
def legacy(tmp_path: Path) -> dict[str, str]:
    return build_legacy(tmp_path / "legacy")


@pytest.fixture
def fresh_caches(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """The cache DB the engine will eventually keep separate from state.db."""
    conn = _connect(tmp_path / "fresh_caches" / "cache.db")
    consolidated.apply_cache_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def legacy_caches(tmp_path: Path) -> dict[str, str]:
    return build_legacy_caches(tmp_path / "legacy_caches")


def test_table_sets_are_identical(fresh: sqlite3.Connection) -> None:
    """The declared table registry is exactly what the runner creates.

    The legacy ``INFRASTRUCTURE_TABLES`` (``schema_meta`` and the v16
    ``schema_meta_markers``) are the only additions: migration bookkeeping,
    created by the runner rather than declared as domain data.
    """
    declared = set(consolidated.ALL_TABLES) | set(INFRASTRUCTURE_TABLES)
    created = _table_names(fresh)
    assert not declared - created, (
        f"consolidated schema is missing tables: {sorted(declared - created)}"
    )
    assert not created - declared, (
        f"consolidated schema invents tables: {sorted(created - declared)}"
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    shadows = _shadow_prefixes(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {
        str(r[0])
        for r in rows
        if not str(r[0]).startswith("sqlite_")
        and not any(str(r[0]).startswith(p) for p in shadows)
    }


def test_object_names_match_legacy(
    fresh: sqlite3.Connection, legacy: dict[str, str]
) -> None:
    """Same set of tables AND indexes on both sides.

    ``schema_meta`` is the one legitimate consolidated-only object beyond the
    legacy set: it is migration infrastructure the legacy state ladder also
    creates, so it appears on both sides anyway. Anything else appearing on
    one side only is drift.
    """
    fresh_names = set(objects(fresh))
    legacy_names = set(legacy)
    assert fresh_names - legacy_names == set(), (
        "consolidated schema creates objects no legacy bootstrap does: "
        f"{sorted(fresh_names - legacy_names)}"
    )
    legacy_only = legacy_names - fresh_names - LEGACY_ONLY_PLAYLIST_SETS
    assert legacy_only == set(), (
        "legacy bootstraps create objects the consolidated schema drops: "
        f"{sorted(legacy_only)}"
    )


@pytest.mark.parametrize("name", sorted(consolidated.ALL_TABLES))
def test_table_definition_matches_legacy(
    name: str, fresh: sqlite3.Connection, legacy: dict[str, str]
) -> None:
    """Table-by-table: the consolidated DDL is byte-semantically the legacy DDL."""
    fresh_objects = objects(fresh)
    assert name in legacy, f"no legacy bootstrap creates {name}"
    assert name in fresh_objects, f"consolidated schema does not create {name}"
    assert fresh_objects[name] == legacy[name], (
        f"{name} drifted from its legacy definition.\n"
        f"legacy:       {legacy[name]}\n"
        f"consolidated: {fresh_objects[name]}"
    )


def test_index_definitions_match_legacy(
    fresh: sqlite3.Connection, legacy: dict[str, str]
) -> None:
    """Index-by-index equivalence, including the partial-index WHERE clauses."""
    fresh_objects = objects(fresh)
    fresh_tables = _table_names(fresh)
    fresh_indexes = {
        name: sql for name, sql in fresh_objects.items() if name not in fresh_tables
    }
    assert fresh_indexes, "no indexes found -- the comparison would be vacuous"
    for name, sql in sorted(fresh_indexes.items()):
        assert name in legacy, f"consolidated index {name} has no legacy origin"
        assert sql == legacy[name], (
            f"index {name} drifted from its legacy definition.\n"
            f"legacy:       {legacy[name]}\n"
            f"consolidated: {sql}"
        )


def test_every_table_is_attributed_to_a_legacy_file() -> None:
    """No table may enter the consolidated schema without a named origin."""
    declared = {**consolidated.TABLES, **consolidated.CACHE_TABLES}
    for domain, tables in declared.items():
        assert domain in consolidated.LEGACY_SOURCES, (
            f"domain {domain!r} declares tables {tables} with no legacy source"
        )
        assert consolidated.LEGACY_SOURCES[domain].startswith("apps/")


# --- durability split (F5) ------------------------------------------------


def test_durable_ladder_excludes_the_regenerable_caches(
    fresh: sqlite3.Connection,
) -> None:
    """F5: regenerable caches must not sit inside the durable ladder.

    ``fingerprints`` and ``file_hashes`` are pure derived data -- every row is
    recomputable from the audio file it describes, and the legacy HashCache
    already DROPs and recreates the whole table on a version mismatch (O-12).
    A durable ladder never wipes; a domain whose legal recovery move IS a wipe
    therefore cannot live in it without making that promise false.
    """
    for table in consolidated.ALL_CACHE_TABLES:
        assert table not in consolidated.ALL_TABLES, (
            f"{table} is regenerable and must not be in the durable ladder"
        )
    created = _table_names(fresh)
    assert not created & set(consolidated.ALL_CACHE_TABLES), (
        "apply_migrations created cache tables in state.db: "
        f"{sorted(created & set(consolidated.ALL_CACHE_TABLES))}"
    )


def test_cache_migrations_create_every_declared_cache_table(
    fresh_caches: sqlite3.Connection,
) -> None:
    """F5: the DDL did not get dropped on the way out of the ladder."""
    created = _table_names(fresh_caches)
    assert created == set(consolidated.ALL_CACHE_TABLES), (
        f"cache DB holds {sorted(created)}, "
        f"declared {sorted(consolidated.ALL_CACHE_TABLES)}"
    )


def test_cache_migrations_are_idempotent(tmp_path: Path) -> None:
    """F5: same contract as the durable runner -- a second call changes nothing."""
    conn = _connect(tmp_path / "cache_twice.db")
    consolidated.apply_cache_migrations(conn)
    snapshot = objects(conn)
    consolidated.apply_cache_migrations(conn)
    assert objects(conn) == snapshot
    conn.close()


def test_cache_object_names_match_legacy(
    fresh_caches: sqlite3.Connection, legacy_caches: dict[str, str]
) -> None:
    """F5: moving the domain out of the ladder must not change what it builds."""
    assert set(objects(fresh_caches)) == set(legacy_caches)


@pytest.mark.parametrize("name", sorted(consolidated.ALL_CACHE_TABLES))
def test_cache_table_definition_matches_legacy(
    name: str, fresh_caches: sqlite3.Connection, legacy_caches: dict[str, str]
) -> None:
    """F5: cache tables are compared against apply_cache_migrations output.

    Same equivalence gate as the durable tables, run against the entry point
    that now owns them.
    """
    fresh_objects = objects(fresh_caches)
    assert name in legacy_caches, f"no legacy bootstrap creates {name}"
    assert name in fresh_objects, f"apply_cache_migrations does not create {name}"
    assert fresh_objects[name] == legacy_caches[name], (
        f"{name} drifted from its legacy definition.\n"
        f"legacy:       {legacy_caches[name]}\n"
        f"consolidated: {fresh_objects[name]}"
    )


def test_vendor_sidecar_tables_are_created_by_the_one_home(tmp_path: Path) -> None:
    """The rekordbox-side reversal tables have exactly one DDL home.

    Replaces test_vendor_sidecar_matches_rb_vendor (T3b D2). That test compared
    this module's DDL against a byte-identical copy in
    ``apps/webui/server/rb_vendor.py``; the copy is now deleted and the hot-cue
    writer provisions through :func:`ensure_vendor_sidecar_tables`, so there is
    no second definition left to drift from. What still needs pinning is that
    this entry point creates the tables it claims to.
    """
    conn = _connect(tmp_path / "vendor.db")
    consolidated.ensure_vendor_sidecar_tables(conn)
    created = objects(conn)
    conn.close()

    assert set(created) == set(consolidated.VENDOR_SIDECAR_TABLES)


# --- runner behaviour -----------------------------------------------------


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    """A second call changes nothing -- same version, same objects."""
    conn = _connect(tmp_path / "twice.db")
    first = consolidated.apply_migrations(conn)
    snapshot = objects(conn)
    second = consolidated.apply_migrations(conn)
    assert first == second == consolidated.SCHEMA_VERSION
    assert objects(conn) == snapshot
    conn.close()


def test_fresh_db_is_not_marked_as_adopted(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "born_consolidated.db")
    consolidated.apply_migrations(conn)
    assert not consolidated.was_adopted(conn)
    conn.close()


def test_adopts_an_existing_legacy_db_without_recreating_it(tmp_path: Path) -> None:
    """A live state.db keeps its rows; only the missing tables get created."""
    path = tmp_path / "existing.db"
    conn = _connect(path)
    legacy_state_migrations(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, "
        "created_at, updated_at) VALUES ('sid1', 'isrc', 'Keep Me', "
        "'/music/keep.aiff', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()

    before = objects(conn)
    assert "pairings" not in before, "fixture assumption: legacy state.db has no pairings"

    consolidated.apply_migrations(conn)

    assert consolidated.was_adopted(conn), "adoption must be recorded in schema_meta"
    rows = conn.execute("SELECT title FROM tracks").fetchall()
    assert rows == [("Keep Me",)], "adoption must not recreate existing tables"
    after = objects(conn)
    for name, sql in before.items():
        assert after[name] == sql, f"adoption rewrote the existing {name}"
    assert "pairings" in after, "adoption must create the tables that were missing"
    conn.close()


def test_adoption_replays_the_track_locations_backfill(tmp_path: Path) -> None:
    """A DB adopted from legacy v3 gets the v4 data backfill, not just the table."""
    path = tmp_path / "v3.db"
    conn = _connect(path)
    legacy_state_migrations(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, "
        "created_at, updated_at) VALUES ('sid2', 'isrc', 'Backfill Me', "
        "'/music/backfill.aiff', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.execute("DELETE FROM track_locations")
    conn.commit()

    consolidated.apply_migrations(conn)

    rows = conn.execute(
        "SELECT stable_id, kind, role, file_path FROM track_locations"
    ).fetchall()
    assert rows == [("sid2", "local", "primary", "/music/backfill.aiff")]
    conn.close()


def test_adoption_refuses_a_pre_webui_track_fields(tmp_path: Path) -> None:
    """Fail fast rather than adopt a table shape adoption cannot repair."""
    conn = _connect(tmp_path / "v1.db")
    # The legacy v1 track_fields: source CHECK without 'webui'. Adoption only
    # creates missing objects, so it must refuse instead of silently leaving
    # the narrow enum in place.
    conn.execute(
        "CREATE TABLE track_fields ("
        "stable_id TEXT NOT NULL, field_name TEXT NOT NULL, "
        "value_json TEXT NOT NULL, "
        "source TEXT NOT NULL CHECK (source IN ('mik','manual')), "
        "modified_at TEXT NOT NULL, PRIMARY KEY (stable_id, field_name))"
    )
    conn.commit()

    with pytest.raises(consolidated.SchemaAdoptionError, match="webui"):
        consolidated.apply_migrations(conn)
    conn.close()


def _v1_track_field_history(conn: sqlite3.Connection) -> None:
    """The legacy v1 ``track_field_history``: natural PK, no surrogate ``id``.

    Verbatim from ``apps/shared/state/schema.py`` _V1. The v1->v2 step REBUILDS
    this table to add the surrogate key, so a DB still carrying this shape is
    one adoption cannot repair.
    """
    conn.execute(
        "CREATE TABLE track_field_history ("
        "stable_id TEXT NOT NULL, field_name TEXT NOT NULL, "
        "value_json TEXT NOT NULL, source TEXT NOT NULL, confidence REAL, "
        "modified_at TEXT NOT NULL, superseded_at TEXT NOT NULL, "
        "PRIMARY KEY (stable_id, field_name, superseded_at))"
    )


def _v5_track_fields(conn: sqlite3.Connection) -> None:
    """The post-v3 ``track_fields`` -- widened CHECK, so its own probe passes."""
    conn.execute(
        "CREATE TABLE track_fields ("
        "stable_id TEXT NOT NULL, field_name TEXT NOT NULL, "
        "value_json TEXT NOT NULL, "
        "source TEXT NOT NULL CHECK (source IN ('mik','rekordbox','djay',"
        "'serato','traktor','open-dj-tool','manual','inferred','webui')), "
        "confidence REAL, modified_at TEXT NOT NULL, "
        "PRIMARY KEY (stable_id, field_name))"
    )


def test_adoption_refuses_a_pre_rebuild_track_field_history(tmp_path: Path) -> None:
    """F1: track_field_history needs a shape probe of its own.

    Only ``track_fields`` was probed, so a DB carrying the v1 natural-PK
    ``track_field_history`` adopted silently and could never be repaired: the
    runner stamps the legacy counter to 5, after which the legacy ladder --
    the only code that can REBUILD the table -- short-circuits forever.
    """
    conn = _connect(tmp_path / "v1_history.db")
    _v5_track_fields(conn)
    _v1_track_field_history(conn)
    conn.commit()

    with pytest.raises(consolidated.SchemaAdoptionError, match="track_field_history"):
        consolidated.apply_migrations(conn)
    conn.close()


def test_adoption_probes_run_when_the_legacy_counter_reads_zero(tmp_path: Path) -> None:
    """F1: an empty schema_meta must not buy a bypass of the shape probes.

    ``_legacy_version`` returns 0 for both "brand new file" and "v1-era file
    whose counter was never written" (or was truncated). The floor guard is
    written ``0 < legacy < MIN``, so the second case slipped past it. The
    probes, not the counter, are what decide adoptability.
    """
    conn = _connect(tmp_path / "counterless_v1.db")
    conn.execute(
        "CREATE TABLE schema_meta (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    _v5_track_fields(conn)
    _v1_track_field_history(conn)
    conn.commit()
    assert consolidated._legacy_version(conn) == 0, "fixture: counter must read 0"

    with pytest.raises(consolidated.SchemaAdoptionError) as caught:
        consolidated.apply_migrations(conn)
    message = str(caught.value)
    assert "track_field_history" in message, "the refusal must name the object"
    assert "apply_migrations" in message, "the refusal must carry remediation text"
    conn.close()


def test_refused_adoption_leaves_the_legacy_counter_unstamped(tmp_path: Path) -> None:
    """F1: the damage being prevented is one-way -- stamping 1..5 on a bad shape.

    Once ``schema_meta`` reads 5 the legacy runner short-circuits, so the
    rebuild can never run. A refused adoption must leave the counter alone.
    """
    conn = _connect(tmp_path / "unstamped.db")
    _v5_track_fields(conn)
    _v1_track_field_history(conn)
    conn.commit()

    with pytest.raises(consolidated.SchemaAdoptionError):
        consolidated.apply_migrations(conn)

    rows = conn.execute("SELECT version FROM schema_meta").fetchall()
    assert rows == [], f"refused adoption stamped versions {rows}"
    conn.close()


def test_adoption_refuses_a_db_below_the_adoptable_floor(tmp_path: Path) -> None:
    """schema_meta stamped at v1 means table rebuilds are still owed."""
    conn = _connect(tmp_path / "stamped_v1.db")
    conn.execute(
        "CREATE TABLE schema_meta (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')"
    )
    conn.commit()

    with pytest.raises(consolidated.SchemaAdoptionError, match="v1"):
        consolidated.apply_migrations(conn)
    conn.close()


def test_existing_objects_includes_views(tmp_path: Path) -> None:
    """F2: the object census must see views, not just tables.

    It filtered ``type = 'table'``, which is precisely the blind spot that
    lets a colliding view through.
    """
    conn = _connect(tmp_path / "with_view.db")
    conn.execute("CREATE VIEW pairings AS SELECT 1 AS from_stable_id")
    conn.commit()
    assert "pairings" in consolidated.existing_objects(conn)
    conn.close()


def test_adoption_refuses_a_view_colliding_with_a_ladder_table(tmp_path: Path) -> None:
    """F2 (R3 #6): a view named like a ladder table must be refused, not skipped.

    ``CREATE TABLE IF NOT EXISTS pairings`` is a silent no-op when a VIEW
    called ``pairings`` already exists -- SQLite's namespace is shared. The
    runner would report success and leave the file with a view where the
    engine expects a writable table. Real DBs already carry views
    (``tracks_available`` and friends), so this is a live collision class.
    """
    conn = _connect(tmp_path / "view_collision.db")
    conn.execute("CREATE VIEW pairings AS SELECT 1 AS from_stable_id")
    conn.commit()

    with pytest.raises(consolidated.SchemaAdoptionError, match="pairings"):
        consolidated.apply_migrations(conn)
    conn.close()


def test_adoption_refuses_a_drifted_table_shape(tmp_path: Path) -> None:
    """F2 (R3 #3): name-presence is not shape-presence.

    The pre-adoption audit only checked that expected names existed. A
    pre-existing table with the right name and the wrong columns passed, and
    every ``IF NOT EXISTS`` statement then no-opped over it.
    """
    conn = _connect(tmp_path / "drifted.db")
    # settings, but ``value`` lost its NOT NULL.
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()

    with pytest.raises(consolidated.SchemaAdoptionError) as caught:
        consolidated.apply_migrations(conn)
    message = str(caught.value)
    assert "settings" in message, "the refusal must name the object"
    assert "expected" in message and "found" in message, (
        "the refusal must show expected vs found shape"
    )
    assert "value TEXT NOT NULL" in message, "expected shape must be quoted back"
    conn.close()


def test_shape_audit_accepts_a_real_legacy_state_db(tmp_path: Path) -> None:
    """F2 guard-rail: the audit must not reject the DBs it exists to protect.

    A legacy-built state.db is the adoption target. If the normalised shapes
    disagreed the audit would refuse every real file, so this pins the
    consolidated DDL to what the legacy ladder actually stores.
    """
    conn = _connect(tmp_path / "legacy_real.db")
    legacy_state_migrations(conn)
    _ensure_analysis_tables(conn)
    ensure_phase08_tables(conn)
    conn.commit()

    consolidated.apply_migrations(conn)
    assert consolidated.was_adopted(conn)
    conn.close()


def test_shape_audit_adopts_a_legacy_pairings_table_without_snapshot_json(
    tmp_path: Path,
) -> None:
    """A state.db whose pairings table predates snapshot_json (PAIR-04) still
    adopts: the column is added before the audit, as smartlists.deleted_at is,
    and its rows survive."""
    conn = _connect(tmp_path / "legacy_pre_snapshot.db")
    legacy_state_migrations(conn)
    _ensure_analysis_tables(conn)
    # The exact pre-PAIR-04 DDL: the shipped statement minus its snapshot line.
    pre_snapshot_ddl = _PAIRINGS_DDL[0].replace("        snapshot_json  TEXT,\n", "")
    assert "snapshot_json" not in pre_snapshot_ddl
    conn.execute(pre_snapshot_ddl)
    conn.execute(
        "INSERT INTO pairings VALUES ('a', 'b', 'into', 'manual', 'kept', NULL, "
        "'2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    # The rest of the pre-PAIR-04 curation DDL, without ensure_phase08_tables
    # (which now adds the column itself and would hide the adoption path).
    for stmt in (*_PAIRINGS_DDL[1:], *_SMARTLISTS_DDL):
        conn.execute(stmt)
    conn.commit()

    consolidated.apply_migrations(conn)
    assert consolidated.was_adopted(conn)
    assert conn.execute("SELECT notes, snapshot_json FROM pairings").fetchall() == [
        ("kept", None)
    ]
    conn.close()


class _RollbackHostileConnection(sqlite3.Connection):
    """A connection whose ROLLBACK fails while a transaction is still live.

    Stands in for the case the ``in_transaction`` guard alone cannot cover: a
    connection broken badly enough that the undo itself errors.
    """

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
        if sql.strip().upper().startswith("ROLLBACK"):
            raise sqlite3.OperationalError("simulated rollback failure")
        return super().execute(sql, *args)


def test_rollback_after_an_auto_rollback_does_not_mask_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 (R3 #2): the caller must see the failure that actually happened.

    SQLite auto-rolls-back on SQLITE_FULL and SQLITE_IOERR. The runner's
    ``except`` then issued an unconditional ROLLBACK, which raised "cannot
    rollback - no transaction is active", and THAT replaced the real error on
    the way out -- so a full disk was reported as a transaction-state
    complaint.
    """
    conn = _connect(tmp_path / "auto_rolled_back.db")

    def _explode(target: sqlite3.Connection) -> None:
        target.execute("ROLLBACK")  # what SQLite does for us on IOERR/FULL
        raise RuntimeError("simulated disk I/O error")

    monkeypatch.setattr(consolidated, "_stamp_legacy_counters", _explode)

    with pytest.raises(RuntimeError, match="simulated disk I/O error"):
        consolidated.apply_migrations(conn)
    conn.close()


def test_a_failing_rollback_is_reported_but_never_swallows_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: guarding is not silently ignoring -- a failed undo must still surface.

    The original exception propagates unchanged; the rollback failure rides
    along as a note so a partially applied migration is never invisible.
    """
    path = tmp_path / "hostile.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), factory=_RollbackHostileConnection)

    def _explode(target: sqlite3.Connection) -> None:
        raise RuntimeError("simulated ladder failure")

    monkeypatch.setattr(consolidated, "_stamp_legacy_counters", _explode)

    with pytest.raises(RuntimeError, match="simulated ladder failure") as caught:
        consolidated.apply_migrations(conn)
    notes = " ".join(getattr(caught.value, "__notes__", []))
    assert "rollback" in notes.lower(), (
        f"the failed rollback must be reported alongside the cause, got {notes!r}"
    )
    assert "simulated rollback failure" in notes
    conn.close()


class _RecordingConnection(sqlite3.Connection):
    """Records every statement executed, so the lock mode can be asserted."""

    statements: list[str]

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
        self.statements.append(sql.strip())
        return super().execute(sql, *args)


def test_migration_takes_the_write_lock_up_front(tmp_path: Path) -> None:
    """F4 (R3 #7): the runner is read-then-write, so it must BEGIN IMMEDIATE.

    A plain ``BEGIN`` opens DEFERRED: the read snapshot is taken first and the
    write lock is only requested at the first write. If another connection
    takes RESERVED in that window, SQLite returns SQLITE_BUSY on the upgrade
    and does NOT retry it, whatever busy_timeout says, because retrying an
    upgrade can deadlock. Taking the write lock at BEGIN makes the wait
    honour the timeout instead.
    """
    path = tmp_path / "lockmode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), factory=_RecordingConnection)
    conn.statements = []
    consolidated.apply_migrations(conn)

    begins = [s for s in conn.statements if s.upper().startswith("BEGIN")]
    assert begins == ["BEGIN IMMEDIATE"], (
        f"runner must open its transaction IMMEDIATE, issued {begins}"
    )
    conn.close()


def _impatient_connect(path: Path) -> sqlite3.Connection:
    """A connection with NO caller-supplied busy timeout.

    ``sqlite3.connect`` defaults to ``timeout=5.0`` and stamps busy_timeout to
    5000ms, so a test using the default would be measuring Python's default
    rather than the runner. ``timeout=0`` is what a caller that tunes its own
    connections can hand us, and it is the case the runner has to cover: the
    migration's contention behaviour must not depend on how someone else built
    the connection.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path), timeout=0)


def test_migration_sets_its_own_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: a contended DB must wait for the lock, not fail instantly.

    The runner inherited whatever busy timeout the caller happened to set. On
    a connection opened ``timeout=0`` that is no wait at all, so a concurrent
    writer made ``apply_migrations`` fail in 0.000s with a bare 'database is
    locked' -- indistinguishable, to the caller, from a permanently unusable
    file. The runner now stamps its own timeout instead of inheriting one.
    """
    monkeypatch.setattr(consolidated, "BUSY_TIMEOUT_MS", 500)
    path = tmp_path / "contended.db"
    holder = _connect(path)
    holder.execute("CREATE TABLE canary (x INTEGER)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO canary VALUES (1)")

    victim = _impatient_connect(path)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        consolidated.apply_migrations(victim)
    waited = time.monotonic() - started

    assert waited >= 0.4, (
        f"gave up after {waited:.3f}s -- the 500ms busy timeout was not applied"
    )
    victim.close()
    holder.rollback()
    holder.close()


def test_migration_succeeds_when_the_lock_clears_within_the_timeout(
    tmp_path: Path,
) -> None:
    """F4: the timeout is there to let contention resolve, not just to delay."""
    path = tmp_path / "transient_lock.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Released from a second thread, so this one opts out of the same-thread check.
    holder = sqlite3.connect(str(path), check_same_thread=False)
    holder.execute("CREATE TABLE canary (x INTEGER)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO canary VALUES (1)")

    released = threading.Event()

    def _release() -> None:
        time.sleep(0.3)
        holder.rollback()
        released.set()

    releaser = threading.Thread(target=_release)
    releaser.start()
    try:
        victim = _impatient_connect(path)
        version = consolidated.apply_migrations(victim)
        assert version == consolidated.SCHEMA_VERSION
        assert released.is_set(), "fixture: the lock should have been held first"
        victim.close()
    finally:
        releaser.join()
        holder.close()


# --- the real-DB invariant ------------------------------------------------

REAL_STATE_DB = Path(
    os.environ.get(
        "ODJ_REAL_STATE_DB",
        "/Users/old/code/music-dj-tools-wt-rebuild-agentB-from-Fable-data"
        "/state/state.db",
    )
)
"""A live state.db to adopt. Overridable; skipped when absent.

Always copied before use -- this suite must never touch the real file.
"""


LEDGER_TABLES = frozenset({"schema_meta", "play_orders_schema_meta"})

# SET-05 playlist_sets authority is not yet inlined in the consolidated
# engine_core schema (ADR-0016); legacy bootstrap applies it additively.
LEGACY_ONLY_PLAYLIST_SETS = frozenset(
    {
        "playlist_sets",
        "playlist_set_entries",
        "playlist_set_runs",
        "playlist_sets_schema_meta",
        "idx_playlist_sets_playlist",
        "idx_playlist_set_runs_set",
    }
)
"""The two migration counters. Not domain data -- adoption is SUPPOSED to write
here, and the module excludes ``schema_meta`` from ``TABLES`` for exactly this
reason. Stamping ``play_orders_schema_meta`` is what makes the legacy play-order
runner a no-op instead of a second competing bootstrap."""


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = sorted(_table_names(conn))
    return {
        name: int(conn.execute(f"SELECT COUNT(*) FROM '{name}'").fetchone()[0])
        for name in names
    }


@pytest.mark.skipif(
    not REAL_STATE_DB.exists(), reason=f"no live state DB at {REAL_STATE_DB}"
)
def test_real_state_db_adopts_without_changing_a_single_row(tmp_path: Path) -> None:
    """The invariant the whole runner exists to protect.

    A real library adopts: every pre-existing object keeps its shape, every
    pre-existing table keeps its exact row count, and the missing domains are
    created empty. This is the test that the hardening had to not break -- the
    shape audit in particular could have refused every real file if the
    consolidated DDL disagreed with what the legacy ladder actually stores.

    Runs against a COPY. The original is never opened for writing.
    """
    working = tmp_path / "real_state_copy.db"
    working.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_STATE_DB, working)

    conn = _connect(working)
    # Legacy v6 REBUILDS track_locations (integer id -> location_id TEXT +
    # machine_id), and adoption only creates missing objects, so a real
    # v5-era file must run the legacy ladder first -- the exact remediation
    # _assert_adoptable names. The ladder must not change a row count either.
    pre_ladder_counts = _row_counts(conn)
    legacy_state_migrations(conn)
    ladder_counts = _row_counts(conn)
    for name, before in pre_ladder_counts.items():
        if name in LEDGER_TABLES:
            continue  # the ladder is SUPPOSED to stamp its own counter
        assert ladder_counts[name] == before, (
            f"legacy ladder changed the row count of {name}"
        )

    before_objects = objects(conn)
    before_counts = _row_counts(conn)
    before_legacy_version = int(
        conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_meta WHERE version < ?",
            (consolidated.VERSION_OFFSET,),
        ).fetchone()[0]
    )
    assert before_counts.get("tracks", 0) > 0, "fixture: the live DB should have tracks"

    consolidated.apply_migrations(conn)

    assert consolidated.was_adopted(conn), "a live DB must take the adoption path"

    after_objects = objects(conn)
    for name, sql in before_objects.items():
        assert after_objects[name] == sql, f"adoption rewrote the existing {name}"

    after_counts = _row_counts(conn)
    changed = {
        name: (before, after_counts[name])
        for name, before in before_counts.items()
        if after_counts[name] != before and name not in LEDGER_TABLES
    }
    assert not changed, f"adoption changed row counts: {changed}"

    # In particular the v4 track_locations backfill must find nothing to do:
    # it is INSERT OR IGNORE, so on a DB that already ran it, it is a no-op.
    assert after_counts["track_locations"] == before_counts["track_locations"]

    stamped = {
        int(row[0])
        for row in conn.execute(
            "SELECT version FROM schema_meta WHERE version >= ?",
            (consolidated.VERSION_OFFSET,),
        ).fetchall()
    }
    assert stamped == {
        consolidated.ADOPTION_VERSION,
        consolidated.VERSION_OFFSET + consolidated.SCHEMA_VERSION,
    }, f"adoption wrote unexpected ledger rows: {sorted(stamped)}"
    # Two consolidated markers, PLUS one row per legacy ladder step this file
    # still owed. A sample DB captured before a legacy version bump is behind
    # by definition, and adoption is right to carry it forward -- pinning this
    # at +2 would only hold while the sample happened to sit at the terminal
    # legacy version, and would fail every future bump for the wrong reason.
    legacy_steps_owed = max(
        0, consolidated.LEGACY_SHARED_STATE_VERSION - before_legacy_version
    )
    assert after_counts["schema_meta"] == (
        before_counts["schema_meta"] + 2 + legacy_steps_owed
    ), (
        "adoption should add the two marker rows plus one row per legacy step "
        f"it had to run (owed {legacy_steps_owed} from v{before_legacy_version})"
    )

    created = set(after_counts) - set(before_counts) - LEDGER_TABLES
    non_empty = {n: after_counts[n] for n in created if after_counts[n]}
    assert non_empty == {}, (
        f"newly created tables should be empty, got rows in {non_empty}"
    )
    conn.close()


def test_legacy_runners_become_no_ops_after_consolidation(tmp_path: Path) -> None:
    """The legacy counters are stamped, so their runners do not re-bootstrap.

    This is the property that lets the scattered bootstraps stay in the tree
    during integration without racing the consolidated one.
    """
    conn = _connect(tmp_path / "stamped.db")
    consolidated.apply_migrations(conn)
    snapshot = objects(conn)

    # Both legacy runners short-circuit on ``current >= SCHEMA_VERSION`` and
    # return whatever MAX(version) they see -- for the shared-state runner
    # that is now the consolidated stamp, which is the point.
    assert legacy_state_migrations(conn) >= consolidated.LEGACY_SHARED_STATE_VERSION
    assert apply_play_order_migrations(conn) == 1
    assert objects(conn) == snapshot
    conn.close()
