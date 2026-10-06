"""GET /tracks/index: the whole library listing in one response (LIBM-171).

[if] the index differs from a full /tracks walk or scales per row [then] fail, [else stop].

The browser holds the library index once and resolves All Tracks from it, the
way rekordbox, Serato and Traktor hold their library in memory. Measured on
silver, Tue 6 Oct 2026: walking 11,165 rows as 23 pages of 500 took 35 s on the
installed engine and 60 s on the preview, while the list re-walked from zero
on every switch back to All Tracks.

The perf half runs on a real rekordbox-mapped 2,300-row state.db in a child
engine (``tests.webui.listing_boot_probe``), the size of the maintainer's present library.

Regression one-liners:
  - if the index rows are not the /tracks walk rows minus provenance then broken
  - if the index costs more sql statements per 1000 rows than a /tracks page then broken
  - if a warm index of 2,300 rows takes longer than INDEX_WARM_BUDGET_MS then broken
  - if a matching If-None-Match does not answer 304 then broken
  - if a gzip-accepting client is not sent gzip then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.shared.state import db as state_db
from tests.webui.listing_boot_probe import run_boot_probe, share_root_under

pytestmark = [pytest.mark.requirement("LIBM-171")]

TRACKS = 2_300
NOW = "2026-10-06T00:00:00Z"
#: Warm index of TRACKS mapped rows, in a child engine. Measured on demon-llama,
#: Tue 6 Oct 2026: 348 ms cold, 359 ms warm, 3.1 MB. A ratchet: lower it, never raise it.
INDEX_WARM_BUDGET_MS = 1_500


def _sid(i: int) -> str:
    return f"{i:040x}"


def _seed(root: Path) -> tuple[Path, Path]:
    data_dir, home = root / "data", root / "home"
    share_root_under(home).mkdir(parents=True)
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    conn = state_db.open_rw(state_path)
    try:
        conn.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "created_at, updated_at) VALUES (?, 'inferred', ?, 180000, ?, ?)",
            [(_sid(i), f"track {i}", NOW, NOW) for i in range(TRACKS)],
        )
        conn.executemany(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)",
            [(_sid(i), str(i + 1)) for i in range(TRACKS)],
        )
        conn.executemany(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, modified_at) "
            "VALUES (?, 'bpm', ?, 'rekordbox', ?)",
            [(_sid(i), str(120 + i % 10), NOW) for i in range(TRACKS)],
        )
        conn.commit()
    finally:
        conn.close()
    master = sqlite3.connect(data_dir / "master.plain.db")
    try:
        master.execute(
            "CREATE TABLE djmdContent (ID TEXT PRIMARY KEY, FolderPath TEXT, ImagePath TEXT, "
            "AnalysisDataPath TEXT, Commnt TEXT, GenreID TEXT, DJPlayCount INTEGER, "
            "rb_local_deleted INTEGER NOT NULL DEFAULT 0)"
        )
        master.execute(
            "CREATE TABLE djmdGenre (ID TEXT PRIMARY KEY, Name TEXT, "
            "rb_local_deleted INTEGER NOT NULL DEFAULT 0)"
        )
        master.executemany(
            "INSERT INTO djmdContent (ID, ImagePath, AnalysisDataPath, DJPlayCount) VALUES (?, ?, ?, 0)",
            [
                (str(i + 1), f"/PIONEER/Artwork/{i:08x}/artwork.jpg",
                 f"/PIONEER/USBANLZ/{i:08x}/ANLZ0000.DAT")
                for i in range(TRACKS)
            ],
        )
        master.commit()
    finally:
        master.close()
    return data_dir, home


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("track-index")
    data_dir, home = _seed(root)
    steps: list[dict[str, Any]] = [
        {"op": "get", "name": "index_cold", "url": "/api/v1/tracks/index"},
        {"op": "get", "name": "index_warm", "url": "/api/v1/tracks/index"},
        {"op": "get", "name": "page_1", "url": "/api/v1/tracks?limit=1000"},
        {"op": "get", "name": "page_2", "url": f"/api/v1/tracks?limit=1000&cursor={_sid(999)}"},
        {"op": "get", "name": "page_3", "url": f"/api/v1/tracks?limit=1000&cursor={_sid(1999)}"},
    ]
    return run_boot_probe(data_dir, home, steps, root)


def test_index_is_the_full_walk_minus_provenance(probe) -> None:
    """[if] the index rows differ from the /tracks walk minus provenance [then] fail, [else stop]."""
    walk = [row for name in ("page_1", "page_2", "page_3") for row in probe[name]["body"]["items"]]
    index = probe["index_warm"]["body"]
    assert len(walk) == TRACKS
    assert isinstance(index["revision"], str) and index["revision"] != ""
    assert all("provenance" not in row for row in index["items"])
    assert index["items"] == [{k: v for k, v in row.items() if k != "provenance"} for row in walk]


def test_index_sql_cost_is_per_read_page_not_per_row(probe) -> None:
    """[if] the index runs more sql than its three 1000-row reads plus a revision [then] fail, [else stop]."""
    page_statements = max(probe[name]["statements"] for name in ("page_1", "page_2", "page_3"))
    assert probe["index_warm"]["statements"] <= 3 * page_statements + 5, probe["index_warm"]["shapes"]


def test_warm_index_stays_inside_its_time_budget(probe) -> None:
    """[if] a warm 2,300-row index takes longer than INDEX_WARM_BUDGET_MS [then] fail, [else stop]."""
    print(f"index cold {probe['index_cold']['wall_ms']:.0f} ms, warm {probe['index_warm']['wall_ms']:.0f} ms, "
          f"{probe['index_warm']['bytes']} bytes")
    assert probe["index_warm"]["wall_ms"] < INDEX_WARM_BUDGET_MS


def test_matching_etag_answers_304_and_gzip_is_honoured(client) -> None:
    """[if] If-None-Match is ignored or a gzip client gets plain bytes [then] fail, [else stop]."""
    plain = client.get("/api/v1/tracks/index", headers={"accept-encoding": "identity"})
    assert plain.status_code == 200, plain.text
    assert "content-encoding" not in plain.headers
    etag = plain.headers["etag"]
    assert len(plain.json()["items"]) == 5
    again = client.get("/api/v1/tracks/index", headers={"if-none-match": etag})
    assert again.status_code == 304
    zipped = client.get("/api/v1/tracks/index", headers={"accept-encoding": "gzip"})
    assert zipped.headers["content-encoding"] == "gzip"
    assert zipped.json() == plain.json()
