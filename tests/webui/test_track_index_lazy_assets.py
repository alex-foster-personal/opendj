"""The library index skips per-row disk reads; rows in view get them from /library/row-assets (LIBM-172).

[if] the index reads a row's assets off disk, or row-assets disagrees with /tracks [then] fail, [else stop].

Measured on the Air, Tue 6 Oct 2026, on a 9,713-row copy: a cold index spent 7.8 s
of 8.5 s reading each mapped row's preview strip, vocal regions and cover off the
rekordbox share. The index now sends none of those; the browser asks for the rows
in view. Fixture: tests.webui.test_track_index's 2,300 mapped rows, with real ANLZ
and artwork files and a lyrics-cache entry written for the first ASSET_ROWS rows.

Regression one-liners:
  - if a cold index opens a file per asset-backed row, or per lyrics-cache entry, then broken
  - if a cold /tracks page over the same rows does NOT open them then the fixture is broken
  - if row-assets answers different strip, vocals, cover or stems than /tracks then broken
  - if row-assets answers for an id that is not a library track then broken
  - if row-assets accepts an empty id batch, or answers a repeated id twice, then broken
  - if one unavailable row in a batch drops or blanks a neighbor that has assets then broken
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.webui.listing_boot_probe import run_boot_probe, share_root_under
from tests.webui.test_listing_boot_perf import _write_assets
from tests.webui.test_track_index import _seed, _sid

pytestmark = [pytest.mark.requirement("LIBM-172")]

ASSET_ROWS = 300
ASKED = [_sid(i) for i in range(12)]
BARE = _sid(ASSET_ROWS + 1)  # a library track with no strip, vocals or cover on disk
REPEATED = [_sid(0), _sid(1), _sid(0), _sid(1), _sid(0)]
MIXED = [_sid(0), BARE, "not-a-track", _sid(1)]
ASSET_FIELDS = ("preview_b64", "preview_max", "vocals", "artwork_available", "artwork_status", "stems")


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("track-index-lazy")
    data_dir, home = _seed(root)
    share = share_root_under(home)
    for i in range(ASSET_ROWS):
        _write_assets(share, i)
        # A lyrics-cache entry per row: the index must not open it (lyrics_available).
        lyric = data_dir / "state" / "lyrics-cache" / f"{_sid(i)}.json"
        lyric.parent.mkdir(parents=True, exist_ok=True)
        lyric.write_text("{}", encoding="utf-8")
    steps: list[dict[str, Any]] = [
        {"op": "get", "name": "index_cold", "url": "/api/v1/tracks/index"},
        {"op": "post", "name": "assets", "url": "/api/v1/library/row-assets",
         "json": {"ids": [*ASKED, "not-a-track"]}},
        {"op": "get", "name": "page_cold", "url": f"/api/v1/tracks?limit={ASSET_ROWS}"},
        {"op": "post", "name": "empty", "url": "/api/v1/library/row-assets", "json": {"ids": []}},
        {"op": "post", "name": "repeated", "url": "/api/v1/library/row-assets", "json": {"ids": REPEATED}},
        {"op": "post", "name": "mixed", "url": "/api/v1/library/row-assets", "json": {"ids": MIXED}},
    ]
    return run_boot_probe(data_dir, home, steps, root)


def test_cold_index_reads_no_row_assets_off_disk(probe) -> None:
    """[if] a cold index opens files per asset-backed row [then] fail, [else stop]."""
    index_opens = probe["index_cold"]["request_opens"]
    page_opens = probe["page_cold"]["request_opens"]
    print(f"cold index opens {index_opens}, cold {ASSET_ROWS}-row page opens {page_opens}")
    assert page_opens >= ASSET_ROWS, "control: the fixture's rows must really have assets to read"
    assert index_opens < 40


def test_row_assets_answer_what_the_listing_row_carries(probe) -> None:
    """[if] row-assets disagrees with /tracks for a row in view [then] fail, [else stop]."""
    rows = {row["stable_id"]: row for row in probe["page_cold"]["body"]["items"]}
    answer = probe["assets"]["body"]
    assert probe["assets"]["status"] == 200
    assert sorted(answer["assets"]) == sorted(ASKED), "an id that is not a track is absent"
    for sid in ASKED:
        expected = {field: rows[sid][field] for field in ASSET_FIELDS}
        # The row strip tempo curve (12042582bb) rides the listing row inside grid_quality.
        expected["tempo"] = rows[sid]["grid_quality"]["tempo"]
        assert answer["assets"][sid] == expected
    assert any(answer["assets"][sid]["preview_b64"] is not None for sid in ASKED)


def test_row_assets_refuses_an_empty_id_batch(probe) -> None:
    """[if] row-assets receives an empty id batch [then] it answers 422, never an empty 200, [else stop]."""
    assert probe["empty"]["status"] == 422


def test_row_assets_answers_a_repeated_id_once(probe) -> None:
    """[if] row-assets receives repeated stable ids [then] each id answers once, as asked alone, [else stop]."""
    answer = probe["repeated"]
    assert answer["status"] == 200
    assert sorted(answer["body"]["assets"]) == sorted(set(REPEATED))
    for sid in set(REPEATED):
        assert answer["body"]["assets"][sid] == probe["assets"]["body"]["assets"][sid]
    assert answer["body"]["pending"] == []


def test_row_assets_keeps_valid_rows_in_a_mixed_availability_batch(probe) -> None:
    """[if] a batch mixes rows with assets, a bare row and a non-track [then] no valid row is dropped, [else stop]."""
    answer = probe["mixed"]
    assert answer["status"] == 200
    assets = answer["body"]["assets"]
    assert sorted(assets) == sorted([_sid(0), _sid(1), BARE]), "only the non-track is absent"
    for sid in (_sid(0), _sid(1)):
        assert assets[sid] == probe["assets"]["body"]["assets"][sid]
        assert assets[sid]["preview_b64"] is not None
    assert assets[BARE]["preview_b64"] is None
