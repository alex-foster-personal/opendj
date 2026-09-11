"""lyric_verdict as a SYNCED row: stamps, tombstones, overrides, readers.

- if a store write skips stamp_and_log then the row syncs as epoch and loses
  every conflict it takes part in -- broken
- if upsert_verdict revives a purged row without resurrect=True then the
  licensing purge is undone by the next batch -- broken
- if a reader stops filtering the row's own tombstone OR the parent tracks
  tombstone then purged lyrics and deleted tracks keep showing up -- broken
- if an override stops surviving recompute, or stops beating the computed
  verdict in effective / list / count, then the human is not being obeyed --
  broken
- if the dataclass and the DDL column order drift then a positional read maps
  values into the wrong fields -- broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.lyrics import store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.lyrics.store import LyricStoreError
from apps.shared.state import sync_stamp

from .conftest import seed_track

DIGEST = "a" * 64


def _upsert(conn: sqlite3.Connection, stable_id: str, **overrides) -> None:
    fields: dict[str, object] = {
        "verdict": "vocal",
        "coverage_pct": 80.0,
        "source": "lrclib get",
        "language_iso3": "eng",
        "n_words": 4,
        "n_lines": 2,
        "pct_witness_red": 0.25,
        "pipeline_version": PIPELINE_VERSION,
        "words_content_hash": DIGEST,
        "computed_at": "2026-09-01T00:00:00.000000+00:00",
        "resurrect": False,
    }
    fields.update(overrides)
    store.upsert_verdict(conn, stable_id=stable_id, **fields)  # type: ignore[arg-type]


def test_columns_match_the_ddl_in_order(conn: sqlite3.Connection) -> None:
    """Explicit column lists are only safe while they mirror the table."""
    actual = tuple(
        row[1] for row in conn.execute("PRAGMA table_info(lyric_verdict)")
    )
    assert actual == store.COLUMNS


def test_a_write_is_stamped_and_logged(conn: sqlite3.Connection) -> None:
    seed_track(conn, "sid-1")
    _upsert(conn, "sid-1")
    verdict = store.get_verdict(conn, "sid-1")
    assert verdict is not None
    assert verdict.origin_device_id == sync_stamp.local_machine_id(conn)
    assert verdict.updated_at == sync_stamp.to_canonical(verdict.updated_at)
    assert verdict.computed_at == "2026-09-01T00:00:00.000000+00:00", (
        "computed_at is the pipeline's judgement time, not the sync stamp"
    )
    logged = conn.execute(
        "SELECT row_pk, updated_at, origin_device_id FROM local_changelog "
        "WHERE table_name = 'lyric_verdict'"
    ).fetchall()
    assert len(logged) == 1
    assert logged[0][0] == sync_stamp.encode_row_pk(("sid-1",))
    assert logged[0][1] == verdict.updated_at


def test_every_write_owns_its_transaction(conn: sqlite3.Connection) -> None:
    """open_rw is autocommit, so a store write that did not open its own
    transaction would leave the row committed and the changelog entry not."""
    seed_track(conn, "sid-1")
    _upsert(conn, "sid-1")
    assert not conn.in_transaction
    rows = conn.execute("SELECT COUNT(*) FROM lyric_verdict").fetchone()[0]
    logged = conn.execute(
        "SELECT COUNT(*) FROM local_changelog WHERE table_name='lyric_verdict'"
    ).fetchone()[0]
    assert (rows, logged) == (1, 1)


def test_an_override_survives_recompute_and_wins(conn: sqlite3.Connection) -> None:
    seed_track(conn, "sid-1")
    _upsert(conn, "sid-1")
    store.set_override(conn, stable_id="sid-1", override="no-lyrics", note="heard it")
    _upsert(conn, "sid-1", verdict="sparse", coverage_pct=20.0)
    verdict = store.get_verdict(conn, "sid-1")
    assert verdict is not None
    assert verdict.verdict == "sparse", "the computed value is preserved"
    assert verdict.override == "no-lyrics"
    assert verdict.effective == "no-lyrics"
    assert verdict.override_note == "heard it"


def test_unknown_is_not_an_overridable_value(conn: sqlite3.Connection) -> None:
    seed_track(conn, "sid-1")
    _upsert(conn, "sid-1")
    with pytest.raises(LyricStoreError, match="override 'unknown'"):
        store.set_override(conn, stable_id="sid-1", override="unknown", note=None)


def test_upsert_refuses_an_unknown_verdict(conn: sqlite3.Connection) -> None:
    seed_track(conn, "sid-1")
    with pytest.raises(LyricStoreError, match="verdict 'maybe'"):
        _upsert(conn, "sid-1", verdict="maybe")


def test_upsert_refuses_a_malformed_content_hash(conn: sqlite3.Connection) -> None:
    seed_track(conn, "sid-1")
    with pytest.raises(LyricStoreError, match="lowercase hex"):
        _upsert(conn, "sid-1", words_content_hash="A" * 64)


def test_a_tombstone_is_sticky(conn: sqlite3.Connection) -> None:
    """Purge, then re-ingest: the row must NOT come back live."""
    seed_track(conn, "sid-1")
    _upsert(conn, "sid-1")
    store.tombstone(conn, "sid-1")
    assert store.get_verdict(conn, "sid-1") is None
    with pytest.raises(LyricStoreError, match="refusing to resurrect"):
        _upsert(conn, "sid-1")
    assert store.get_verdict(conn, "sid-1") is None
    _upsert(conn, "sid-1", resurrect=True)
    assert store.get_verdict(conn, "sid-1") is not None


def test_set_override_distinguishes_absent_from_tombstoned(
    conn: sqlite3.Connection,
) -> None:
    seed_track(conn, "sid-1")
    with pytest.raises(LyricStoreError, match="no lyric_verdict row"):
        store.set_override(conn, stable_id="sid-1", override="vocal", note=None)
    _upsert(conn, "sid-1")
    store.tombstone(conn, "sid-1")
    with pytest.raises(LyricStoreError, match="was purged"):
        store.set_override(conn, stable_id="sid-1", override="vocal", note=None)


def test_readers_filter_the_parent_track_tombstone(conn: sqlite3.Connection) -> None:
    """FK CASCADE never fires (hard DELETE of tracks is forbidden), so a
    tombstoned track would otherwise keep a live-looking verdict."""
    seed_track(conn, "sid-live")
    seed_track(conn, "sid-gone")
    _upsert(conn, "sid-live")
    _upsert(conn, "sid-gone")
    conn.execute(
        "UPDATE tracks SET deleted_at = '2026-09-09T00:00:00.000000+00:00' "
        "WHERE stable_id = 'sid-gone'"
    )
    assert store.get_verdict(conn, "sid-gone") is None
    assert set(store.bulk_verdicts(conn, ["sid-live", "sid-gone"])) == {"sid-live"}
    assert [v.stable_id for v in store.list_verdicts(
        conn, limit=10, offset=0, verdict=None, order="suspect"
    )] == ["sid-live"]
    assert store.count_verdicts(conn) == {"vocal": 1}
    assert [v.stable_id for v in store.verdicts_by_source(conn, "lrclib")] == ["sid-live"]


def test_readers_filter_their_own_tombstone(conn: sqlite3.Connection) -> None:
    seed_track(conn, "sid-1")
    seed_track(conn, "sid-2")
    _upsert(conn, "sid-1")
    _upsert(conn, "sid-2", verdict="sparse")
    store.tombstone(conn, "sid-2")
    assert store.count_verdicts(conn) == {"vocal": 1}
    assert store.bulk_verdicts(conn, ["sid-1", "sid-2"]).keys() == {"sid-1"}
    assert store.verdicts_by_source(conn, "lrclib")[0].stable_id == "sid-1"


def test_bulk_verdicts_short_circuits_on_an_empty_list(
    conn: sqlite3.Connection,
) -> None:
    assert store.bulk_verdicts(conn, []) == {}


def test_list_orders_and_filters_on_the_effective_verdict(
    conn: sqlite3.Connection,
) -> None:
    for index, (stable_id, red) in enumerate(
        [("sid-a", 0.9), ("sid-b", 0.1), ("sid-c", None)]
    ):
        seed_track(conn, stable_id)
        _upsert(
            conn, stable_id, pct_witness_red=red, coverage_pct=float(index),
            computed_at=f"2026-09-0{index + 1}T00:00:00.000000+00:00",
        )
    suspect = store.list_verdicts(
        conn, limit=10, offset=0, verdict=None, order="suspect"
    )
    assert [v.stable_id for v in suspect] == ["sid-a", "sid-b", "sid-c"]
    recent = store.list_verdicts(conn, limit=10, offset=0, verdict=None, order="recent")
    assert [v.stable_id for v in recent] == ["sid-c", "sid-b", "sid-a"]
    store.set_override(conn, stable_id="sid-b", override="no-lyrics", note=None)
    filtered = store.list_verdicts(
        conn, limit=10, offset=0, verdict="no-lyrics", order="suspect"
    )
    assert [v.stable_id for v in filtered] == ["sid-b"]
    assert store.count_verdicts(conn) == {"vocal": 2, "no-lyrics": 1}


def test_list_refuses_an_unknown_order_or_verdict(conn: sqlite3.Connection) -> None:
    with pytest.raises(LyricStoreError, match="unknown order"):
        store.list_verdicts(conn, limit=1, offset=0, verdict=None, order="sideways")
    with pytest.raises(LyricStoreError, match="verdict 'maybe'"):
        store.list_verdicts(conn, limit=1, offset=0, verdict="maybe", order="suspect")


def test_store_never_hardcodes_a_database_path() -> None:
    """The router must hand in the cloudsync-style write connection."""
    source = Path(store.__file__).read_text(encoding="utf-8")
    assert "data/state/state.db" in source, "the docstring must name the trap"
    assert "open_rw(" not in source, "store.py opens no connection of its own"
