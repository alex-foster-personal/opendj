"""CUE sheet and plain tracklist files beside the recording (issue #10086)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sets.tracklist_export import (
    Tracklist,
    TracklistRow,
    format_start_label,
    render_cue_sheet,
    render_plain_tracklist,
    write_tracklist_files,
)

REQ = "SET-10086"


def _row(position: int, start_s: float, artist: str, title: str) -> TracklistRow:
    """Build one timeline row with the given start, artist and title."""
    return TracklistRow(
        position=position,
        start_s=start_s,
        start_label=format_start_label(start_s),
        artist=artist,
        title=title,
        display_name=f"{artist} - {title}",
        key=None,
        played_s=None,
        played_label=None,
        transition_label=None,
        hidden=False,
        track_stable_id=None,
        source=None,
        deck=None,
        duration_ms=None,
        file_path=None,
    )


def _three_changes() -> Tracklist:
    """Timeline with track changes at 0 s, 312.4 s and 655 s."""
    rows = [
        _row(1, 0.0, "A1", "One"),
        _row(2, 312.4, "A2", "Two"),
        _row(3, 655.0, "A3", "Three"),
    ]
    return Tracklist(session_id="s1", rows=rows, segments=[])


def _parse_cue(text: str) -> tuple[str | None, list[dict[str, str]]]:
    """Minimal CUE parser: the FILE name and per-track TITLE, PERFORMER and INDEX 01."""
    file_name: str | None = None
    tracks: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("FILE "):
            file_name = line.split('"')[1]
        elif line.startswith("TRACK "):
            tracks.append({"no": line.split()[1]})
        elif tracks and line.startswith(("TITLE ", "PERFORMER ")):
            key, _, rest = line.partition(" ")
            tracks[-1][key] = rest[1:-1]
        elif tracks and line.startswith("INDEX 01 "):
            tracks[-1]["INDEX"] = line.split()[2]
    return file_name, tracks


@pytest.mark.requirement(REQ)
def test_cue_names_recording_and_indexes_at_75_fps() -> None:
    """[if] three track changes and a recording [then] FILE line and floored mm:ss:ff indexes, [else stop]."""
    cue = render_cue_sheet(_three_changes(), "s1.wav")
    assert 'FILE "s1.wav" WAVE' in cue
    file_name, tracks = _parse_cue(cue)
    assert file_name == "s1.wav"
    assert [t["no"] for t in tracks] == ["01", "02", "03"]
    assert [t["INDEX"] for t in tracks] == ["00:00:00", "05:12:30", "10:55:00"]
    assert [(t["PERFORMER"], t["TITLE"]) for t in tracks] == [("A1", "One"), ("A2", "Two"), ("A3", "Three")]


@pytest.mark.requirement(REQ)
def test_cue_frames_round_down() -> None:
    """[if] a change lands mid-frame [then] the frame count is rounded down, [else stop]."""
    tl = Tracklist(session_id="s1", rows=[_row(1, 1.999, "A", "T")], segments=[])
    assert "INDEX 01 00:01:74" in render_cue_sheet(tl, "s1.wav")


@pytest.mark.requirement(REQ)
def test_cue_without_audio_has_no_file_line() -> None:
    """[if] the session has no audio [then] the CUE names no FILE line, [else stop]."""
    cue = render_cue_sheet(_three_changes(), None)
    assert "FILE " not in cue
    assert _parse_cue(cue)[0] is None
    assert len(_parse_cue(cue)[1]) == 3


@pytest.mark.requirement(REQ)
def test_cue_quotes_in_titles_parse_cleanly() -> None:
    """[if] a title carries double quotes [then] the CUE stays parseable, [else stop]."""
    tl = Tracklist(session_id="s1", rows=[_row(1, 0.0, 'The "X"', 'Say "Hi"')], segments=[])
    _, tracks = _parse_cue(render_cue_sheet(tl, "s1.wav"))
    assert tracks[0]["TITLE"] == "Say 'Hi'"
    assert tracks[0]["PERFORMER"] == "The 'X'"


@pytest.mark.requirement(REQ)
def test_plain_tracklist_lines() -> None:
    """[if] three track changes [then] the TXT has mm:ss two spaces Artist - Title lines, [else stop]."""
    assert render_plain_tracklist(_three_changes()) == ("00:00  A1 - One\n05:12  A2 - Two\n10:55  A3 - Three\n")


@pytest.mark.requirement(REQ)
def test_write_files_beside_recording(tmp_path: Path) -> None:
    """[if] files are written into the session dir [then] the CUE names the joined WAV only when present, [else stop]."""
    tl = _three_changes()
    cue_path, txt_path = write_tracklist_files(tmp_path, tl)
    assert (cue_path.name, txt_path.name) == ("s1.cue", "s1.tracklist.txt")
    assert "FILE " not in cue_path.read_text(encoding="utf-8")
    assert txt_path.read_text(encoding="utf-8") == render_plain_tracklist(tl)

    (tmp_path / "s1.wav").write_bytes(b"\x00")
    cue_path, _ = write_tracklist_files(tmp_path, tl)
    assert 'FILE "s1.wav" WAVE' in cue_path.read_text(encoding="utf-8")
