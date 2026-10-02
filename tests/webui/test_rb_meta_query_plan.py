"""The rekordbox meta read for a page seeks its ids, not the library (LIBM-130).

[if] a page's djmdContent read walks every live row [then] fail, [else stop].

Found at 10k (Sat 26 Sep 2026, lead from the coordinator on cad91e688):
``bulk_rb_meta`` runs twice per listing request, and its djmdContent query
filtered ``c.ID IN (...) AND c.rb_local_deleted = 0``. Real rekordbox master
dbs index ``rb_local_deleted`` and carry no ``sqlite_stat1``, so sqlite chose
that index over the ID key and walked every live row for each page: 5.80 ms
against 1.51 ms per 500-id page on a 10,138-row master, identical rows. The
unary ``+`` takes the column off the index and the planner seeks the ids.

The master here is pyrekordbox's schema plus the ``rb_local_deleted`` index
DDL captured verbatim from a real master.plain.db (pyrekordbox's models omit
it, and without it the defect does not reproduce - the control proves the
fixture still can). The plan is read from the SQL the REAL function ran,
captured by the audit-hook tracer, so a query rewritten elsewhere is seen.
``bulk_rb_meta`` runs in a child process that finds the fixture through the
production ``MDT_DATA_DIR`` contract (``listing_probe``), so no adapter
setting is rebound.

Regression one-liners:
  - if bulk_rb_meta's djmdContent read plans onto the rb_local_deleted index then broken
  - if the library wheel's genre read plans onto the rb_local_deleted index then broken
  - if any app query filters ID IN (...) with a bare rb_local_deleted = 0 then broken
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from pyrekordbox.db6 import tables
from sqlalchemy import create_engine

from apps.library_wheel.query import _load_genre_and_play_count
from apps.shared.state.schema import apply_migrations
from tests.webui.listing_probe import run_probe
from tests.webui.sql_trace import trace_sqlite

pytestmark = [pytest.mark.requirement("LIBM-130")]

REPO_ROOT = Path(__file__).resolve().parents[2]
ROWS = 300
#: Captured from a real rekordbox master.plain.db (sqlite_master), verbatim.
REAL_REKORDBOX_INDEXES = (
    "CREATE INDEX `djmd_content_rb_local_deleted` ON `djmdContent` (`rb_local_deleted`)",
    "CREATE INDEX `djmd_genre_rb_local_deleted` ON `djmdGenre` (`rb_local_deleted`)",
)
PK_SEEK = "sqlite_autoindex_djmdContent_1 (ID=?)"
DELETED_INDEX = "djmd_content_rb_local_deleted"
#: ``ID IN (...)`` then a bare (index-eligible) rb_local_deleted equality.
BARE_DELETED_AFTER_ID_IN = re.compile(
    r"\bID IN \(\{\w+\}\)\s+AND\s+(?!\+)(?:\w+\.)?rb_local_deleted\s*=\s*0"
)


def _vendor_id(i: int) -> str:
    return f"{9_000_000 + i}"


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """Insert one row, filling pyrekordbox's other NOT NULL columns by type."""
    row = dict(values)
    for _cid, name, decl, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
        if notnull and default is None and name not in row:
            row[name] = 0 if "INT" in decl.upper() else ""
    marks = ",".join("?" * len(row))
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({marks})", tuple(row.values()))


def _make_master(path: Path) -> None:
    tables.Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
    conn = sqlite3.connect(path)
    try:
        for ddl in REAL_REKORDBOX_INDEXES:
            conn.execute(ddl)
        _insert(conn, "djmdGenre", {"ID": "g1", "Name": "Techno", "rb_local_deleted": 0})
        for i in range(ROWS):
            _insert(conn, "djmdContent", {
                "ID": _vendor_id(i), "FolderPath": f"/music/{i}.flac", "GenreID": "g1",
                "DJPlayCount": 3, "rb_local_deleted": 0,
            })
        conn.commit()
    finally:
        conn.close()


