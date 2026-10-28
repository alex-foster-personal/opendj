"""Sources-map matching for a fresh MIK copy batch (MIKBF-01).

- [if] a copy has one source line that equals one row below the library root [then] it resolves to that row, [else stop].
- [if] a copy has zero or several source lines or rows [then] it is unmatched with a reason and counted, [else stop].
- [if] a store song's path is not under the batch folder [then] it is ignored and counted, [else stop].
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from apps.mik import cli, mikdb
from apps.mik import load as loader
from apps.mik import sources_match as sources

pytestmark = pytest.mark.requirement("MIKBF-01")

ROOT = "/Users/dev/Music/air-library"
BATCH = "/Users/dev/Music/_ingest/mik-batch-20261013-1200"


def _batch(tmp_path: Path, lines: list[str]) -> Path:
    """A real batch folder on disk holding only ``.sources.txt``."""
    folder = tmp_path / "mik-batch"
    folder.mkdir()
    (folder / sources.SOURCES_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def _songs(make_mik_store, folder: Path, names: list[str], extra: list[dict] | None = None):
    store = make_mik_store(
        [
            {"path": f"{folder}/{name}", "energy": 4.0 + index, "segments": [(0.0, 30.0, 5)]}
            for index, name in enumerate(names)
        ]
        + (extra or [])
    )
    conn = mikdb.open_ro(store)
    try:
        songs, _stats = mikdb.read_songs(conn)
    finally:
        conn.close()
    return store, songs


def test_one_source_line_and_one_row_resolves(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    add_track("a" * 40, file_path="/Users/old/Music/House/x.mp3")
    folder = _batch(tmp_path, [f"{ROOT}/Music/House/x.mp3"])
    _store, songs = _songs(make_mik_store, folder, ["x.mp3"])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.report.matches[1].stable_id == "a" * 40
    assert result.report.matches[1].tier == sources.SOURCES_TIER
    assert result.report.unmatched == {}
    assert result.counts() == {
        "store_songs": 1,
        "batch_songs": 1,
        "ignored_outside_batch": 0,
        "source_lines": 1,
        "resolved": 1,
        "unresolved": 0,
    }


def test_equality_not_suffix_and_not_basename(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    """A row whose path merely ENDS with the source remainder is not a hit."""
    add_track("a" * 40, file_path="/Users/old/Backup/Music/House/x.mp3")
    add_track("b" * 40, file_path="/Users/old/Music/Techno/x.mp3")
    folder = _batch(tmp_path, [f"{ROOT}/Music/House/x.mp3"])
    _store, songs = _songs(make_mik_store, folder, ["x.mp3"])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.report.matches == {}
    assert result.report.unmatched[1].reason == "no_candidate"
    assert result.by_detail() == {"no_library_row": 1}


def test_several_rows_is_unmatched_never_guessed(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    add_track("a" * 40, file_path="/Users/old/Music/House/x.mp3")
    add_track("b" * 40, file_path="/Users/user/Music/House/x.mp3")
    folder = _batch(tmp_path, [f"{ROOT}/Music/House/x.mp3"])
    _store, songs = _songs(make_mik_store, folder, ["x.mp3"])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.report.matches == {}
    assert result.report.unmatched[1].reason == "ambiguous_candidates"
    assert result.by_detail() == {"several_library_rows": 1}
    assert "2 rows" in result.report.unmatched[1].detail


def test_zero_or_several_source_lines_are_unmatched(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    add_track("a" * 40, file_path="/Users/old/Music/House/x.mp3")
    add_track("b" * 40, file_path="/Users/old/Music/Techno/x.mp3")
    folder = _batch(tmp_path, [f"{ROOT}/Music/House/x.mp3", f"{ROOT}/Music/Techno/x.mp3"])
    _store, songs = _songs(make_mik_store, folder, ["x.mp3", "stray.mp3"])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.report.matches == {}
    assert result.by_detail() == {"several_source_lines": 1, "no_source_line": 1}
    assert result.report.unmatched[1].reason == "ambiguous_candidates"
    assert result.report.unmatched[2].reason == "no_candidate"


def test_source_outside_the_library_root_is_unmatched(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    add_track("a" * 40, file_path="/Users/old/Music/House/x.mp3")
    folder = _batch(tmp_path, ["/Volumes/Other/Music/House/x.mp3"])
    _store, songs = _songs(make_mik_store, folder, ["x.mp3"])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.by_detail() == {"source_outside_library_root": 1}


def test_source_equal_to_the_stored_path_resolves_without_the_root(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    """A row already stored under its on-disk path needs no root rewrite."""
    add_track("a" * 40, file_path="/Volumes/Other/Music/House/x.mp3")
    folder = _batch(tmp_path, ["/Volumes/Other/Music/House/x.mp3"])
    _store, songs = _songs(make_mik_store, folder, ["x.mp3"])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.report.matches[1].stable_id == "a" * 40


def test_nfc_normalizes_both_sides(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    composed = unicodedata.normalize("NFC", "Zölbrin.mp3")
    decomposed = unicodedata.normalize("NFD", "Zölbrin.mp3")
    assert composed != decomposed
    add_track("a" * 40, file_path=f"/Users/old/Music/{composed}")
    folder = _batch(tmp_path, [f"{ROOT}/Music/{decomposed}"])
    _store, songs = _songs(make_mik_store, folder, [decomposed])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.report.matches[1].stable_id == "a" * 40


def test_songs_outside_the_batch_folder_are_ignored_and_counted(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    add_track("a" * 40, file_path="/Users/old/Music/House/x.mp3")
    folder = _batch(tmp_path, [f"{ROOT}/Music/House/x.mp3"])
    sibling = f"{folder}-other/x.mp3"
    _store, songs = _songs(
        make_mik_store,
        folder,
        ["x.mp3"],
        extra=[{"path": "/Users/old/tones/sine.wav"}, {"path": sibling}],
    )
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert [song.row_id for song in result.songs] == [1]
    assert result.ignored_outside_batch == 2
    assert set(result.report.matches) == {1}


def test_a_deleted_row_is_not_a_candidate(
    tmp_path: Path, state_conn: sqlite3.Connection, add_track, make_mik_store
) -> None:
    add_track("a" * 40, file_path="/Users/old/Music/House/x.mp3")
    state_conn.execute("UPDATE tracks SET deleted_at = '2026-01-01T00:00:00+00:00'")
    folder = _batch(tmp_path, [f"{ROOT}/Music/House/x.mp3"])
    _store, songs = _songs(make_mik_store, folder, ["x.mp3"])
    result = sources.match_songs_by_sources(
        songs, state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux"
    )
    assert result.by_detail() == {"no_library_row": 1}


def test_a_missing_sources_file_is_an_explicit_error(
    tmp_path: Path, state_conn: sqlite3.Connection
) -> None:
    folder = tmp_path / "empty-batch"
    folder.mkdir()
    with pytest.raises(loader.LoadError, match=r"\.sources\.txt"):
        sources.match_songs_by_sources([], state_conn, batch_dir=folder, library_root=Path(ROOT), platform="linux")


def test_load_cli_writes_a_fresh_batch_through_the_sources_map(
    tmp_path: Path,
    data_dir: Path,
    state_conn: sqlite3.Connection,
    add_track,
    make_mik_store,
    write_verdicts,
    capsys,
) -> None:
    """The three tiers stage this copy batch; the sources map writes it."""
    add_track("a" * 40, file_path="/Users/old/Music/House/x.mp3")
    add_track("b" * 40, file_path="/Users/old/Music/Other/x.mp3")
    state_conn.commit()
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    folder = _batch(tmp_path, [f"{ROOT}/Music/House/x.mp3"])
    store, _songs_read = _songs(
        make_mik_store, folder, ["x.mp3"], extra=[{"path": "/Users/old/tones/sine.wav"}]
    )
    base = ["load", "--json", "--fields", "energy,energy_segments", "--data-dir", str(data_dir)]
    base += ["--store", str(store)]
    assert cli.main(base) == 0
    tiers = json.loads(capsys.readouterr().out)
    assert tiers["planned"]["field_writes"] == 0  # basename is ambiguous across two rows
    argv = [*base, "--live", "--sources-batch", str(folder), "--library-root", ROOT]
    assert cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources_batch"]["resolved"] == 1
    assert payload["sources_batch"]["ignored_outside_batch"] == 1
    assert payload["applied"]["fields_written"] == 1
    assert payload["applied"]["staged_written"] == 0
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        rows = conn.execute(
            "SELECT stable_id, field_name, value_json FROM track_fields WHERE source = 'mik'"
        ).fetchall()
        series = conn.execute("SELECT stable_id FROM track_energy_segments").fetchall()
    finally:
        conn.close()
    assert rows == [("a" * 40, "energy", "4")]
    assert series == [("a" * 40,)]


def test_load_cli_needs_both_sources_flags(data_dir: Path, make_mik_store) -> None:
    store = make_mik_store([{"path": "/Users/old/x.mp3"}])
    with pytest.raises(SystemExit):
        cli.main(["load", "--data-dir", str(data_dir), "--store", str(store), "--sources-batch", BATCH])


def test_source_lines_keep_spaces_that_are_part_of_a_file_name(tmp_path: Path) -> None:
    """if a source path whose name starts or ends with a space is trimmed (then it can never bind) then broken."""
    lines = ["/Users/old/Music/ends-in-a-space.mp3 ", "/Users/old/Music/ends-in-a-tab.mp3\t", "/Users/old/Music/plain.mp3"]
    (tmp_path / sources.SOURCES_FILE).write_text("\n".join(lines) + "\n\n   \n", encoding="utf-8")
    assert sources.read_source_lines(tmp_path) == lines  # blank and all-space lines are skipped
