"""Per-row lyric summaries and title flags on every listing row (PR-4b, B).

The browser's Lyrics column, its 'lyrics' sort key and the Vocals filter all
read one field per row. These tests pin that the field is hydrated by ONE
bulk verdict read inside ``build_track_rows`` and that its absence stays
absent.

The bulk read has no standalone public helper on this branch: the summary is
shaped inside ``build_track_rows`` from ``lyric_store.bulk_verdicts``, so the
row builder IS the surface under test here.

- if a track with no live lyric_verdict row starts carrying a verdict then
  the UI states a calibrated no-lyrics claim the pipeline never made --
  broken
- if has_words stops tracking words_content_hash (e.g. reverts to
  bool(n_words)) then a coverage-only row opens the word-level surfaces and
  the words route 404s behind them -- broken
- if a human override stops winning the effective verdict then the listing
  disagrees with the triage table over the same row -- broken
- if a missing state.db raises instead of yielding no summaries then a fresh
  machine cannot list its library at all -- broken
- if 'Radio Edit' starts counting as a remix then the Remixes filter hides
  the canonical single (the maintainer's call, Tue 1 Sep 2026) -- broken
- if GET /api/v1/tracks stops serializing the nine-key lyric object then
  every consumer of the listing wire type breaks at once -- broken
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.lyrics import store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.backend import Track
from apps.webui.server.rb_vendor_pkg import track_rows
from apps.webui.server.sqlite_backend import SqliteBackend

WORDS_ID = "lyr-row-words"
NOHASH_ID = "lyr-row-nohash"
WORDS_TITLE = "Song (VIP)"
NOHASH_TITLE = "Song (Radio Edit)"
DIGEST = "b" * 64
COMPUTED_AT = "2026-09-01T00:00:00.000000+00:00"
SUMMARY_KEYS = {
    "verdict",
    "effective",
    "n_words",
    "has_words",
    "n_lines",
    "pct_witness_red",
    "source",
    "language_iso3",
    "override",
}


#-----------------------------------------------------------------------------
# fixtures
#-----------------------------------------------------------------------------
def _upsert_verdict(conn: sqlite3.Connection, stable_id: str, **overrides: object) -> None:
    fields: dict[str, object] = {
        "verdict": "vocal",
        "coverage_pct": 80.0,
        "source": "lrclib get",
        "language_iso3": "eng",
        "n_words": 42,
        "n_lines": 7,
        "pct_witness_red": 0.25,
        "pipeline_version": PIPELINE_VERSION,
        "words_content_hash": DIGEST,
        "computed_at": COMPUTED_AT,
        "resurrect": False,
    }
    fields.update(overrides)
    store.upsert_verdict(conn, stable_id=stable_id, **fields)  # type: ignore[arg-type]


def _seed_track(writer: StateWriter, stable_id: str, title: str, file_name: str) -> None:
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="inferred",
        title=title,
        artists=["Depeche Mode"],
        album=None,
        isrc=None,
        duration_ms=203_000,
        file_path=f"/music/{file_name}",
    )


@pytest.fixture
def lyric_state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A state.db in the canonical ``<data-dir>/state/state.db`` layout.

    Canonical because ``sync_stamp`` derives the machine identity from the DB
    path: a flat ``tmp_path/state.db`` cannot stamp a single verdict write.
    ``config`` is patched as a module attribute so every reader that
    dereferences ``config.STATE_DB`` at call time sees the fixture.

    NOHASH carries a word COUNT but no words_content_hash on purpose: that is
    the row that separates has_words from bool(n_words).
    """
    path = tmp_path / "data" / "state" / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        _seed_track(writer, WORDS_ID, WORDS_TITLE, "words.mp3")
        _seed_track(writer, NOHASH_ID, NOHASH_TITLE, "nohash.mp3")
        _upsert_verdict(conn, WORDS_ID)
        _upsert_verdict(
            conn,
            NOHASH_ID,
            verdict="sparse",
            coverage_pct=20.0,
            source=None,
            language_iso3=None,
            n_lines=None,
            pct_witness_red=None,
            words_content_hash=None,
        )
    finally:
        writer.close()
        conn.close()
    monkeypatch.setattr(rb_config, "STATE_DB", path)
    return path


