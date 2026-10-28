"""AGENT-31: the single-track loudness read refuses corrupt projection rows rather than relabeling them.

[if] a loudness_lufs row is ok without a number, or carries an unknown status [then] it raises [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.webui.server.track_loudness import NO_ANALYSIS, track_loudness


def _db(tmp_path: Path, value: object, status: str, reason: str | None = None) -> Path:
    path = tmp_path / "state.db"
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE analysis_projection (stable_id TEXT, field TEXT, value, status TEXT, reason TEXT)")
    conn.execute("INSERT INTO analysis_projection VALUES ('t1', 'loudness_lufs', ?, ?, ?)", (value, status, reason))
    conn.commit()
    conn.close()
    return path


def test_an_ok_row_serves_its_lufs_and_a_failed_row_its_reason(tmp_path: Path) -> None:
    assert track_loudness(_db(tmp_path, -9.5, "ok"), "t1").lufs == -9.5
    assert track_loudness(_db(tmp_path, -9.5, "ok"), "t2").reason == NO_ANALYSIS  # no row for t2


def test_a_failed_row_keeps_its_own_status_and_reason(tmp_path: Path) -> None:
    failed = track_loudness(_db(tmp_path, None, "failed", "decode error"), "t1")
    assert (failed.lufs, failed.status, failed.reason) == (None, "failed", "decode error")


@pytest.mark.parametrize(
    ("status", "valid", "invalid"),
    [
        ("ok", (-9.0, None), [("loud", None), (None, None), (b"\x00", None), (-9.0, "why")]),  # sqlite has no bool or NaN
        ("failed", (None, "decode error"), [(-9.0, "decode error"), (None, None), (None, "")]),
        ("missing", (None, "not analyzed"), [(-9.0, "not analyzed"), (None, None), (None, "")]),
    ],
)
def test_every_status_has_one_valid_row_shape(tmp_path: Path, status: str, valid: tuple, invalid: list) -> None:
    """AGENT-31 (Sol P1, the class): each status accepts exactly its shape and raises on every other one."""
    served = track_loudness(_db(tmp_path, valid[0], status, valid[1]), "t1")
    assert (served.status, served.lufs, served.reason) == (status, valid[0], valid[1])
    for value, reason in invalid:
        with pytest.raises(ValueError, match="not a valid"):
            track_loudness(_db(tmp_path, value, status, reason), "t1")


@pytest.mark.parametrize("status", ["weird", "OK", "", None])
def test_an_unknown_status_raises(tmp_path: Path, status: object) -> None:
    with pytest.raises(ValueError, match="unknown status"):
        track_loudness(_db(tmp_path, -9.0, status, None), "t1")  # type: ignore[arg-type]


def test_a_missing_store_or_schema_is_an_error_not_unmeasured(tmp_path: Path) -> None:
    """AGENT-31 (Sol P1): no state.db, or one without the projection schema, raises; only a missing row is unmeasured."""
    with pytest.raises(FileNotFoundError, match="loudness store missing"):
        track_loudness(tmp_path / "absent.db", "t1")
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).execute("CREATE TABLE other (x)").connection.commit()
    with pytest.raises(RuntimeError, match="has no analysis_projection"):
        track_loudness(bare, "t1")
    assert track_loudness(_db(tmp_path, -9.5, "ok"), "t2").reason == NO_ANALYSIS  # control: the store is there, the row is not


def test_a_broken_store_or_row_is_served_as_a_loudness_error_block(tmp_path: Path) -> None:
    """AGENT-31 (Sol P2 #7594): [if] the row is corrupt or the database unreadable [then] the single-track read serves
    ``loudness.status = "error"`` with the reason, never a 500; [else] an ok row still serves its LUFS."""
    from apps.webui.server.routes.tracks import loudness_block

    corrupt_row = loudness_block(_db(tmp_path, None, "ok"), "t1")
    assert (corrupt_row.status, corrupt_row.lufs) == ("error", None)
    assert (corrupt_row.reason or "").startswith("ValueError")
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a sqlite database at all" * 100)
    unreadable = loudness_block(garbage, "t1")
    assert unreadable.status == "error" and (unreadable.reason or "").startswith("DatabaseError")
    assert loudness_block(_db(tmp_path, -9.5, "ok"), "t1").lufs == -9.5  # control
