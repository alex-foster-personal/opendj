"""A playlist tracks page reads only its window, in today's order (LIBM-133).

[if] ``GET /playlists/{id}/tracks`` reads or sorts every member to serve one page [then] fail, [else stop].

Found by the LIBM-120 re-measure on main ``2ecfde5d7`` (Wed 30 Sep 2026, agentbox):
the first 30-row page of a 10,042-member playlist took 455 ms at p95 against
24 ms for a 70-member control, because the route called ``get_playlist`` and
sorted the whole membership for every page.

Instruments, all on real sqlite files, none a mock:

* the full ordered read ``SqliteBackend.get_playlist`` (unchanged) is the
  oracle: every window must equal its slice, byte for byte;
* sqlite VM instructions (``set_progress_handler(cb, 1)``), which count C-level
  scans and temp b-tree sorts that no Python counter sees;
* ``EXPLAIN QUERY PLAN`` of the exact SQL the window runs.

Regression one-liners:
  - if any window differs from the same slice of the full ordered read then broken
  - if a NULL order_key row does not sort by its zero-padded position then broken
  - if the tracks route still reads the whole playlist then broken
  - if the first window runs more sqlite steps at 4,000 members than at 40 then broken
  - if a page hands more rows to Python than header, count and its window then broken
  - if the window query sorts in a temp b-tree or scans the table then broken
  - if a writer's commit mid-read makes total disagree with the rows then broken
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.etag import compute_etag
from apps.webui.server.playlist_page import LIVE_COUNT_SQL, WINDOW_SQL, read_playlist_page
from apps.webui.server.sqlite_backend import SqliteBackend

from .sql_trace import RowCounter

pytestmark = pytest.mark.requirement("LIBM-133")

NOW = "2026-09-30T00:00:00Z"
SMALL, LARGE = 40, 4000
MIXED = "pl-mixed"
OTHER = "pl-other"


def _sid(i: int) -> str:
    return f"{i:040x}"


def _seed(path: Path, n_tracks: int) -> sqlite3.Connection:
    conn = state_db.open_rw(path)
    conn.executemany(
        "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
        "created_at, updated_at) VALUES (?, 'inferred', 1000, NULL, ?, ?)",
        [(_sid(i), NOW, NOW) for i in range(n_tracks)],
    )
    conn.commit()
    return conn


def _add_playlist(conn: sqlite3.Connection, playlist_id: str) -> None:
    conn.execute(
        "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
        "VALUES (?, ?, 'webui', ?, ?, ?)",
        (playlist_id, playlist_id, playlist_id, NOW, NOW),
    )


def _insert_members(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO playlist_memberships (playlist_id, stable_id, position, order_key, "
        "item_id, deleted_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _mixed_rows() -> list[tuple]:
    """NULL and non-NULL keys interleaved, legacy '' and overlong keys, tombstones.

    Inserted shuffled so rowid order says nothing about the sort order.
    """
    keys: list[str | None] = [
        None, None, "", "00000003", "00000003", "000000031", "00000010", "0000001",
        "V", "VV", "a", "Z", "0", None, "00000002", "zzzzzzzzzzzzzzzzzzzzzzzz",
    ]
    rows = []
    for position in range(120):
        key = keys[position % len(keys)]
        deleted = NOW if position % 11 == 5 else None
        rows.append((MIXED, _sid(position % 60), position, key, f"item-{position:04d}", deleted, NOW))
    rows += [(OTHER, _sid(i), i, None, f"other-{i}", None, NOW) for i in range(7)]
    random.Random(133).shuffle(rows)
    return rows


@pytest.fixture
def mixed_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = _seed(path, 60)
    _add_playlist(conn, MIXED)
    _add_playlist(conn, OTHER)
    _insert_members(conn, _mixed_rows())
    conn.close()
    return path


# ---------------------------------------------------------------------------
# equivalence


@pytest.mark.parametrize("with_index", [True, False], ids=["index", "no-index"])
def test_every_window_equals_the_full_read_slice(mixed_db: Path, with_index: bool) -> None:
    """Without the index sqlite sorts instead, and ties must still break on position."""
    if not with_index:
        conn = sqlite3.connect(str(mixed_db))
        conn.execute("DROP INDEX idx_playlist_memberships_live_order")
        conn.commit()
        conn.close()
    backend = SqliteBackend(mixed_db)
    full = backend.get_playlist(MIXED)
    assert len(full.items) == 120 - len([p for p in range(120) if p % 11 == 5])
    for limit in (1, 7, 30, 500):
        for offset in range(len(full.items) + 2):
            page = backend.get_playlist_page(MIXED, limit=limit, offset=offset)
            assert page.total == len(full.items)
            assert page.playlist.items == full.items[offset : offset + limit], (limit, offset)
            assert page.playlist.item_ids == full.item_ids[offset : offset + limit], (limit, offset)
            assert page.playlist.updated_at == full.updated_at


def test_null_order_key_sorts_by_padded_position(mixed_db: Path) -> None:
    """The oracle itself is today's rule, so a change to both sides cannot hide."""
    conn = sqlite3.connect(str(mixed_db))
    rows = conn.execute(
        "SELECT item_id, order_key, position FROM playlist_memberships "
        "WHERE playlist_id = ? AND deleted_at IS NULL",
        (MIXED,),
    ).fetchall()
    conn.close()
    expected = [r[0] for r in sorted(rows, key=lambda r: (r[1] if r[1] is not None else f"{r[2]:08d}", r[2]))]
    page = SqliteBackend(mixed_db).get_playlist_page(MIXED, limit=500, offset=0)
    assert page.playlist.item_ids == expected


