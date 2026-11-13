"""Native-analysis targets refuse an ambiguous normalized audio path (issue #13356).

- [if] two tracks resolve to one normalized audio path [then] the request raises QueueError naming both, [else stop].
- [if] a unique normalized path is analyzed [then] each track is a candidate on its own path, [else stop].
"""

from __future__ import annotations

import sqlite3
import struct
import wave
from pathlib import Path

import pytest

from apps.analysis.queue import QueueError
from apps.analysis.queue_targets import candidates_from_state
from apps.analysis.store import open_conn
from apps.shared.state.ingest.path_collisions import AmbiguousAnalysisPathError

pytestmark = pytest.mark.requirement("SYNCEXPORT-13356")


def _wav(path: Path) -> Path:
    """Write a short silent mono WAV so the path resolves as a real local file."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22_050)
        out.writeframes(struct.pack("<" + "h" * 2205, *([0] * 2205)))
    return path


def _track(conn: sqlite3.Connection, stable_id: str, file_path: str) -> None:
    """Insert one live track row with a stored duration so no probe runs."""
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
        "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, 100, ?, "
        "'2026-11-13T00:00:00Z', '2026-11-13T00:00:00Z')",
        (stable_id, stable_id, file_path),
    )
    conn.commit()


def test_two_tracks_on_one_path_refuse_the_analysis_request(tmp_path: Path) -> None:
    """Two live tracks resolving to one audio file fail loudly, naming both ids and the path."""
    conn = open_conn(tmp_path / "state.db")
    audio = str(_wav(tmp_path / "Song.wav"))
    _track(conn, "sid_a", audio)
    _track(conn, "sid_b", audio)
    with pytest.raises(QueueError, match="ambiguous analysis path") as caught:
        candidates_from_state(conn, ["sid_a", "sid_b"], lane="waveform", backend="own_waveform.backfill")
    cause = caught.value.__cause__
    assert isinstance(cause, AmbiguousAnalysisPathError)
    assert cause.stable_ids == ["sid_a", "sid_b"]
    assert cause.paths == [audio]
    conn.close()


def test_unique_paths_still_resolve_to_one_candidate_each(tmp_path: Path) -> None:
    """Distinct normalized paths keep the existing one-candidate-per-track behavior."""
    conn = open_conn(tmp_path / "state.db")
    first = str(_wav(tmp_path / "one.wav"))
    second = str(_wav(tmp_path / "two.wav"))
    _track(conn, "sid_a", first)
    _track(conn, "sid_b", second)
    candidates = candidates_from_state(conn, ["sid_a", "sid_b"], lane="waveform", backend="own_waveform.backfill")
    assert {(c.stable_id, c.file_path) for c in candidates} == {("sid_a", first), ("sid_b", second)}
    conn.close()
