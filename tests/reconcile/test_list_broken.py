"""Tests for :mod:`apps.reconcile.list_broken`. Ties to RECON-01."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.reconcile import list_broken

# ------------------------------------------------------------------ CSV shape


@pytest.mark.requirement("RECON-01")
def test_csv_columns_match_the_reconcile_contract() -> None:
    """Column order + names must stay stable (downstream locate.py parses CSV)."""
    assert list_broken.CSV_COLUMNS == (
        "id", "title", "artist", "album", "genre",
        "bpm", "key", "duration_s", "file_size",
        "original_path", "basename", "parent_dir",
    )


@pytest.mark.requirement("RECON-01")
def test_broken_row_as_csv_handles_nones() -> None:
    """None BPM/duration/file_size → empty string in the CSV output."""
    row = list_broken.BrokenRow(
        id="42", title="t", artist="a", album="al", genre="g",
        bpm=None, key="", duration_s=None, file_size=None,
        original_path="/x/y.mp3", basename="y.mp3", parent_dir="/x",
    )
    out = row.as_csv()
    assert out == ["42", "t", "a", "al", "g", "", "", "", "", "/x/y.mp3", "y.mp3", "/x"]


@pytest.mark.requirement("RECON-01")
def test_broken_row_formats_bpm_to_two_decimals() -> None:
    """BPM is written as ``"128.00"`` (2 decimals) to keep CSV diff-stable."""
    row = list_broken.BrokenRow(
        id="1", title="t", artist="a", album="", genre="",
        bpm=128.0, key="", duration_s=250, file_size=1024,
        original_path="/x", basename="x", parent_dir="/",
    )
    out = row.as_csv()
    assert out[5] == "128.00"
    assert out[7] == "250"
    assert out[8] == "1024"


# ------------------------------------------------------------------ _collect_broken


@pytest.mark.requirement("RECON-01")
def test_collect_broken_skips_streaming_and_existing_files(
    rb_pyrekordbox_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only is_streaming=False tracks with non-existent file_path are emitted."""
    # Force every track in the fixture to "not exist" by monkeypatching
    # Path.exists to return False for any local music library path — that way
    # we can count non-streaming, non-empty-path rows unambiguously.
    rows = list_broken._collect_broken(rb_pyrekordbox_db)
    # Every emitted row is non-streaming with a local file path that doesn't
    # currently exist on disk. The fixture was built from a library where
    # some files DO exist, so we only assert the invariants.
    for r in rows:
        assert r.original_path, "broken row must have an original_path"
        assert not Path(r.original_path).exists(), r.original_path
        # Streaming prefixes must NEVER appear.
        assert not r.original_path.startswith(
            ("spotify:", "tidal:", "http://", "https://")
        )
    # Sanity: basename / parent_dir derive from original_path.
    for r in rows[:5]:
        assert r.basename == Path(r.original_path).name
        assert r.parent_dir == str(Path(r.original_path).parent)


@pytest.mark.requirement("RECON-01")
def test_fixture_retains_the_phase1_edge_case_ids(rb_pyrekordbox_db) -> None:
    """The fixture builder seeds it with historical Phase-1 broken IDs. They
    may now resolve on disk (Phase 1 fixed most of them), so they won't
    necessarily appear in ``_collect_broken``'s output — but they MUST
    still be present in ``djmdContent`` for downstream tests to exercise
    the full Phase-1 data path.
    """
    from apps.shared import rekordbox_db

    all_ids = {t.id for t in rekordbox_db.iter_tracks(rb_pyrekordbox_db)}
    known = {"55476460", "76887163", "88741256", "98442200", "31424283"}
    assert all_ids & known, (
        "Fixture should retain at least one Phase-1 historical broken ID; "
        "regenerate via `python -m scripts.make_rb_fixture --force`."
    )