# ---------------------------------------------------------------------------
# route


@pytest.fixture
def client(mixed_db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from .conftest import _stub_rb_vendor

    _stub_rb_vendor(monkeypatch)

    def _full_read_forbidden(self: SqliteBackend, playlist_id: str) -> None:
        raise AssertionError("the tracks page must not read the whole playlist")

    oracle = SqliteBackend(mixed_db).get_playlist(MIXED)
    monkeypatch.setattr(SqliteBackend, "get_playlist", _full_read_forbidden)
    app = create_app(
        backend=SqliteBackend(mixed_db), bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    app.state.page_oracle = oracle
    with TestClient(app) as c:
        yield c


def test_tracks_route_serves_the_window_without_a_full_read(client: TestClient) -> None:
    oracle = client.app.state.page_oracle  # type: ignore[attr-defined]
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        r = client.get(f"/api/v1/playlists/{MIXED}/tracks", params={"limit": 30, "offset": offset})
        assert r.status_code == 200, r.text
        assert r.headers["etag"] == compute_etag(MIXED, oracle.updated_at)
        body = r.json()
        assert body["total"] == len(oracle.items)
        seen += [t["item_id"] for t in body["tracks"]]
        offset = body["next_offset"]
    assert seen == oracle.item_ids


def test_tracks_route_past_the_end_is_empty(client: TestClient) -> None:
    oracle = client.app.state.page_oracle  # type: ignore[attr-defined]
    total = len(oracle.items)
    r = client.get(f"/api/v1/playlists/{MIXED}/tracks", params={"limit": 30, "offset": total})
    assert r.status_code == 200, r.text
    assert r.json() == {"tracks": [], "total": total, "next_offset": None}
    r = client.get("/api/v1/playlists/no-such-playlist/tracks", params={"limit": 30})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# growth


def _sized_db(tmp_path: Path, size: int) -> sqlite3.Connection:
    conn = _seed(tmp_path / f"state-{size}.db", size)
    _add_playlist(conn, MIXED)
    rows = [(MIXED, _sid(i), i, f"{i:08d}x" if i % 3 else None, f"i-{i}", None, NOW) for i in range(size)]
    _insert_members(conn, rows)
    conn.row_factory = sqlite3.Row
    return conn


def _vm_steps(conn: sqlite3.Connection, action: Callable[[], object]) -> int:
    steps = [0]

    def _tick() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(_tick, 1)
    try:
        action()
    finally:
        conn.set_progress_handler(None, 1)
    return steps[0]


def test_first_window_does_not_grow_with_the_playlist(tmp_path: Path) -> None:
    steps = {}
    for size in (SMALL, LARGE):
        conn = _sized_db(tmp_path, size)
        rows = conn.execute(WINDOW_SQL, (MIXED, 30, 0)).fetchall()
        assert len(rows) == 30
        steps[size] = _vm_steps(conn, lambda c=conn: c.execute(WINDOW_SQL, (MIXED, 30, 0)).fetchall())
        conn.close()
    assert steps[LARGE] <= steps[SMALL] * 1.2, steps


def test_page_read_materializes_only_its_window(tmp_path: Path) -> None:
    """Header row, count row and ``limit`` member rows, at any playlist size."""
    conn = _sized_db(tmp_path, LARGE)
    counter = RowCounter()
    conn.row_factory = counter
    page = read_playlist_page(conn, MIXED, limit=30, offset=LARGE - 30, has_memberships=True)
    conn.close()
    assert page.total == LARGE
    assert len(page.playlist.items) == 30
    assert counter.rows == 1 + 1 + 30, counter.rows


@pytest.mark.parametrize("sql", [WINDOW_SQL, LIVE_COUNT_SQL])
def test_page_queries_use_the_live_order_index(tmp_path: Path, sql: str) -> None:
    conn = _sized_db(tmp_path, SMALL)
    params = (MIXED, 30, 0) if "LIMIT" in sql else (MIXED,)
    plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    conn.close()
    assert "TEMP B-TREE" not in plan, plan
    assert "USING INDEX idx_playlist_memberships_live" in plan or "USING COVERING INDEX" in plan, plan


# ---------------------------------------------------------------------------
# consistency


def test_a_commit_mid_read_does_not_split_total_from_rows(mixed_db: Path) -> None:
    writer = state_db.open_rw(mixed_db)
    reader = state_db.open_ro(mixed_db)
    reader.row_factory = sqlite3.Row
    before = SqliteBackend(mixed_db).get_playlist(MIXED)

    def _commit_before_window(statement: str) -> None:
        if statement.startswith("SELECT stable_id, item_id"):
            writer.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position, order_key, "
                "item_id, updated_at) VALUES (?, ?, 1000, '', 'late-item', ?)",
                (MIXED, _sid(0), NOW),
            )
            writer.commit()

    reader.set_trace_callback(_commit_before_window)
    page = read_playlist_page(reader, MIXED, limit=500, offset=0, has_memberships=True)
    reader.set_trace_callback(None)
    reader.close()
    writer.close()
    assert page.total == len(before.items)
    assert page.playlist.item_ids == before.item_ids
    after = SqliteBackend(mixed_db).get_playlist(MIXED)
    assert "late-item" in after.item_ids  # the write did land, just not inside the read
