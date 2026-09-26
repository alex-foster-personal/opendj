"""Library listing and playlist detail cost O(1) sqlite work per call (LIBM-128, #3962).

[if] a listing page issues sqlite work per row [then] fail, [else stop].

Found at 10k folder-imported tracks (LIBM-120 measurement, Fri 25 Sep 2026):
every row with no rekordbox mapping opened its OWN read-only state.db
connection and re-ran two schema probes plus two lookups, so a 500-row page
paid ~2,500 statements and 500 connections, a 10,000-member playlist 50,000.

The instrument is the real connection's trace callback (``sql_trace``), not a
mock: a page of 20 unmapped rows must issue exactly the statements a page of
4 does. The overshoot control is the artwork verdict itself: batching the
path lookup must not change what any row says, so every row is checked
against the per-row public function the rb-meta route still uses.

Regression one-liners:
  - if listing N unmapped tracks issues more sqlite statements than listing 4 then broken
  - if listing N unmapped tracks opens more sqlite connections than listing 4 then broken
  - if a listed row's artwork verdict differs from local_artwork_available(stable_id) then broken
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.paths import local_artwork_available
from apps.shared._mutagen import HAS_MUTAGEN
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.sql_trace import SqlTrace, statement_shapes, trace_sqlite

pytestmark = [pytest.mark.requirement("LIBM-128")]

#: LARGE is sized so every local path the page probes fits ONE request's
#: availability budget (PROBE_BUDGET_ROW_HYDRATION = 16: four probes per five
#: rows here). Past it, the budget's own bounded stats and the background
#: refresher would add work that has nothing to do with per-row lookups.
SMALL, LARGE = 4, 20
NOW = "2026-09-25T00:00:00Z"
SMALL_PLAYLIST, LARGE_PLAYLIST = "pl-small", "pl-large"
#: ``apps.webui.server.path_availability_refresh``'s worker thread name.
REFRESH_THREAD = "path-availability-refresh"

#: One row per residency shape the path resolver distinguishes, cycled so
#: every page contains all five: file on tracks.file_path; file only via a
#: local track_locations row (tracks.file_path stale); missing file;
#: streaming URI; no path at all.
SHAPES = ("file", "location", "missing", "streaming", "pathless")


def _sid(i: int) -> str:
    return f"{i:040x}"


def _seed_library(tmp_path: Path, count: int) -> Path:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    state_path = tmp_path / "state.db"
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


@pytest.fixture
def traced_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, SqlTrace]]:
    state_path = _seed_library(tmp_path, LARGE)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    with trace_sqlite() as trace:
        app = create_app(
            backend=SqliteBackend(state_path),
            bind_host="127.0.0.1",
            hostname="test-host",
            state_db_path=str(state_path),
            mount_frontend=False,
        )
        with TestClient(app) as client:
            yield client, trace


def _measure(client: TestClient, trace: SqlTrace, url: str) -> tuple[int, list[str]]:
    """Connections and statements of one warm request, request threads only.

    The first call warms per-process caches (machine id, the availability
    L1) that are not the defect. The background availability refresher is
    excluded: it runs on its own schedule, not per listed row.
    """
    assert client.get(url).status_code == 200
    trace.reset()
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert not trace.untraced_outside(REFRESH_THREAD), (
        f"{url}: a connection closed before its trace attached, so its statements "
        "went uncounted"
    )
    return (
        trace.connections_outside(REFRESH_THREAD),
        trace.statements_outside(REFRESH_THREAD),
    )


def _assert_constant(client: TestClient, trace: SqlTrace, small_url: str, large_url: str) -> None:
    small_conns, small_stmts = _measure(client, trace, small_url)
    large_conns, large_stmts = _measure(client, trace, large_url)
    assert small_stmts, "trace saw no statements: the instrument is not attached"
    grew = (statement_shapes(large_stmts) - statement_shapes(small_stmts)).most_common(6)
    assert (large_conns, len(large_stmts)) == (small_conns, len(small_stmts)), (
        f"{small_url}: {small_conns} connections / {len(small_stmts)} statements; "
        f"{large_url}: {large_conns} / {len(large_stmts)}. Statement shapes that "
        f"grew with the row count: {grew}"
    )


def test_track_listing_statement_count_is_constant_in_page_size(traced_client) -> None:
    """[if] /tracks lists 20 unmapped rows [then] it issues what 4 rows do, [else stop]."""
    client, trace = traced_client
    _assert_constant(
        client, trace, f"/api/v1/tracks?limit={SMALL}", f"/api/v1/tracks?limit={LARGE}",
    )


def test_playlist_detail_statement_count_is_constant_in_member_count(traced_client) -> None:
    """[if] a 20-member playlist opens [then] it issues what a 4-member one does, [else stop]."""
    client, trace = traced_client
    _assert_constant(
        client, trace,
        f"/api/v1/playlists/{SMALL_PLAYLIST}", f"/api/v1/playlists/{LARGE_PLAYLIST}",
    )


def _oracle(stable_id: str) -> tuple[bool | None, str]:
    """The pre-batching per-row verdict, via the function rb-meta still calls."""
    available = local_artwork_available(stable_id)
    if available is True:
        return True, "ok"
    if available is False:
        return False, "no_image_path"
    return None, "unresolved"


def test_batched_artwork_verdicts_match_the_per_row_oracle(traced_client) -> None:
    """[if] artwork is batched [then] each row matches the per-row verdict, [else stop]."""
    client, _trace = traced_client
    listing = client.get(f"/api/v1/tracks?limit={LARGE}").json()["items"]
    detail = client.get(f"/api/v1/playlists/{LARGE_PLAYLIST}").json()["tracks"]
    assert len(listing) == LARGE and len(detail) == LARGE
    verdicts: Counter[tuple[bool | None, str]] = Counter()
    for row in detail:
        expected = _oracle(row["stable_id"])
        assert (row["artwork_available"], row["artwork_status"]) == expected, row["stable_id"]
        verdicts[expected] += 1
    for row in listing:
        assert row["artwork_available"] == _oracle(row["stable_id"])[0], row["stable_id"]
    # Control: the resolvable shapes (file, location) must reach a different
    # verdict than the unresolvable three, or agreement proves nothing about
    # the batching. With a tag reader the fixture's picture-less files read
    # False, so only the no-reader build can tell them apart by verdict.
    resolvable = 2 * LARGE // len(SHAPES)
    if HAS_MUTAGEN:
        assert verdicts == Counter({(False, "no_image_path"): LARGE}), verdicts
    elif not HAS_MUTAGEN:
        assert verdicts[(None, "unresolved")] == resolvable, verdicts
        assert verdicts[(False, "no_image_path")] == LARGE - resolvable, verdicts


def test_fixture_rows_are_really_unmapped(traced_client, tmp_path: Path) -> None:
    """[if] the fixture gains a rekordbox mapping [then] fail loudly, [else stop]."""
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        mapped = conn.execute("SELECT COUNT(*) FROM track_vendor_ids").fetchone()[0]
    finally:
        conn.close()
    assert mapped == 0