def _make_state(path: Path) -> list[str]:
    stable_ids = [f"sid-{i:04d}" for i in range(ROWS)]
    conn = sqlite3.connect(path)
    try:
        apply_migrations(conn)
        conn.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, created_at, "
            "updated_at) VALUES (?, 'inferred', ?, '[]', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')",
            [(sid, sid) for sid in stable_ids],
        )
        conn.executemany(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', ?)",
            [(sid, _vendor_id(i)) for i, sid in enumerate(stable_ids)],
        )
        conn.commit()
    finally:
        conn.close()
    return stable_ids


@pytest.fixture
def master_db(tmp_path: Path) -> tuple[Path, list[str]]:
    """``<tmp>/data`` in the MDT_DATA_DIR layout: master.plain.db and state/state.db."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    _make_master(data_dir / "master.plain.db")
    return data_dir, _make_state(data_dir / "state" / "state.db")


def _content_plan(master: Path, sql: str) -> str:
    conn = sqlite3.connect(master)
    try:
        return " | ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}"))
    finally:
        conn.close()


def _djmd_content_statement(statements: list[tuple[str, str]]) -> str:
    matches = [sql for _thread, sql in statements if "FROM djmdContent c" in sql]
    assert len(matches) == 1, f"expected one djmdContent read, traced {len(matches)}"
    return matches[0]


def test_bulk_rb_meta_seeks_the_page_ids(master_db: tuple[Path, list[str]], tmp_path: Path) -> None:
    """[if] bulk_rb_meta plans onto the rb_local_deleted index [then] fail, [else stop]."""
    data_dir, stable_ids = master_db
    master = data_dir / "master.plain.db"
    page = stable_ids[:50]
    probe = run_probe(data_dir, {"mode": "rb_meta", "ids": page}, tmp_path)
    assert probe["meta_ids"] == page, "every mapped id must still come back"
    statements = [("probe", sql) for sql in probe["statements"]]
    sql = _djmd_content_statement(statements)
    plan = _content_plan(master, sql)
    assert PK_SEEK in plan and DELETED_INDEX not in plan, plan
    # Control: the same statement without the unary + still plans onto the
    # rb_local_deleted index on this fixture, so the assertion above can fail.
    bare = sql.replace("+c.rb_local_deleted", "c.rb_local_deleted")
    assert bare != sql, "the traced statement no longer carries the unary +"
    assert DELETED_INDEX in _content_plan(master, bare), "fixture no longer reproduces the defect"


def test_library_wheel_genre_read_seeks_the_page_ids(master_db: tuple[Path, list[str]]) -> None:
    """[if] the wheel's genre read plans onto rb_local_deleted [then] fail, [else stop]."""
    data_dir, _stable_ids = master_db
    master = data_dir / "master.plain.db"
    vendor_ids = [_vendor_id(i) for i in range(50)]
    with trace_sqlite() as trace:
        conn = sqlite3.connect(master)
        conn.row_factory = sqlite3.Row
        try:
            genres = _load_genre_and_play_count(conn, vendor_ids)
        finally:
            conn.close()
    assert genres == dict.fromkeys(vendor_ids, ("Techno", 3))
    plan = _content_plan(master, _djmd_content_statement(trace.statements))
    assert PK_SEEK in plan and DELETED_INDEX not in plan, plan


def test_no_app_query_filters_id_in_with_a_bare_rb_local_deleted() -> None:
    """[if] an app query pairs ID IN with bare rb_local_deleted [then] fail, [else stop]."""
    sources = sorted((REPO_ROOT / "apps").rglob("*.py"))
    assert len(sources) > 100, f"scanned only {len(sources)} files: wrong root?"
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{text.count(chr(10), 0, match.start()) + 1}"
        for path in sources
        for text in [path.read_text(encoding="utf-8")]
        for match in BARE_DELETED_AFTER_ID_IN.finditer(text)
    ]
    assert not offenders, f"ID IN (...) with an index-eligible rb_local_deleted: {offenders}"
    # Control: the pattern fires on the pre-fix shape and spares the fixed one.
    pre_fix = "WHERE c.ID IN ({placeholders}) AND c.rb_local_deleted = 0"
    assert BARE_DELETED_AFTER_ID_IN.search(pre_fix)
    assert not BARE_DELETED_AFTER_ID_IN.search("WHERE ID IN ({marks}) AND +rb_local_deleted = 0")
