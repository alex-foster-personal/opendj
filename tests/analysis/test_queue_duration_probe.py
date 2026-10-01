"""An installed app's folder-imported tracks are admitted with a MEASURED duration.

- [if] a track has no stored duration [then] admission measures it with ffmpeg, [else stop].

NATIVE-10 (issue #2315). A payload used to omit any tag reader (mutagen, GPL,
was the opt-in ``tags`` extra; tinytag ships since Thu 1 Oct 2026), so
folder ingest wrote ``tracks.duration_ms = NULL`` and the memory-admission
rule refused EVERY folder-imported track as ``duration_unknown``: on the one
install path that needs no rekordbox, the v1 backfill analyzed nothing.
Found by the offline acceptance run against a payload built from main.

The queue now asks the decoder every lane already requires (ffmpeg) for the
length of a track whose row carries none. It never invents one: a file ffmpeg
cannot read, or no ffmpeg at all, still refuses by name.

Acceptance tests:

- [if] a resolvable track has no duration_ms [then] it is admitted at the
  length ffmpeg measures, [else ⛔️].
- [if] a track row carries duration_ms [then] that value wins and ffmpeg is
  not consulted, [else ⛔️].
- [if] ffmpeg cannot read the file, or cannot be found [then] the track is
  refused ``duration_unknown``, never admitted at a guessed length, [else ⛔️].
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from apps.analysis import admission
from apps.analysis.queue_targets import candidates_from_state
from apps.analysis.store import open_conn
from apps.shared.ffmpeg import probe_duration_s

pytestmark = [pytest.mark.requirement("NATIVE-10"), pytest.mark.requires_ffmpeg]

SECONDS = 3.0
RATE = 22_050


def _tone(path: Path, seconds: float = SECONDS) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        frames = int(seconds * RATE)
        out.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / RATE)))
            for i in range(frames)))
    return path


def _track(conn, stable_id: str, audio: Path, duration_ms: int | None) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
        "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, "
        "'2026-09-26T00:00:00Z', '2026-09-26T00:00:00Z')",
        (stable_id, stable_id, duration_ms, str(audio)),
    )
    conn.commit()


def test_probe_duration_reads_the_length_ffmpeg_decodes(tmp_path: Path) -> None:
    measured = probe_duration_s(_tone(tmp_path / "tone.wav"))
    assert measured == pytest.approx(SECONDS, abs=0.05)


def test_probe_duration_is_none_for_a_file_ffmpeg_cannot_read(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"this is prose, not an mp3 frame" * 64)
    assert probe_duration_s(junk) is None


def test_null_duration_track_is_admitted_at_the_measured_length(tmp_path: Path) -> None:
    conn = open_conn(tmp_path / "state.db")
    _track(conn, "sid_null", _tone(tmp_path / "a.wav"), None)
    (candidate,) = candidates_from_state(
        conn, ["sid_null"], lane="waveform", backend="own_waveform.backfill"
    )
    assert candidate.duration_s == pytest.approx(SECONDS, abs=0.05)
    plan = admission.admit([candidate], model=admission.memory_model_for(candidate.backend))
    assert [item.stable_id for item in plan.admitted] == ["sid_null"]
    conn.close()


def test_a_stored_duration_wins_over_the_probe(tmp_path: Path) -> None:
    conn = open_conn(tmp_path / "state.db")
    # The row says 120 s; the file is 3 s. The library's value is used, so the
    # probe is provably not consulted for a row that already knows its length.
    _track(conn, "sid_known", _tone(tmp_path / "b.wav"), 120_000)
    (candidate,) = candidates_from_state(
        conn, ["sid_known"], lane="waveform", backend="own_waveform.backfill"
    )
    assert candidate.duration_s == 120.0
    conn.close()


def test_unreadable_file_is_still_refused_duration_unknown(tmp_path: Path) -> None:
    conn = open_conn(tmp_path / "state.db")
    junk = tmp_path / "c.mp3"
    junk.write_bytes(b"not audio" * 256)
    _track(conn, "sid_junk", junk, None)
    (candidate,) = candidates_from_state(
        conn, ["sid_junk"], lane="waveform", backend="own_waveform.backfill"
    )
    assert candidate.duration_s is None
    plan = admission.admit([candidate], model=admission.memory_model_for(candidate.backend))
    assert [r.reason for r in plan.refused] == [admission.REFUSED_DURATION_UNKNOWN]
    conn.close()


def test_no_ffmpeg_refuses_rather_than_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDT_FFMPEG", str(tmp_path / "no-such-ffmpeg"))
    conn = open_conn(tmp_path / "state.db")
    _track(conn, "sid_noff", _tone(tmp_path / "d.wav"), None)
    (candidate,) = candidates_from_state(
        conn, ["sid_noff"], lane="waveform", backend="own_waveform.backfill"
    )
    assert candidate.duration_s is None
    conn.close()
