"""A warm All Tracks page does no path resolution per row (LIBM-137).

[if] a warm listing page re-walks the share root for each row [then] fail, [else stop].

Measured on a copy of a 9,713-track library, Thu 1 Oct 2026: the sqlite read
was 24 ms of a 590 ms page. The rest was every rekordbox-mapped row resolving
its analysis and artwork paths again on every request (8,820 file opens and
8,283 stats per 500 rows), then building each row's response model twice.

The fixture is a synthetic 10,000-track library the test builds itself: a real
state.db, a real master.plain.db and real ANLZ and artwork files under a share
root, so nothing in the application path is replaced. The counts come from
``tests.webui.listing_boot_probe`` (CPython's ``open`` audit event and the
sqlite trace callback), in a child process that finds the fixture the way the
packaged engine does.

A cache that only ever says "same as last time" passes every budget here, so
the controls run the other way: after each kind of change on disk the next
page must say something different, and a directory swapped for a symlink to a
look-alike outside the share root must never have its content served.

Regression one-liners:
  - if a warm 500-row page opens more than WARM_OPEN_BUDGET files then broken
  - if a deep page costs more statements, connections or opens than the first then broken
  - if a 100-row page costs different statements than a 500-row page then broken
  - if a warm page's rows differ from the cold page's rows then broken
  - if a re-analyzed .2EX keeps its old preview on the next page then broken
  - if a deleted artwork file still reads available on the next page then broken
  - if analysis files that appear later never show on the next page then broken
  - if a symlinked directory outside the share root has its preview served then broken
  - if a listing row costs more than ROW_BYTES_BUDGET bytes then broken
"""
from __future__ import annotations

import base64
import sqlite3
import struct
from pathlib import Path
from typing import Any

import pytest

from apps.shared.state import db as state_db
from tests.webui.listing_boot_probe import run_boot_probe, share_root_under

pytestmark = [pytest.mark.requirement("LIBM-137")]

TRACKS = 10_000
PAGE = 500
#: First row of the deep page: 14 full pages in, where an OFFSET scan would hurt.
DEEP = 7_000
NOW = "2026-10-01T00:00:00Z"
#: A warm page may open a handful of files that are per request (none of them
#: per row). Before LIBM-137 a 500-row page opened about 6,000 in this fixture.
WARM_OPEN_BUDGET = 40
#: Bytes of JSON per listed row in this fixture (rows with a preview strip and
#: vocal regions measure about 1,660). A ratchet: raise it only with a reason.
ROW_BYTES_BUDGET = 1_900
PREVIEW_LEVEL, REANALYZED_LEVEL, OUTSIDE_LEVEL = 5, 9, 13

#: What each row has on disk, cycled by row number.
#: full: .2EX (tri-band preview, vocals), .DAT and cover art.
#: dat_only: a .DAT preview only, the oldest fallback, and no small cover.
#: absent: paths recorded in rekordbox, nothing on disk.
SHAPES = ("full", "full", "full", "dat_only", "absent")


def _sid(i: int) -> str:
    return f"{i:040x}"


def _section(fourcc: bytes, head: bytes, payload: bytes) -> bytes:
    head_len = 12 + len(head)
    return fourcc + struct.pack(">II", head_len, head_len + len(payload)) + head + payload


def _pmai(*sections: bytes) -> bytes:
    body = b"".join(sections)
    return b"PMAI" + struct.pack(">II", 28, 28 + len(body)) + bytes(16) + body


def _two_ex(level: int) -> bytes:
    """A .2EX with a 1,200-column tri-band preview and a 30 s vocal envelope."""
    preview = _section(b"PWV6", struct.pack(">II", 3, 1200), bytes([level, level, level]) * 1200)
    envelope = bytes([3]) * 646  # 30 s at 22050/1024 frames per second
    vocals = _section(
        b"PVDI", bytes.fromhex("0000040056220001") + struct.pack(">I", len(envelope)), envelope
    )
    return _pmai(preview, vocals)


def _dat(level: int) -> bytes:
    return _pmai(_section(b"PWAV", struct.pack(">II", 400, 0), bytes([level]) * 400))


def _expected_preview(level: int) -> str:
    return base64.b64encode(bytes([level]) * 360).decode("ascii")


def _anlz_dir(share: Path, i: int) -> Path:
    return share / "PIONEER" / "USBANLZ" / f"{i % 4096:03x}" / f"{i:08x}"


