"""H2: a set history must never present an analysed value as a performed one.

Rekordbox records no played BPM, no pitch-fader %, no live key shift and no
in/out points actually hit. Those columns therefore carry an explicit
NOT-RECORDED marker and are NEVER back-filled from the library's analysed
values -- a refactor that "helpfully" fills them makes every future set
analysis treat analysed tempo as performed tempo, and the distinction is then
unrecoverable from the CSV.

Mini-PRD
========
* [if] a history session is exported [then] played_bpm and played_pct carry the
  NOT-RECORDED marker on every row [else -] a derived value ships as measured.
* [if] a track has an analysed BPM of 124.00 [then] native_bpm is 124.00 and
  played_bpm is still NOT-RECORDED [else -] the two columns have collapsed.
* [if] the printed table shows a Pitch% column [then] it reads n/r, never a
  number [else -] the console lies where the CSV does not.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.audit import session_history

NOT_RECORDED = "NOT_RECORDED"
UNRECORDED_COLUMNS = ("played_bpm", "played_pct")


def _track(order: int, *, bpm: float | None, title: str = "Quiet Curriculum") -> session_history.PlayedTrack:
    """One resolved history row, shaped exactly as _resolve emits it."""
    return session_history.PlayedTrack(
        order=order,
        title=title,
        artist="Tomas Rye",
        bpm=bpm,
        camelot="9A",
        classical="Em",
        rating=4,
        folder_path="/Users/user/Music/Manual Library/quiet-curriculum.aiff",
        content_id=f"content-{order}",
        playlists=["Warm Up Set"],
        played_at="21:04:11",
        gap="4:12",
        is_streaming=False,
    )


def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracks) -> list[dict[str, str]]:
    monkeypatch.setattr(session_history.paths, "PROJECT_ROOT", tmp_path)
    csv_path = session_history._save("HISTORY 1977580217", tracks)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_export_carries_separate_analysed_and_performed_tempo_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """native_bpm and played_bpm must remain two distinct columns."""
    monkeypatch.setattr(session_history.paths, "PROJECT_ROOT", tmp_path)
    csv_path = session_history._save("HISTORY 1", [])

    with csv_path.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))

    for column in ("native_bpm", *UNRECORDED_COLUMNS):
        assert column in header, f"{column} disappeared from the session export header"
    assert len(set(header)) == len(header), "the export header has duplicate columns"


def test_played_columns_are_not_recorded_on_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every exported row marks the performance columns as unrecorded."""
    rows = _export(
        tmp_path,
        monkeypatch,
        [_track(1, bpm=124.0), _track(2, bpm=None), _track(3, bpm=87.5)],
    )

    assert len(rows) == 3
    for row in rows:
        for column in UNRECORDED_COLUMNS:
            assert row[column] == NOT_RECORDED, (
                f"row {row['order']} column {column} reads {row[column]!r}; "
                "Rekordbox does not record it, so it must stay explicitly unrecorded"
            )


def test_an_analysed_bpm_never_leaks_into_the_played_bpm_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 124.00 analysed tempo lands in native_bpm and nowhere else."""
    rows = _export(tmp_path, monkeypatch, [_track(1, bpm=124.0)])
    row = rows[0]

    assert row["native_bpm"] == "124.00"
    assert row["played_bpm"] == NOT_RECORDED
    assert row["played_bpm"] != row["native_bpm"], "the two tempo columns have collapsed into one"
    assert "124" not in row["played_pct"], "pitch % must not be derived from the analysed tempo"


def test_a_track_without_an_analysed_bpm_still_marks_the_played_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing analysed tempo is blank; the performed one stays NOT_RECORDED."""
    rows = _export(tmp_path, monkeypatch, [_track(1, bpm=None)])
    row = rows[0]

    assert row["native_bpm"] == "", "an unanalysed track has no native BPM to report"
    for column in UNRECORDED_COLUMNS:
        assert row[column] == NOT_RECORDED, (
            f"{column} fell back to a blank when the analysed tempo was missing; "
            "blank reads as 'no data yet', NOT_RECORDED reads as 'never knowable'"
        )


def test_the_printed_table_shows_pitch_as_not_recorded_rather_than_a_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console readout must not invent a pitch-fader figure either."""
    with session_history.console.capture() as capture:
        session_history._print_table("HISTORY 1977580217", [_track(1, bpm=124.0)])
    rendered = capture.get()

    assert "Pitch%" in rendered, "the pitch column disappeared from the printed set list"
    assert "n/r" in rendered, "the pitch column must read n/r, the not-recorded marker"
    assert "124.0" in rendered, "the analysed BPM should still be shown, in the BPM column"
    # Exactly one 124 reading: the analysed one. A second would mean the pitch
    # or played column had been back-filled from it.
    assert rendered.count("124") == 1, (
        "the analysed tempo appears more than once in the printed table - a "
        f"performance column has been back-filled from it:\n{rendered}"
    )


def test_the_module_contract_still_names_what_rekordbox_does_not_record() -> None:
    """The contract that justifies the markers must survive a refactor."""
    contract = session_history.__doc__ or ""

    assert "does NOT record" in contract, "the not-recorded contract was dropped from the module"
    for phrase in ("played BPM", "pitch-fader", "key shift", "in/out points"):
        assert phrase in contract, f"the contract no longer names {phrase!r} as unrecorded"
    # rode_for is inferred from the gap between consecutive plays, not measured.
    assert "approximates" in contract, (
        "rode_for must stay documented as approximated from the gap to the next "
        "play, or a derived number is read downstream as a measured duration"
    )
