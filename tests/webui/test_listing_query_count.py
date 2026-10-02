"""Library listing and playlist detail do no sqlite work per row (LIBM-130, #3962).

[if] a listing page issues sqlite work per row [then] fail, [else stop].

Found at 10k folder-imported tracks (LIBM-120 measurement, Fri 25 Sep 2026):
every row with no rekordbox mapping opened its OWN read-only state.db
connection and re-ran two schema probes plus two lookups, so a 500-row page
paid ~2,500 statements and 500 connections, a 10,000-member playlist 50,000.

The instrument is the real connection's trace callback (``sql_trace``), not a
mock: a page of 20 unmapped rows must issue exactly the statements a page of
4 does. Both sizes sit inside ONE 500-id bind batch, so this pins "no work per
row", not "fixed work at any size": past a batch boundary each batched read
adds one SELECT per 500 ids, which
``tests/shared/state/test_locations.py::test_bulk_local_audio_paths_issues_one_select_per_bind_batch``
pins at 501 and 1001 ids.

The engine runs in a child process that finds the fixture through the
production ``MDT_DATA_DIR`` contract (``listing_probe``), so nothing in the
application path is rebound or replaced. The overshoot control is the artwork
verdict itself: batching the path lookup must not change what any row says,
so every row is checked against the per-row public function the rb-meta
route still uses.

Regression one-liners:
  - if listing N unmapped tracks issues more sqlite statements than listing 4 then broken
  - if listing N unmapped tracks opens more sqlite connections than listing 4 then broken
  - if a listed row's artwork verdict differs from local_artwork_available(stable_id) then broken
  - if the probe runs without MDT_DATA_DIR resolving to the fixture then broken
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from tests.webui.listing_probe import run_probe
from tests.webui.sql_trace import statement_shapes

pytestmark = [pytest.mark.requirement("LIBM-130")]

#: LARGE is sized so every local path the page probes fits ONE request's
#: availability budget (PROBE_BUDGET_ROW_HYDRATION = 16: four probes per five
#: rows here). Past it, the budget's own bounded stats and the background
#: refresher would add work that has nothing to do with per-row lookups.
SMALL, LARGE = 4, 20
NOW = "2026-09-25T00:00:00Z"
SMALL_PLAYLIST, LARGE_PLAYLIST = "pl-small", "pl-large"
TRACKS_SMALL, TRACKS_LARGE = f"/api/v1/tracks?limit={SMALL}", f"/api/v1/tracks?limit={LARGE}"
DETAIL_SMALL = f"/api/v1/playlists/{SMALL_PLAYLIST}"
DETAIL_LARGE = f"/api/v1/playlists/{LARGE_PLAYLIST}"

#: One row per residency shape the path resolver distinguishes, cycled so
#: every page contains all five: file on tracks.file_path; file only via a
#: local track_locations row (tracks.file_path stale); missing file;
#: streaming URI; no path at all.
SHAPES = ("file", "location", "missing", "streaming", "pathless")


def _sid(i: int) -> str:
    return f"{i:040x}"


def _seed_library(root: Path, count: int) -> Path:
    """Seed ``<root>/data/state/state.db``, the layout MDT_DATA_DIR names."""
    audio_dir = root / "audio"
    audio_dir.mkdir()
    # A real cover image beside the files: only rows whose file resolves can
    # reach it, which is what makes the artwork verdict a control below.
    Image.new("RGB", (8, 8), (90, 20, 160)).save(audio_dir / "cover.jpg", format="JPEG")
    state_path = root / "data" / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    conn = state_db.open_rw(state_path)
    try:
        writer = StateWriter(conn, actor="unit-test")
        for i in range(count):
            shape = SHAPES[i % len(SHAPES)]
            real = audio_dir / f"track {i}.mp3"
            if shape in ("file", "location"):
                real.write_bytes(b"ID3" + bytes(1024))
            file_path = {
                "file": str(real),
                "location": str(audio_dir / f"moved away {i}.mp3"),
                "missing": str(audio_dir / f"never existed {i}.mp3"),
                "streaming": f"spotify:track:{i}",
                "pathless": None,
            }[shape]
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, "
                "duration_ms, file_path, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, 180000, ?, ?, ?)",
                (_sid(i), f"track {i}", file_path, NOW, NOW),
            )
            conn.commit()
            if shape == "location":
                writer.upsert_track_location(
                    stable_id=_sid(i), kind="local", file_path=str(real),
                )
        for playlist_id, size in ((SMALL_PLAYLIST, SMALL), (LARGE_PLAYLIST, LARGE)):
            conn.execute(
                "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, "
                "created_at, updated_at) VALUES (?, ?, 'webui', ?, ?, ?)",
                (playlist_id, playlist_id, playlist_id, NOW, NOW),
            )
            conn.executemany(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
                "VALUES (?, ?, ?)",
                [(playlist_id, _sid(i), i) for i in range(size)],
            )
        conn.commit()
        writer.close()
    finally:
        conn.close()
    return state_path


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One engine run over the seeded library, measured in a child process."""
    root = tmp_path_factory.mktemp("listing")
    state_path = _seed_library(root, LARGE)
    result = run_probe(state_path.parent.parent, {
        "mode": "listing",
        "measure": [TRACKS_SMALL, TRACKS_LARGE, DETAIL_SMALL, DETAIL_LARGE],
        "fetch": {"listing": TRACKS_LARGE, "detail": DETAIL_LARGE},
        "oracle_ids": [_sid(i) for i in range(LARGE)],
    }, root)
    result["state_path"] = str(state_path)
    return result