def _art_dir(share: Path, i: int) -> Path:
    return share / "PIONEER" / "Artwork" / f"{i % 4096:03x}" / f"{i:08x}"


def _write_assets(share: Path, i: int) -> None:
    shape = SHAPES[i % len(SHAPES)]
    if shape == "absent":
        return
    anlz = _anlz_dir(share, i)
    anlz.mkdir(parents=True)
    (anlz / "ANLZ0000.DAT").write_bytes(_dat(PREVIEW_LEVEL))
    if shape == "full":
        (anlz / "ANLZ0000.2EX").write_bytes(_two_ex(PREVIEW_LEVEL))
        art = _art_dir(share, i)
        art.mkdir(parents=True)
        (art / "artwork.jpg").write_bytes(b"jpg")
        (art / "artwork_s.jpg").write_bytes(b"jpg-s")


def _seed(root: Path) -> tuple[Path, Path, Path]:
    """Build the library; return (data_dir, home, share_root)."""
    data_dir, home = root / "data", root / "home"
    share = share_root_under(home)
    share.mkdir(parents=True)
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
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', ?)",
            [(_sid(i), str(i + 1)) for i in range(TRACKS)],
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
            "INSERT INTO djmdContent (ID, ImagePath, AnalysisDataPath, DJPlayCount) "
            "VALUES (?, ?, ?, 0)",
            [
                (
                    str(i + 1),
                    f"/PIONEER/Artwork/{i % 4096:03x}/{i:08x}/artwork.jpg",
                    f"/PIONEER/USBANLZ/{i % 4096:03x}/{i:08x}/ANLZ0000.DAT",
                )
                for i in range(TRACKS)
            ],
        )
        master.commit()
    finally:
        master.close()
    # Only the rows the measured pages list need files; the other 8,900 rows
    # are the real "recorded in rekordbox, nothing on disk" state.
    for i in [*range(PAGE + 100), *range(DEEP, DEEP + PAGE)]:
        _write_assets(share, i)
    return data_dir, home, share


FIRST = f"/api/v1/tracks?limit={PAGE}"
SMALL = "/api/v1/tracks?limit=100"
DEEP_PAGE = f"/api/v1/tracks?limit={PAGE}&cursor={_sid(DEEP - 1)}"
HEAD = "/api/v1/tracks?limit=10"
#: Rows the liveness steps touch, all inside HEAD and all "full" except 4.
REANALYZED, ART_DELETED, SWAPPED, APPEARS = 0, 1, 2, 4


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("listing-boot")
    data_dir, home, share = _seed(root)
    outside = root / "outside" / "look-alike"
    outside.mkdir(parents=True)
    (outside / "ANLZ0000.DAT").write_bytes(_dat(OUTSIDE_LEVEL))
    (outside / "ANLZ0000.2EX").write_bytes(_two_ex(OUTSIDE_LEVEL))
    late = _anlz_dir(share, APPEARS)
    late.mkdir(parents=True)
    steps: list[dict[str, Any]] = [
        {"op": "get", "name": "cold_first", "url": FIRST},
        {"op": "get", "name": "warm_first", "url": FIRST},
        {"op": "get", "name": "small", "url": SMALL},
        {"op": "get", "name": "cold_deep", "url": DEEP_PAGE},
        {"op": "get", "name": "warm_deep", "url": DEEP_PAGE},
        {"op": "get", "name": "head_before", "url": HEAD},
        {
            "op": "write",
            "path": str(_anlz_dir(share, REANALYZED) / "ANLZ0000.2EX"),
            "hex": _two_ex(REANALYZED_LEVEL).hex(),
            "mtime_ns": 1_900_000_000_000_000_000,
        },
        {"op": "unlink", "path": str(_art_dir(share, ART_DELETED) / "artwork_s.jpg")},
        {
            "op": "write",
            "path": str(late / "ANLZ0000.DAT"),
            "hex": _dat(PREVIEW_LEVEL).hex(),
            "mtime_ns": 1_900_000_000_000_000_000,
        },
        {
            "op": "swap_dir_for_symlink",
            "path": str(_anlz_dir(share, SWAPPED)),
            "target": str(outside),
        },
        {"op": "get", "name": "head_after", "url": HEAD},
    ]
    return run_boot_probe(data_dir, home, steps, root)


