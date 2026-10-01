"""One bad row never fails a listing page, through the HTTP route (LIBM-137, round 4).

[if] a listing page with a malformed row, or after a share root change, answers wrongly over HTTP [then] fail, [else stop].

The unit tests in ``test_row_assets_degrade.py`` call the row reader. These
run the real engine in a child process (``listing_boot_probe``, which exits
non-zero on any answer but 200) over a small rekordbox-mapped library, and
change the real filesystem between requests.

Regression one-liners:
  - if a zero-byte, garbage, truncated or bad-header analysis file fails the page then broken
  - if a malformed row's neighbors lose their preview, vocals or cover then broken
  - if a lyrics-cache path that is a file fails the page then broken
  - if a share root remounted as a new real directory keeps answering without previews then broken
  - if a re-pointed symlinked share root is served before the re-anchor call then broken
  - if the re-anchor call does not bring a re-pointed symlinked share root back then broken
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.shared import fd_anchored_walk
from apps.shared.state import db as state_db
from tests.webui.listing_boot_probe import run_boot_probe, share_root_under
from tests.webui.test_listing_boot_perf import NOW, _dat, _expected_preview, _sid, _two_ex
from tests.webui.test_row_assets_degrade import MALFORMED, bad_pvdi_header

pytestmark = [
    pytest.mark.requirement("LIBM-137"),
    pytest.mark.skipif(
        not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED, reason="needs the fd-anchored walk"
    ),
]

LEVEL, OTHER_LEVEL = 5, 9
PAGE = "/api/v1/tracks?limit=50"
REANCHOR = "/api/v1/library/share-root/reanchor"
#: Row number -> what its .2EX holds. Rows not named here are healthy.
BAD_2EX: dict[int, bytes] = {
    1: MALFORMED["zero-byte"],
    2: MALFORMED["garbage"],
    3: bad_pvdi_header(),
    4: MALFORMED["truncated-header"],
    5: MALFORMED["truncated-section"],
}
EVERYTHING_BAD, UNCACHED_BAD, NUL_PATH, HEALTHY = 6, 7, 8, (0, 9, 10, 11)
ROWS = 12


def _vendor_paths(i: int) -> tuple[str, str]:
    anlz, art = f"/PIONEER/USBANLZ/p/{i:08x}/ANLZ0000.DAT", f"/PIONEER/Artwork/p/{i:08x}/artwork.jpg"
    if i == UNCACHED_BAD:  # an empty component: the row takes the uncached path
        return anlz.replace("/USBANLZ/", "/USBANLZ//"), art
    if i == NUL_PATH:
        return f"/PIONEER/USBANLZ/p/a\x00b/{i:08x}/ANLZ0000.DAT", art
    return anlz, art


def _build_share(share: Path, level: int) -> None:
    for i in range(ROWS):
        anlz = share / "PIONEER" / "USBANLZ" / "p" / f"{i:08x}"
        anlz.mkdir(parents=True)
        (anlz / "ANLZ0000.DAT").write_bytes(_dat(level))
        (anlz / "ANLZ0000.2EX").write_bytes(_two_ex(level))
        art = share / "PIONEER" / "Artwork" / "p" / f"{i:08x}"
        art.mkdir(parents=True)
        (art / "artwork.jpg").write_bytes(b"jpg")
        (art / "artwork_s.jpg").write_bytes(b"jpg-s")


def _seed(root: Path) -> tuple[Path, Path]:
    """State and rekordbox databases for ROWS mapped tracks; return (data_dir, home)."""
    data_dir, home = root / "data", root / "home"
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    conn = state_db.open_rw(state_path)
    try:
        conn.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "created_at, updated_at) VALUES (?, 'inferred', ?, 180000, ?, ?)",
            [(_sid(i), f"track {i}", NOW, NOW) for i in range(ROWS)],
        )
        conn.executemany(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)",
            [(_sid(i), str(i + 1)) for i in range(ROWS)],
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
            [(str(i + 1), _vendor_paths(i)[1], _vendor_paths(i)[0]) for i in range(ROWS)],
        )
        master.commit()
    finally:
        master.close()
    return data_dir, home


def _by_row(page: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows = {int(row["stable_id"], 16): row for row in page["body"]["items"]}
    assert sorted(rows) == list(range(ROWS)), "control: the page lists every fixture row"
    return rows


def _write(path: Path, data: bytes) -> dict[str, Any]:
    return {"op": "write", "path": str(path), "hex": data.hex(), "mtime_ns": 1_900_000_000_000_000_000}


# ----- P1-a and the lyrics flag ----------------------------------------------


@pytest.fixture(scope="module")
def degraded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("listing-degraded")
    data_dir, home = _seed(root)
    share = share_root_under(home)
    _build_share(share, LEVEL)
    anlz = share / "PIONEER" / "USBANLZ" / "p"
    for i, payload in BAD_2EX.items():
        (anlz / f"{i:08x}" / "ANLZ0000.2EX").write_bytes(payload)
    for i in (EVERYTHING_BAD, UNCACHED_BAD):
        (anlz / f"{i:08x}" / "ANLZ0000.2EX").write_bytes(MALFORMED["garbage"])
    (anlz / f"{EVERYTHING_BAD:08x}" / "ANLZ0000.DAT").write_bytes(MALFORMED["zero-byte"])
    lyrics_cache_path = data_dir / "state" / "lyrics-cache"
    steps: list[dict[str, Any]] = [
        {"op": "get", "name": "cold", "url": PAGE},
        {"op": "get", "name": "warm", "url": PAGE},
        _write(lyrics_cache_path, b"a file where the lyrics-cache directory belongs"),
        {"op": "get", "name": "lyrics_cache_is_a_file", "url": PAGE},
        {"op": "get", "name": "one_track", "url": f"/api/v1/tracks/{_sid(0)}"},
    ]
    return run_boot_probe(data_dir, home, steps, root)


@pytest.mark.parametrize("request_name", ["cold", "warm"])
def test_a_page_with_malformed_analysis_files_answers_with_those_rows_degraded(
    degraded: dict[str, Any], request_name: str
) -> None:
    rows = _by_row(degraded[request_name])
    for i in (1, 2, 4, 5, UNCACHED_BAD):
        assert rows[i]["preview_b64"] == _expected_preview(LEVEL), f"row {i}: the .DAT still answers"
        assert rows[i]["vocals"] == {"status": "not_analyzed"}, f"row {i}"
        assert rows[i]["artwork_status"] == "ok", f"row {i}"
    assert rows[3]["preview_b64"] == _expected_preview(LEVEL), "a bad PVDI header keeps the preview"
    assert rows[3]["vocals"] == {"status": "not_analyzed"}
    for i in (EVERYTHING_BAD, NUL_PATH):
        assert rows[i]["preview_b64"] is None and rows[i]["preview_max"] is None, f"row {i}"
        assert rows[i]["vocals"] == {"status": "not_analyzed"}, f"row {i}"


@pytest.mark.parametrize("request_name", ["cold", "warm", "lyrics_cache_is_a_file"])
def test_the_neighbors_of_a_malformed_row_are_intact(
    degraded: dict[str, Any], request_name: str
) -> None:
    rows = _by_row(degraded[request_name])
    for i in HEALTHY:
        assert rows[i]["preview_b64"] == _expected_preview(LEVEL), f"row {i}"
        assert rows[i]["vocals"]["status"] == "rekordbox", f"row {i}"
        assert (rows[i]["artwork_available"], rows[i]["artwork_status"]) == (True, "ok"), f"row {i}"


def test_a_warm_page_says_what_the_cold_page_said(degraded: dict[str, Any]) -> None:
    assert _by_row(degraded["warm"]) == _by_row(degraded["cold"])


def test_a_lyrics_cache_path_that_is_a_file_is_no_lyrics_not_a_failed_page(
    degraded: dict[str, Any]
) -> None:
    rows = _by_row(degraded["lyrics_cache_is_a_file"])
    assert {row["lyrics_available"] for row in rows.values()} == {False}
    assert degraded["one_track"]["body"]["lyrics_available"] is False


# ----- P2-b: a remounted root, and a symlinked one ----------------------------


@pytest.mark.requirement("LIBM-139")
def test_a_share_root_remounted_as_a_new_directory_is_read_on_the_next_page(
    tmp_path: Path,
) -> None:
    data_dir, home = _seed(tmp_path)
    share = share_root_under(home)
    _build_share(share, LEVEL)
    remounted = tmp_path / "the-volume-after-remount"
    _build_share(remounted, OTHER_LEVEL)
    steps: list[dict[str, Any]] = [
        {"op": "get", "name": "before", "url": PAGE},
        {"op": "rename", "path": str(share), "to": str(tmp_path / "unplugged")},
        {"op": "rename", "path": str(remounted), "to": str(share)},
        {"op": "get", "name": "after", "url": PAGE},
    ]
    result = run_boot_probe(data_dir, home, steps, tmp_path)
    assert _by_row(result["before"])[0]["preview_b64"] == _expected_preview(LEVEL)
    after = _by_row(result["after"])
    for i in HEALTHY:
        assert after[i]["preview_b64"] == _expected_preview(OTHER_LEVEL), f"row {i}"
        assert after[i]["artwork_status"] == "ok", f"row {i}"


@pytest.mark.requirement("LIBM-139")
def test_a_re_pointed_symlinked_share_root_waits_for_the_reanchor_call(tmp_path: Path) -> None:
    data_dir, home = _seed(tmp_path)
    share = share_root_under(home)
    first, second = tmp_path / "library-a", tmp_path / "library-b"
    _build_share(first, LEVEL)
    _build_share(second, OTHER_LEVEL)
    share.parent.mkdir(parents=True)
    os.symlink(first, share)
    steps: list[dict[str, Any]] = [
        {"op": "get", "name": "before", "url": PAGE},
        {"op": "symlink", "path": str(share), "target": str(second)},
        {"op": "get", "name": "refused", "url": PAGE},
        {"op": "get", "name": "still_refused", "url": PAGE},
        {"op": "post", "name": "reanchor", "url": REANCHOR},
        {"op": "get", "name": "after", "url": PAGE},
    ]
    result = run_boot_probe(data_dir, home, steps, tmp_path)
    assert _by_row(result["before"])[0]["preview_b64"] == _expected_preview(LEVEL)
    for name in ("refused", "still_refused"):
        for i, row in _by_row(result[name]).items():
            assert row["preview_b64"] is None, f"{name} row {i}: a re-pointed root is not served"
            assert row["artwork_available"] is False, f"{name} row {i}"
    assert result["reanchor"] == {"status": 200, "body": {"exists": True}}
    after = _by_row(result["after"])
    for i in HEALTHY:
        assert after[i]["preview_b64"] == _expected_preview(OTHER_LEVEL), f"row {i}"
