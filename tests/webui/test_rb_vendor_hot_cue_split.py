"""Regression pins for the T3b S4 hot-cue split fixes.

These cover the defects the decomposition map assigns to this slice, which the
inherited suite (tests/webui/test_rb_hot_cue_write.py) does not and cannot
catch: it pins behaviour through the public API, and every fix here is
invisible from there by construction.

Regression one-liners:
  - if the hot-cue sidecar DDL is declared anywhere but engine_core store
    schema then broken (D2)
  - if a hot-cue write does not provision its sidecar through the schema
    module's one home then broken (D2)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store import schema as store_schema
from apps.webui.server import rb_vendor
from apps.webui.server.rb_vendor_pkg import reversal as rb_reversal

pytestmark = pytest.mark.requirement("CAT-05")

VENDOR_ID = "126790091"
SIDECAR_TABLES = ("rb_hot_cue_reversal", "rb_hot_cue_slot_revision")


def _make_master_plain_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
            "Length INTEGER, rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdCue ("
            "ID VARCHAR(255) PRIMARY KEY, ContentID VARCHAR(255), "
            "InMsec INTEGER, InFrame INTEGER, InMpegFrame INTEGER, "
            "InMpegAbs INTEGER, OutMsec INTEGER, OutFrame INTEGER, "
            "Kind INTEGER, Color INTEGER, ColorTableIndex INTEGER, "
            "ActiveLoop INTEGER, Comment VARCHAR(255), BeatLoopSize INTEGER, "
            "rb_local_deleted TINYINT(1) DEFAULT 0, "
            "created_at DATETIME, updated_at DATETIME)"
        )
        conn.execute(
            "INSERT INTO djmdContent (ID, Length) VALUES (?, ?)", (VENDOR_ID, 300)
        )
        conn.commit()
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {name for (name,) in rows}


@pytest.fixture
def master_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "master.plain.db"
    _make_master_plain_db(path)
    monkeypatch.setattr(rb_vendor, "MASTER_PLAIN_DB", path)
    return path


def _revision(slots: list[dict[str, object]], slot: str) -> str:
    return next(row["revision"] for row in slots if row["slot"] == slot)


# ----- D2: the sidecar DDL has exactly one home ----------------------------


def test_no_hot_cue_sidecar_ddl_outside_the_schema_module() -> None:
    """Nothing under the rb_vendor family may re-declare the sidecar tables.

    The duplicate that ``_ensure_reversal_tables`` held is the defect D2
    names; a drift test used to keep the two copies level instead of removing
    one. Scanning source rather than asserting on a symbol is deliberate --
    a reintroduced copy under a new name is the same defect.
    """
    server = Path(rb_vendor.__file__).parent
    offenders = [
        f"{path}: {table}"
        for path in sorted(server.rglob("*.py"))
        for table in SIDECAR_TABLES
        if f"CREATE TABLE IF NOT EXISTS {table}" in path.read_text()
    ]
    assert offenders == [], (
        "hot-cue sidecar DDL must only live in apps/engine_core/store/schema.py "
        f"(VENDOR_SIDECAR_DDL); found copies at {offenders}"
    )


def test_ensure_reversal_tables_is_gone() -> None:
    assert not hasattr(rb_vendor, "_ensure_reversal_tables")
    assert not hasattr(rb_reversal, "_ensure_reversal_tables")


def test_hot_cue_save_provisions_the_sidecar_through_the_schema_module(
    master_db: Path,
) -> None:
    assert _tables(master_db).isdisjoint(SIDECAR_TABLES), "fixture must start bare"
    rb_vendor.save_hot_cue(
        VENDOR_ID,
        "A",
        1_000,
        expected_revision=_revision(rb_vendor.fetch_hot_cue_slots(VENDOR_ID), "A"),
    )
    assert set(SIDECAR_TABLES).issubset(_tables(master_db))
    assert set(store_schema.VENDOR_SIDECAR_TABLES) == set(SIDECAR_TABLES)