def _rows(page: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = page["body"]["items"]
    return items


def test_fixture_rows_carry_real_previews_vocals_and_artwork(probe) -> None:
    """[if] the fixture stops exercising the asset path [then] fail loudly, [else stop]."""
    rows = _rows(probe["cold_first"])
    assert len(rows) == PAGE and all(row["has_rb_mapping"] for row in rows)
    full, dat_only, absent = rows[0], rows[3], rows[4]
    assert full["preview_b64"] == _expected_preview(PREVIEW_LEVEL)
    assert full["vocals"]["status"] == "rekordbox" and full["vocals"]["regions"]
    assert (full["artwork_available"], dat_only["artwork_available"]) == (True, False)
    assert dat_only["preview_b64"] == _expected_preview(PREVIEW_LEVEL)
    assert dat_only["vocals"] == {"status": "not_analyzed"}
    assert (absent["preview_b64"], absent["artwork_available"]) == (None, False)
    assert probe["cold_first"]["content_type"] == "application/json"


def test_warm_page_opens_no_file_per_row(probe) -> None:
    """[if] a warm 500-row page opens files per row [then] fail, [else stop]."""
    cold, warm = probe["cold_first"], probe["warm_first"]
    assert cold["request_opens"] > PAGE, (
        f"the cold page opened {cold['request_opens']} files: the open counter is not "
        f"attached to the request thread ({cold['opens_by_thread']})"
    )
    assert warm["request_opens"] <= WARM_OPEN_BUDGET, warm["opens_by_thread"]
    assert probe["warm_deep"]["request_opens"] <= WARM_OPEN_BUDGET, probe["warm_deep"]["opens_by_thread"]


def test_warm_page_says_what_the_cold_page_said(probe) -> None:
    """[if] serving from the row cache changes any row [then] fail, [else stop]."""
    assert _rows(probe["warm_first"]) == _rows(probe["cold_first"])
    assert _rows(probe["warm_deep"]) == _rows(probe["cold_deep"])


def test_page_cost_is_constant_in_offset_and_page_size(probe) -> None:
    """[if] a deep or a larger page issues more sqlite work [then] fail, [else stop]."""
    first, deep, small = probe["warm_first"], probe["warm_deep"], probe["small"]
    assert first["statements"] > 0, "trace saw no statements: the instrument is not attached"
    assert len(_rows(deep)) == PAGE and _rows(deep)[0]["stable_id"] == _sid(DEEP)
    for name, page in (("deep", deep), ("small", small)):
        grew = [shape for shape in page["shapes"] if shape not in first["shapes"]]
        assert (page["statements"], page["connections"]) == (
            first["statements"], first["connections"],
        ), f"{name} page: statement shapes that differ from the first page: {grew[:6]}"


def test_row_payload_stays_inside_its_byte_budget(probe) -> None:
    """[if] a listed row grows past its byte budget [then] fail, [else stop]."""
    per_row = probe["warm_first"]["bytes"] / PAGE
    assert 500 < per_row <= ROW_BYTES_BUDGET, f"{per_row:.0f} bytes per row"


def test_changes_on_disk_show_on_the_next_page(probe) -> None:
    """[if] the row cache outlives a change to the files behind it [then] fail, [else stop]."""
    before, after = _rows(probe["head_before"]), _rows(probe["head_after"])
    assert before[REANALYZED]["preview_b64"] == _expected_preview(PREVIEW_LEVEL)
    assert after[REANALYZED]["preview_b64"] == _expected_preview(REANALYZED_LEVEL)
    assert (before[ART_DELETED]["artwork_available"], before[ART_DELETED]["artwork_status"]) == (True, "ok")
    assert (after[ART_DELETED]["artwork_available"], after[ART_DELETED]["artwork_status"]) == (
        False, "file_missing",
    )
    assert before[APPEARS]["preview_b64"] is None
    assert after[APPEARS]["preview_b64"] == _expected_preview(PREVIEW_LEVEL)
    # Control: a row nothing touched still says what it said.
    assert after[5] == before[5]


def test_symlink_out_of_the_share_root_is_never_served(probe) -> None:
    """[if] a directory swapped for an outside symlink has its files served [then] fail, [else stop]."""
    before, after = _rows(probe["head_before"])[SWAPPED], _rows(probe["head_after"])[SWAPPED]
    assert before["preview_b64"] == _expected_preview(PREVIEW_LEVEL)
    assert after["preview_b64"] != _expected_preview(OUTSIDE_LEVEL)
    assert (after["preview_b64"], after["vocals"]) == (None, {"status": "not_analyzed"})