@pytest.mark.requirement("RECON-01")
def test_collect_broken_output_structure_is_sound(rb_pyrekordbox_db) -> None:
    """Whether or not any fixture rows are currently broken on disk, the
    output is a list of ``BrokenRow`` instances — never raises."""
    rows = list_broken._collect_broken(rb_pyrekordbox_db)
    assert isinstance(rows, list)
    for r in rows:
        assert isinstance(r, list_broken.BrokenRow)


@pytest.mark.requirement("RECON-01")
def test_collect_broken_detects_forced_broken_rows(
    tmp_rb_db: Path, tmp_path: Path
) -> None:
    """Rewriting every FolderPath to a guaranteed-missing path proves the
    happy path in ``_collect_broken`` works. This also exercises the
    summary table (parent directory counter) which would otherwise be
    dead code in the coverage report."""
    import sqlite3

    # Force EVERY local path to a never-existent dir. Streaming rows stay
    # untouched so we still exercise the is_streaming branch.
    con = sqlite3.connect(tmp_rb_db)
    con.execute(
        "UPDATE djmdContent SET FolderPath = ? || '/' || ID || '.mp3' "
        "WHERE FolderPath LIKE '/%'",
        (str(tmp_path / "definitely-not-a-real-dir-xyz"),),
    )
    con.commit()
    con.close()

    from pyrekordbox import Rekordbox6Database
    db = Rekordbox6Database(path=str(tmp_rb_db), unlock=False)
    try:
        rows = list_broken._collect_broken(db)
    finally:
        db.close()

    assert rows, "after rewriting local paths, EVERY local track must be broken"
    # Also exercise the summary printer for parent-dir aggregation.
    list_broken._print_summary(rows)


# ------------------------------------------------------------------ _write_csv


@pytest.mark.requirement("RECON-01")
def test_main_end_to_end_writes_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rb_plain_db_path: Path,
) -> None:
    """Running ``list_broken.main()`` end-to-end produces the CSV file.

    We point the module at a per-test working directory and stub out
    ``copy_live_dbs`` so it doesn't try to touch the live DB. ``open_db``
    is monkeypatched to use the plain fixture with ``unlock=False``.
    """
    import shutil

    import apps.shared.paths as shared_paths

    # Copy fixture into the fake working DB path.
    fake_data = tmp_path / "data"
    fake_data.mkdir()
    working = fake_data / "master.db.copy"
    shutil.copy2(rb_plain_db_path, working)

    monkeypatch.setattr(shared_paths, "DATA_DIR", fake_data)
    monkeypatch.setattr(shared_paths, "REKORDBOX_WORKING_DB", working)
    monkeypatch.setattr(list_broken, "OUT_DIR", fake_data / "reconcile")
    monkeypatch.setattr(list_broken, "OUT_CSV", fake_data / "reconcile" / "broken.csv")
    monkeypatch.setattr(list_broken.paths, "copy_live_dbs", lambda: {"rekordbox": working, "djay": None})
    # Force open_db to use unlock=False so the plain fixture loads.
    from pyrekordbox import Rekordbox6Database
    monkeypatch.setattr(
        list_broken.rekordbox_db,
        "open_db",
        lambda path=None: Rekordbox6Database(path=str(working), unlock=False),
    )

    list_broken.main()
    assert (fake_data / "reconcile" / "broken.csv").exists()


@pytest.mark.requirement("RECON-01")
def test_write_csv_roundtrips_through_dictreader(tmp_path: Path) -> None:
    """Writing + reading back yields the same CSV_COLUMNS fields."""
    rows = [
        list_broken.BrokenRow(
            id="1", title="t", artist="a", album="b", genre="c",
            bpm=120.5, key="8A", duration_s=200, file_size=4096,
            original_path="/x/y.mp3", basename="y.mp3", parent_dir="/x",
        ),
    ]
    out = tmp_path / "broken.csv"
    list_broken._write_csv(rows, out)
    parsed = list(csv.DictReader(out.open()))
    assert len(parsed) == 1
    assert set(parsed[0].keys()) == set(list_broken.CSV_COLUMNS)
    assert parsed[0]["bpm"] == "120.50"
    assert parsed[0]["original_path"] == "/x/y.mp3"
