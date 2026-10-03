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
  - if either rekordbox writer stops using the one shared frame conversion
    then broken (D3)
  - if the 44.1 kHz frame heuristic changes value then every InFrame this
    repo has ever written is inconsistent and broken (D3)
  - if reading the hot-cue bank takes a write transaction then broken (D5)
  - if reading the hot-cue bank provisions sidecar rows then broken (D5)
  - if the read-only slot read reports different revisions than the old
    provisioning read then broken (D5)
  - if a slot's revision stops changing across a save then the CAS token is
    no longer bound to slot state and broken (D5)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.adapters import rekordbox as rb_adapter
from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox import reversal as rb_reversal
from apps.adapters.rekordbox import writer as rb_writer
from apps.engine_core.store import schema as store_schema
from apps.shared import rb_frames
from apps.webui.server import rb_vendor

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

VENDOR_ID = "126790091"
SIDECAR_TABLES = ("rb_hot_cue_reversal", "rb_hot_cue_slot_revision")


def _make_master_plain_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
            "FolderPath VARCHAR(255), Length INTEGER, "
            "rb_local_deleted TINYINT(1) DEFAULT 0)"
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
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", path)
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

    Both trees are scanned because S8 split the family across them: the
    hot-cue cluster now lives in ``apps/adapters/rekordbox/`` while the rest
    of the vendor surface is still staged under ``apps/webui/server/``.
    Scanning only one would let a copy reappear in the other.
    """
    trees = (Path(rb_vendor.__file__).parent, Path(rb_adapter.__file__).parent)
    offenders = [
        f"{path}: {table}"
        for tree in trees
        for path in sorted(tree.rglob("*.py"))
        for table in SIDECAR_TABLES
        if f"CREATE TABLE IF NOT EXISTS {table}" in path.read_text(encoding="utf-8")
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


# ----- D3: one _msec_to_frame ----------------------------------------------


def test_both_rekordbox_writers_share_one_frame_conversion() -> None:
    """Identity, not equality: two functions that agree today can diverge.

    rb_vendor's copy documented itself as "same 44.1 kHz heuristic as
    apps/sync/rb_writer.py" and the two stayed byte-identical by luck, for as
    long as nobody edited one of them.
    """
    from apps.sync import rb_writer as sync_rb_writer

    assert sync_rb_writer._msec_to_frame is rb_frames.msec_to_frame
    assert rb_writer.msec_to_frame is rb_frames.msec_to_frame


def test_the_one_home_is_importable_without_the_web_layer() -> None:
    """apps.sync may not import apps.webui (.importlinter, hard-fail).

    That was the original reason the shared core holds the definition rather
    than the decomposition map's adapters/rekordbox/cues.py: cues.py was
    staged under apps/webui/server. S8 has since moved it to
    apps/adapters/rekordbox/, so the contract no longer objects -- but cues.py
    still imports fastapi (the map's errors.py is an unbuilt S0 carry-over),
    and folding the primitive back into it would pull the web framework into
    apps.sync. The indirection survives until that debt is paid.
    """
    assert rb_frames.__name__.startswith("apps.shared.")


@pytest.mark.parametrize(
    ("msec", "frame"),
    [(0, 0), (1_000, 441), (12_345, 5_444)],
)
def test_msec_to_frame_keeps_the_44_1khz_heuristic(msec: int, frame: int) -> None:
    """The surviving definition still answers what both copies answered.

    Same cases tests/test_rb_writer.py pins on the apps/sync side, asserted
    here against the module that now owns the conversion.
    """
    assert rb_frames.msec_to_frame(msec) == frame


# ----- D5: reading the hot-cue bank does not write --------------------------



def test_fetch_hot_cue_slots_takes_no_write_transaction(
    master_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GET of the hot-cue bank must not touch the user's live rekordbox DB.

    Before D5 this opened read-write, ran DDL and an INSERT OR IGNORE inside
    its own BEGIN IMMEDIATE, so every read of eight slots took an exclusive
    lock on master.plain.db.
    """
    statements: list[str] = []
    open_ro = rb_vendor._open_ro

    def traced(path: Path, label: str) -> sqlite3.Connection:
        conn = open_ro(path, label)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(rb_vendor, "_open_ro", traced)
    monkeypatch.setattr(
        rb_vendor,
        "_open_rw",
        lambda path, label: pytest.fail("fetch_hot_cue_slots opened read-write"),
    )
    rb_vendor.fetch_hot_cue_slots(VENDOR_ID)

    assert statements, "trace callback saw nothing; the test is not measuring"
    # Verb of each statement, not a substring scan: column names like
    # `updated_at` and `rb_local_deleted` contain mutating verbs.
    verbs = {stmt.split(maxsplit=1)[0].upper() for stmt in statements}
    assert verbs <= {"SELECT", "PRAGMA"}, f"read path issued {verbs - {'SELECT', 'PRAGMA'}}"


def test_fetch_hot_cue_slots_provisions_nothing(master_db: Path) -> None:
    slots = rb_vendor.fetch_hot_cue_slots(VENDOR_ID)
    assert len(slots) == len(rb_vendor.HOT_CUE_SLOTS)
    assert all(row["cue"] is None for row in slots)
    assert _tables(master_db).isdisjoint(SIDECAR_TABLES)


def test_read_only_revisions_match_the_provisioning_read(master_db: Path) -> None:
    """The generation a read reports equals the one provisioning would create.

    This is the whole argument for deleting the write: an absent sidecar row
    and a row at generation 0 are the same state, so both paths hash to the
    same CAS token. Compared against the real provisioning helper, not a
    restatement of it.
    """
    read_only = rb_vendor.fetch_hot_cue_slots(VENDOR_ID)

    conn = sqlite3.connect(str(master_db))
    try:
        provisioned = {
            kind: rb_reversal._slot_generation(conn, VENDOR_ID, kind)
            for kind in range(1, len(rb_vendor.HOT_CUE_SLOTS) + 1)
        }
        conn.commit()
    finally:
        conn.close()

    after_provisioning = rb_vendor.fetch_hot_cue_slots(VENDOR_ID)
    assert provisioned == dict.fromkeys(provisioned, 0)
    assert read_only == after_provisioning


def test_read_slot_generations_reports_live_bumps(master_db: Path) -> None:
    before = _revision(rb_vendor.fetch_hot_cue_slots(VENDOR_ID), "B")
    rb_vendor.save_hot_cue(VENDOR_ID, "B", 2_000, expected_revision=before)
    after = _revision(rb_vendor.fetch_hot_cue_slots(VENDOR_ID), "B")
    assert after != before, "read path must see the generation the write bumped"

    conn = sqlite3.connect(f"file:{master_db}?mode=ro", uri=True)
    try:
        generations = rb_reversal._read_slot_generations(conn, VENDOR_ID, (1, 2, 3))
    finally:
        conn.close()
    assert generations[2] == 1, "slot B (Kind 2) was bumped exactly once"
    assert generations[1] == 0 and generations[3] == 0


def test_writer_read_path_needs_no_write_capable_connection(master_db: Path) -> None:
    """The read path is satisfiable by a query_only connection.

    Injecting the factory directly, so this holds for any caller of the
    adapter and not only for the rb_vendor facade.
    """

    def open_query_only() -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{master_db}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        return conn

    slots = rb_writer.fetch_hot_cue_slots(VENDOR_ID, open_ro=open_query_only)
    assert [row["slot"] for row in slots] == list(rb_vendor.HOT_CUE_SLOTS)
