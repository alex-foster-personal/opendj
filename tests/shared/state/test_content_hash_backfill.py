"""Regression tests for ``apps.shared.state.backfill_content_hash``.

Seeds a throwaway ``<tmp>/data/state/state.db`` with two track rows via
``StateWriter`` (the repo's one supported mutation surface -- never a raw
``INSERT``): one whose ``file_path`` points at a real, tiny generated WAV
file, one whose ``file_path`` points nowhere. Every assertion is a fresh
read against the DB, never a remembered number (honest-denominator house
rule).
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from apps.shared import hashing
from apps.shared.state import db as state_db
from apps.shared.state.backfill_content_hash import build_parser, run_backfill
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

GOOD_ID = "a" * 40
DEAD_ID = "b" * 40


def _write_silent_wav(path: Path, seconds: float = 0.1, framerate: int = 44100) -> None:
    """A tiny, valid, silent mono 16-bit WAV -- just needs to be real bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    nframes = int(seconds * framerate)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(framerate)
        fh.writeframes(b"\x00\x00" * nframes)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    return root


@pytest.fixture
def good_wav(tmp_path: Path) -> Path:
    wav_path = tmp_path / "audio" / "good.wav"
    _write_silent_wav(wav_path)
    return wav_path


@pytest.fixture
def seeded(data_dir: Path, good_wav: Path) -> Path:
    """Seed state.db with one resolvable row and one dead-path row."""
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        writer.upsert_track(
            stable_id=GOOD_ID, stable_id_tier="isrc", title="Real Track",
            artists=["Someone"], album="An Album", isrc="GBCEN0900132",
            duration_ms=100, file_path=str(good_wav),
        )
        writer.upsert_track(
            stable_id=DEAD_ID, stable_id_tier="isrc", title="Ghost Track",
            artists=["No One"], album=None, isrc="GBCEN0900133",
            duration_ms=100, file_path=str(data_dir / "audio" / "missing.wav"),
        )
    finally:
        writer.close()
        conn.close()
    return db_path


def _content_hashes(db_path: Path) -> dict[str, str | None]:
    conn = state_db.open_ro(db_path)
    try:
        rows = conn.execute("SELECT stable_id, content_hash FROM tracks").fetchall()
    finally:
        conn.close()
    return dict(rows)


def _event_kinds(db_path: Path, stable_id: str) -> list[str]:
    conn = state_db.open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT kind FROM events WHERE stable_id = ?", (stable_id,)
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def test_dry_run_changes_nothing(seeded: Path, data_dir: Path) -> None:
    report = run_backfill(data_dir, live=False)
    assert report.total == 2
    assert report.resolvable == 1
    assert report.hashed == 1
    assert report.unresolvable == 1
    assert report.live is False

    hashes = _content_hashes(seeded)
    assert hashes[GOOD_ID] is None
    assert hashes[DEAD_ID] is None


def test_live_hashes_exactly_the_resolvable_row(
    seeded: Path, data_dir: Path, good_wav: Path
) -> None:
    dry = run_backfill(data_dir, live=False)
    live = run_backfill(data_dir, live=True)

    # Same computation either way; only persistence differs (R3).
    assert live.total == dry.total
    assert live.resolvable == dry.resolvable
    assert live.hashed == dry.hashed
    assert live.unresolvable == dry.unresolvable
    assert live.live is True

    expected = hashing.sha256_file(good_wav)
    hashes = _content_hashes(seeded)
    assert hashes[GOOD_ID] == expected
    assert hashes[DEAD_ID] is None

    # Went through StateWriter, not a raw UPDATE (R4).
    assert "track.update" in _event_kinds(seeded, GOOD_ID)


def test_rerun_after_live_is_a_no_op(seeded: Path, data_dir: Path, good_wav: Path) -> None:
    run_backfill(data_dir, live=True)
    before = _content_hashes(seeded)[GOOD_ID]

    second = run_backfill(data_dir, live=True)

    assert second.total == 1  # only the dead-path row is still NULL
    assert second.resolvable == 0
    assert second.hashed == 0
    assert second.unresolvable == 1
    assert _content_hashes(seeded)[GOOD_ID] == before


def test_limit_caps_candidate_rows(seeded: Path, data_dir: Path) -> None:
    report = run_backfill(data_dir, live=False, limit=1)
    assert report.total == 1


def test_dead_path_never_crashes_the_run(seeded: Path, data_dir: Path) -> None:
    """A missing file is an unresolvable row, never an exception."""
    report = run_backfill(data_dir, live=True)
    assert report.unresolvable == 1


def test_cli_rejects_dry_run_plus_live() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--dry-run", "--live"])


def test_cli_requires_dry_run_or_live() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_cli_parses_live_with_data_dir_and_limit(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["--data-dir", str(tmp_path), "--limit", "5", "--live"]
    )
    assert args.live is True
    assert args.dry_run is False
    assert args.limit == 5
    assert args.data_dir == tmp_path