def _assert_constant(probe: dict[str, Any], small_url: str, large_url: str) -> None:
    small, large = probe["measured"][small_url], probe["measured"][large_url]
    assert small["statements"], "trace saw no statements: the instrument is not attached"
    grew = (
        statement_shapes(large["statements"]) - statement_shapes(small["statements"])
    ).most_common(6)
    assert (large["connections"], len(large["statements"])) == (
        small["connections"], len(small["statements"]),
    ), (
        f"{small_url}: {small['connections']} connections / {len(small['statements'])} "
        f"statements; {large_url}: {large['connections']} / {len(large['statements'])}. "
        f"Statement shapes that grew with the row count: {grew}"
    )


def test_track_listing_statement_count_is_constant_in_page_size(probe) -> None:
    """[if] /tracks lists 20 unmapped rows [then] it issues what 4 rows do, [else stop]."""
    _assert_constant(probe, TRACKS_SMALL, TRACKS_LARGE)


def test_playlist_detail_statement_count_is_constant_in_member_count(probe) -> None:
    """[if] a 20-member playlist opens [then] it issues what a 4-member one does, [else stop]."""
    _assert_constant(probe, DETAIL_SMALL, DETAIL_LARGE)


def _as_facts(available: bool | None) -> tuple[bool | None, str]:
    """The (artwork_available, artwork_status) pair for one per-row verdict."""
    if available is True:
        return True, "ok"
    if available is False:
        return False, "no_image_path"
    return None, "unresolved"


def test_batched_artwork_verdicts_match_the_per_row_oracle(probe) -> None:
    """[if] artwork is batched [then] each row matches the per-row verdict, [else stop]."""
    listing = probe["fetched"]["listing"]["items"]
    detail = probe["fetched"]["detail"]["tracks"]
    oracle = {sid: _as_facts(available) for sid, available in probe["oracle"].items()}
    assert len(listing) == LARGE and len(detail) == LARGE
    verdicts: Counter[tuple[bool | None, str]] = Counter()
    for row in detail:
        expected = oracle[row["stable_id"]]
        assert (row["artwork_available"], row["artwork_status"]) == expected, row["stable_id"]
        verdicts[expected] += 1
    for row in listing:
        assert row["artwork_available"] == oracle[row["stable_id"]][0], row["stable_id"]
    # Control: the resolvable shapes (file, location) must reach a different
    # verdict than the unresolvable three, or agreement proves nothing about
    # the batching. The cover.jpg beside the files makes them True.
    resolvable = 2 * LARGE // len(SHAPES)
    assert verdicts[(True, "ok")] == resolvable, verdicts
    assert verdicts[(False, "no_image_path")] == LARGE - resolvable, verdicts


def test_fixture_rows_are_really_unmapped(probe) -> None:
    """[if] the fixture gains a rekordbox mapping [then] fail loudly, [else stop]."""
    conn = sqlite3.connect(probe["state_path"])
    try:
        mapped = conn.execute("SELECT COUNT(*) FROM track_vendor_ids").fetchone()[0]
    finally:
        conn.close()
    assert mapped == 0
