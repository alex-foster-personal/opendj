"""Regression test for the streaming-track stable_id collision fix.

Fan-out v2 triage finding (2/3 consensus, see
``.planning/FAN-OUT-V2-TRIAGE-2026-04-17.md``):

Before the fix, ``apps/shared/state/ingest/rekordbox.py`` set
``path_str = ""`` for every streaming row, so the tier-3 stable_id
input was always ``"|0.0"`` and every ISRC-less streaming track hashed
to the same SHA-1. The intra-run ``seen_sids`` dedupe then silently
dropped all but the first such track.

The fix feeds the raw ``folder_path`` (e.g. ``spotify:track:ABC...``)
into tier-3 even for streaming rows, while still suppressing
``os.path.exists`` / ``os.path.getmtime`` calls on non-filesystem
schemes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.writer import StateWriter


class _FakeRbDb:
    """Stand-in for ``pyrekordbox.Rekordbox6Database``.

    ``ingest_rb`` only calls ``_rb_rows`` / ``_rb_playlists`` (both
    monkeypatched) and ``close()`` in its finally block.
    """

    def get_cue(self) -> list[object]:
        # No djmdCue rows: these tests are about tracks, not cues (CUES-01).
        return []

    def close(self) -> None:
        return None


@pytest.fixture
def writer_and_conn(tmp_path: Path):
    conn = state_db.open_rw(tmp_path / "state.db")
    w = StateWriter(conn, bus=FakeEventBus(), actor="ingest-rb-streaming")
    try:
        yield w, conn
    finally:
        w.close()
        conn.close()


def _patch_rb_ingest(monkeypatch, rows, playlists=()):
    # ingest_rb does ``from pyrekordbox import Rekordbox6Database`` at
    # runtime; patch the attribute on the real module so the lazy import
    # picks up our stub.
    import pyrekordbox

    monkeypatch.setattr(
        pyrekordbox, "Rekordbox6Database", lambda *a, **k: _FakeRbDb(),
        raising=False,
    )
    monkeypatch.setattr(rb_ingest, "_rb_rows", lambda _rb: iter(rows))
    monkeypatch.setattr(
        rb_ingest, "_rb_playlists", lambda _rb: iter(playlists)
    )


def _make_streaming_row(rb_id: str, uri: str) -> dict:
    return {
        "id": rb_id,
        "title": f"t-{rb_id}",
        "artist": "a",
        "album": "b",
        "folder_path": uri,
        "is_streaming": True,
        "isrc": None,
        "bpm": None,
        "rating": None,
        "duration_ms": None,
        "file_size": None,
        "key_name": None,
        "updated_at": None,
    }


def test_streaming_rows_with_distinct_uris_do_not_collide(
    monkeypatch, writer_and_conn
):
    """Two ISRC-less streaming rows with distinct ``folder_path`` URIs
    must produce distinct ``stable_id`` values and both land in the
    ``tracks`` table.
    """
    writer, conn = writer_and_conn
    rows = [
        _make_streaming_row("S1", "spotify:track:AAAAAAAAAAAAAAAAAAAAAA"),
        _make_streaming_row("S2", "spotify:track:BBBBBBBBBBBBBBBBBBBBBB"),
    ]
    _patch_rb_ingest(monkeypatch, rows)

    report = rb_ingest.ingest_rb(
        writer, Path("/unused/rb.db"), dry_run=False
    )

    assert report.tracks_inserted == 2, (
        "expected both streaming rows to insert; before the fix the "
        "seen_sids guard silently dropped the second row."
    )
    assert report.tracks_skipped == 0

    sids = [
        r[0]
        for r in conn.execute(
            "SELECT stable_id FROM tracks ORDER BY stable_id"
        ).fetchall()
    ]
    assert len(sids) == 2
    assert len(set(sids)) == 2, (
        "streaming rows collided on the same stable_id; tier-3 hash "
        "must include the streaming URI."
    )


def test_streaming_rows_with_same_uri_still_dedupe(
    monkeypatch, writer_and_conn
):
    """Two streaming rows that legitimately share the same ``folder_path``
    URI must still collapse to one track (idempotency guarantee).
    """
    writer, conn = writer_and_conn
    uri = "spotify:track:CCCCCCCCCCCCCCCCCCCCCC"
    rows = [
        _make_streaming_row("C1", uri),
        _make_streaming_row("C2", uri),
    ]
    _patch_rb_ingest(monkeypatch, rows)

    report = rb_ingest.ingest_rb(
        writer, Path("/unused/rb.db"), dry_run=False
    )

    assert report.tracks_inserted == 1
    assert report.tracks_skipped == 1
    tracks_count = conn.execute(
        "SELECT COUNT(*) FROM tracks"
    ).fetchone()[0]
    assert tracks_count == 1