@pytest.fixture
def listing_client(lyric_state_db: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(lyric_state_db),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(lyric_state_db),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


def _rows_by_id(*tracks: Track) -> dict[str, dict[str, Any]]:
    return {row["stable_id"]: row for row in track_rows.build_track_rows(list(tracks))}


#-----------------------------------------------------------------------------
# the bulk verdict read behind build_track_rows
#-----------------------------------------------------------------------------
def test_every_seeded_track_gets_its_own_summary(lyric_state_db: Path) -> None:
    """if the bulk read drops a seeded row then the column is blank for a
    track the pipeline has already judged -- broken"""
    rows = _rows_by_id(
        Track(stable_id=WORDS_ID, title=WORDS_TITLE),
        Track(stable_id=NOHASH_ID, title=NOHASH_TITLE),
    )

    assert set(rows) == {WORDS_ID, NOHASH_ID}
    assert set(rows[WORDS_ID]["lyrics"]) == SUMMARY_KEYS
    assert rows[WORDS_ID]["lyrics"] == {
        "verdict": "vocal",
        "effective": "vocal",
        "n_words": 42,
        "has_words": True,
        "n_lines": 7,
        "pct_witness_red": 0.25,
        "source": "lrclib get",
        "language_iso3": "eng",
        "override": None,
    }


def test_has_words_tracks_the_hash_not_the_word_count(lyric_state_db: Path) -> None:
    """if has_words stops being the words_content_hash test then a
    coverage-only row opens word surfaces that have no artifact -- broken"""
    rows = _rows_by_id(
        Track(stable_id=WORDS_ID, title=WORDS_TITLE),
        Track(stable_id=NOHASH_ID, title=NOHASH_TITLE),
    )

    assert rows[WORDS_ID]["lyrics"]["has_words"] is True
    assert rows[NOHASH_ID]["lyrics"]["n_words"] == 42, "the count alone must not decide"
    assert rows[NOHASH_ID]["lyrics"]["has_words"] is False
    assert rows[NOHASH_ID]["lyrics"]["verdict"] == "sparse"
    assert rows[NOHASH_ID]["lyrics"]["n_lines"] is None


def test_effective_verdict_honours_a_human_override(lyric_state_db: Path) -> None:
    """if the override stops beating the computed verdict then the listing
    disagrees with triage over the same row -- broken"""
    conn = state_db.open_rw(lyric_state_db)
    try:
        store.set_override(conn, stable_id=WORDS_ID, override="no-lyrics", note="mine")
    finally:
        conn.close()

    summary = _rows_by_id(Track(stable_id=WORDS_ID, title=WORDS_TITLE))[WORDS_ID]["lyrics"]

    assert summary["effective"] == "no-lyrics"
    assert summary["override"] == "no-lyrics"
    assert summary["verdict"] == "vocal", "the computed verdict must survive the override"


def test_unknown_stable_ids_are_absent_not_nulled(lyric_state_db: Path) -> None:
    """if an unjudged track gets a fabricated entry then 'no data yet' and
    'no lyrics' become the same state -- broken"""
    rows = _rows_by_id(
        Track(stable_id=WORDS_ID, title=WORDS_TITLE),
        Track(stable_id="never-judged", title="Plain Title"),
    )

    assert rows["never-judged"]["lyrics"] is None
    assert rows[WORDS_ID]["lyrics"] is not None


def test_missing_state_db_yields_no_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a library with no state.db raises here then a fresh machine cannot
    list its tracks at all -- broken"""
    monkeypatch.setattr(rb_config, "STATE_DB", tmp_path / "nope" / "state.db")

    rows = track_rows.build_track_rows([Track(stable_id=WORDS_ID, title=WORDS_TITLE)])

    assert rows[0]["lyrics"] is None


def test_empty_input_builds_no_rows(lyric_state_db: Path) -> None:
    """if an empty page fabricates rows then a blank playlist paints tracks
    it does not hold -- broken"""
    assert track_rows.build_track_rows([]) == []


#-----------------------------------------------------------------------------
# build_track_rows: the three listing keys together
#-----------------------------------------------------------------------------
def test_build_track_rows_carries_lyrics_and_title_flags(lyric_state_db: Path) -> None:
    """if a listing row loses lyrics/is_remix/is_radio_edit then the column,
    the sort key and both filters have no data source -- broken"""
    rows = _rows_by_id(
        Track(stable_id=WORDS_ID, title=WORDS_TITLE),
        Track(stable_id=NOHASH_ID, title=NOHASH_TITLE),
    )

    assert rows[WORDS_ID]["lyrics"]["has_words"] is True
    assert rows[WORDS_ID]["lyrics"]["n_lines"] == 7
    assert rows[WORDS_ID]["is_remix"] is True
    assert rows[WORDS_ID]["is_radio_edit"] is False
    assert rows[NOHASH_ID]["lyrics"]["has_words"] is False
    assert rows[NOHASH_ID]["is_remix"] is False
    assert rows[NOHASH_ID]["is_radio_edit"] is True


#-----------------------------------------------------------------------------
# GET /api/v1/tracks
#-----------------------------------------------------------------------------
def test_tracks_listing_serializes_the_lyric_summary_object(
    listing_client: TestClient,
) -> None:
    """if the wire shape drifts from LyricsRowSummaryOut then api-types.ts
    and every UI consumer of it are wrong at once -- broken"""
    items = {
        item["stable_id"]: item
        for item in listing_client.get("/api/v1/tracks", params={"limit": 50}).json()["items"]
    }

    assert set(items) == {WORDS_ID, NOHASH_ID}
    assert set(items[WORDS_ID]["lyrics"]) == SUMMARY_KEYS
    assert items[WORDS_ID]["lyrics"]["effective"] == "vocal"
    assert items[WORDS_ID]["lyrics"]["n_words"] == 42
    assert items[WORDS_ID]["is_remix"] is True
    assert items[NOHASH_ID]["is_radio_edit"] is True
    assert items[NOHASH_ID]["lyrics"]["has_words"] is False


def test_tracks_listing_serializes_an_unjudged_row_as_null(
    listing_client: TestClient, lyric_state_db: Path
) -> None:
    """if an unjudged row serializes an object instead of null then the UI
    cannot tell 'not processed' from 'processed, no lyrics' -- broken"""
    conn = state_db.open_rw(lyric_state_db)
    try:
        writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
        _seed_track(writer, "lyr-row-unjudged", "Plain Title", "unjudged.mp3")
        writer.close()
    finally:
        conn.close()

    items = {
        item["stable_id"]: item
        for item in listing_client.get("/api/v1/tracks", params={"limit": 50}).json()["items"]
    }

    assert items["lyr-row-unjudged"]["lyrics"] is None
    assert items["lyr-row-unjudged"]["is_remix"] is False
    assert items["lyr-row-unjudged"]["is_radio_edit"] is False
