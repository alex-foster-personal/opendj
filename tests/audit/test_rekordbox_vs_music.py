"""Tests for :mod:`apps.audit.rekordbox_vs_music`.

The module is a pure-reporting CLI that compares Rekordbox master.db
rows against on-disk audio files and emits three CSVs. We cover:

* ``_classify`` bucketing (streaming / empty / ok / missing)
* ``_category`` single-row classifier
* CSV writers (header + row shape)
* ``main()`` end-to-end with paths + scanners + DB opener all
  monkey-patched so no live-DB or filesystem scan runs.

Requirement: RECON (audit surface feeds reconcile).
"""
from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.audit import rekordbox_vs_music as rvm
from apps.shared import audio_files as _audio_files


pytestmark = pytest.mark.requirement("RECON-01")


# --------------------------------------------------------------------------- #
# Helpers: build RBTrack stand-ins without touching the real dataclass schema.
# --------------------------------------------------------------------------- #


def _mk_track(
    *,
    id: str = "1",
    title: str = "t",
    artist: str = "a",
    album: str = "al",
    folder_path: str = "",
    file_path: Path | None = None,
    is_streaming: bool = False,
    file_size: int | None = None,
):
    """Build a lightweight stand-in that satisfies attribute access."""
    return SimpleNamespace(
        id=id,
        title=title,
        artist=artist,
        album=album,
        folder_path=folder_path,
        file_path=file_path,
        is_streaming=is_streaming,
        file_size=file_size,
    )


# --------------------------------------------------------------------------- #
# _classify / _category
# --------------------------------------------------------------------------- #


def test_classify_streaming_row(tmp_path: Path) -> None:
    """Streaming rows with a folder_path land in the streaming bucket."""
    t = _mk_track(folder_path="spotify:track:abc", is_streaming=True)
    buckets = rvm._classify([t])
    assert buckets.streaming == [t]
    assert buckets.empty_path == []
    assert buckets.file_linked_ok == []
    assert buckets.file_linked_missing == []


def test_classify_empty_path_row() -> None:
    """Empty FolderPath → empty_path bucket regardless of streaming flag."""
    t = _mk_track(folder_path="", is_streaming=True)
    buckets = rvm._classify([t])
    # Empty path takes precedence over the streaming check.
    assert buckets.empty_path == [t]
    assert buckets.streaming == []


def test_classify_file_linked_ok_and_missing(tmp_path: Path) -> None:
    real = tmp_path / "exists.mp3"
    real.write_bytes(b"stub")
    gone = tmp_path / "gone.mp3"
    t_ok = _mk_track(id="1", folder_path=str(real), file_path=real)
    t_bad = _mk_track(id="2", folder_path=str(gone), file_path=gone)
    buckets = rvm._classify([t_ok, t_bad])
    assert [t.id for t in buckets.file_linked_ok] == ["1"]
    assert [t.id for t in buckets.file_linked_missing] == ["2"]
    assert buckets.total == 2


def test_classify_missing_when_file_path_is_none() -> None:
    """A non-streaming row with no file_path is still classified as missing."""
    t = _mk_track(folder_path="/nope.mp3", file_path=None)
    buckets = rvm._classify([t])
    assert buckets.file_linked_missing == [t]


def test_category_returns_expected_labels(tmp_path: Path) -> None:
    real = tmp_path / "exists.mp3"
    real.write_bytes(b"stub")
    assert rvm._category(_mk_track(folder_path="")) == "empty_path"
    assert rvm._category(
        _mk_track(folder_path="spotify:track", is_streaming=True)
    ) == "streaming"
    assert rvm._category(
        _mk_track(folder_path=str(real), file_path=real)
    ) == "file_ok"
    assert rvm._category(
        _mk_track(folder_path="/nope", file_path=tmp_path / "nope.mp3")
    ) == "file_missing"


# --------------------------------------------------------------------------- #
# CSV writers
# --------------------------------------------------------------------------- #


def test_write_rb_missing_csv_has_expected_columns(tmp_path: Path) -> None:
    out = tmp_path / "rb_missing.csv"
    rows = [_mk_track(id="7", title="T", artist="A", album="Al",
                      folder_path="/x.mp3")]
    rvm._write_rb_missing_csv(out, rows)
    with out.open() as fh:
        reader = csv.reader(fh)
        header = next(reader)
        body = list(reader)
    assert header == ["id", "title", "artist", "album", "folder_path"]
    assert body == [["7", "T", "A", "Al", "/x.mp3"]]


def test_write_fs_missing_csv_has_expected_shape(tmp_path: Path) -> None:
    out = tmp_path / "fs_missing.csv"
    f = _audio_files.AudioFile(
        path=tmp_path / "orphan.mp3", size_bytes=1234, mtime=1_700_000_000.0,
        ext=".mp3",
    )
    rvm._write_fs_missing_csv(out, [f])
    with out.open() as fh:
        header, row = list(csv.reader(fh))
    assert header == ["path", "size_bytes", "mtime"]
    # mtime is formatted with no decimal places.
    assert row == [str(f.path), "1234", "1700000000"]


def test_write_rb_all_csv_includes_category_column(tmp_path: Path) -> None:
    out = tmp_path / "rb_all.csv"
    real = tmp_path / "a.mp3"
    real.write_bytes(b"x")
    rows = [
        _mk_track(id="1", folder_path=str(real), file_path=real, file_size=99),
        _mk_track(id="2", folder_path="", file_path=None),
    ]
    rvm._write_rb_all_csv(out, rows)
    with out.open() as fh:
        reader = csv.reader(fh)
        header = next(reader)
        body = list(reader)
    assert header[:2] == ["id", "category"]
    # Row 1 → file_ok with size 99; row 2 → empty_path with blank size.
    assert body[0][0] == "1"
    assert body[0][1] == "file_ok"
    assert body[0][-1] == "99"
    assert body[1][0] == "2"
    assert body[1][1] == "empty_path"
    assert body[1][-1] == ""


def test_print_summary_renders_all_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """``_print_summary`` must print each bucket label at least once."""
    buckets = rvm.Buckets(
        streaming=[_mk_track(id="s")],
        empty_path=[_mk_track(id="e")],
        file_linked_ok=[_mk_track(id="ok")],
        file_linked_missing=[_mk_track(id="bad")],
    )
    rvm._print_summary(buckets, fs_total=42, fs_not_in_rb=7)
    captured = capsys.readouterr().out
    assert "Rekordbox tracks total" in captured
    assert "file-linked (OK)" in captured
    assert "file-linked (MISSING)" in captured
    assert "streaming" in captured
    assert "empty path" in captured
    assert "42" in captured
    assert "7" in captured


# --------------------------------------------------------------------------- #
# main() end-to-end with stubbed DB + scanner
# --------------------------------------------------------------------------- #


def test_main_writes_three_csvs_and_prints_missing_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main()`` wires the pipeline end-to-end.

    We stub every external touchpoint: ``paths.copy_live_dbs`` (no live
    DBs), ``rekordbox_db.open_db`` + ``iter_tracks`` (synthetic rows),
    and ``audio_files.scan_music_files`` (synthetic FS). Then we verify
    the three CSVs land in a tmpdir with expected headers.
    """
    # Redirect DATA_DIR so the CSVs land in tmp_path.
    monkeypatch.setattr(rvm.paths, "DATA_DIR", tmp_path, raising=True)
    monkeypatch.setattr(rvm.paths, "MUSIC_ROOTS", [tmp_path], raising=False)
    monkeypatch.setattr(rvm.paths, "copy_live_dbs", lambda: {"rekordbox": None, "djay": None})

    # Two synthetic RB rows: one OK (on-disk), one missing.
    real = tmp_path / "exists.mp3"
    real.write_bytes(b"stub")
    gone = tmp_path / "gone.mp3"
    rb_rows = [
        _mk_track(id="1", title="t1", artist="a", album="al",
                  folder_path=str(real), file_path=real),
        _mk_track(id="2", title="t2", artist="a", album="al",
                  folder_path=str(gone), file_path=gone),
    ]
    monkeypatch.setattr(rvm.rekordbox_db, "open_db", lambda: object())
    monkeypatch.setattr(rvm.rekordbox_db, "iter_tracks", lambda db: iter(rb_rows))

    # Synthetic FS: the OK file plus one orphan not linked by RB.
    orphan = tmp_path / "orphan.mp3"
    orphan.write_bytes(b"stub")
    fs_rows = [
        _audio_files.AudioFile(path=real, size_bytes=4, mtime=1_700_000_000.0, ext=".mp3"),
        _audio_files.AudioFile(path=orphan, size_bytes=4, mtime=1_700_000_000.0, ext=".mp3"),
    ]
    monkeypatch.setattr(rvm.audio_files, "scan_music_files", lambda: iter(fs_rows))

    rvm.main()

    assert (tmp_path / "rb_missing_files.csv").is_file()
    assert (tmp_path / "fs_files_not_in_rb.csv").is_file()
    assert (tmp_path / "rb_all_tracks.csv").is_file()

    # rb_missing_files.csv should list the one missing row.
    with (tmp_path / "rb_missing_files.csv").open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert [r["id"] for r in rows] == ["2"]

    # fs_files_not_in_rb.csv should list the orphan only.
    with (tmp_path / "fs_files_not_in_rb.csv").open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["path"].endswith("orphan.mp3")

    # rb_all_tracks.csv carries both rows and their categories.
    with (tmp_path / "rb_all_tracks.csv").open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    cats = {r["id"]: r["category"] for r in rows}
    assert cats == {"1": "file_ok", "2": "file_missing"}

    stdout = capsys.readouterr().out
    assert "Step 1" in stdout
    assert "First 10 RB entries with missing files" in stdout
    assert "First 10 filesystem files not in Rekordbox" in stdout


def test_main_handles_resolve_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``Path.resolve`` raises OSError for an OK row, fallback is used.

    Covers the ``except OSError`` branches in ``main()``.
    """
    monkeypatch.setattr(rvm.paths, "DATA_DIR", tmp_path, raising=True)
    monkeypatch.setattr(rvm.paths, "MUSIC_ROOTS", [tmp_path], raising=False)
    monkeypatch.setattr(rvm.paths, "copy_live_dbs", lambda: {})

    # Path subclass whose .resolve() explodes.
    class _BoomPath(type(Path())):  # type: ignore[misc]
        def resolve(self, strict: bool = False):  # type: ignore[override]
            raise OSError("no")
        def exists(self) -> bool:  # type: ignore[override]
            return True

    real = _BoomPath(tmp_path / "boom.mp3")
    real.write_bytes(b"x")
    rb_rows = [_mk_track(id="1", folder_path=str(real), file_path=real)]
    monkeypatch.setattr(rvm.rekordbox_db, "open_db", lambda: object())
    monkeypatch.setattr(rvm.rekordbox_db, "iter_tracks", lambda db: iter(rb_rows))

    fs_rows = [
        _audio_files.AudioFile(path=real, size_bytes=1, mtime=1.0, ext=".mp3"),
    ]
    monkeypatch.setattr(rvm.audio_files, "scan_music_files", lambda: iter(fs_rows))

    # Shouldn't raise — falls back to the un-resolved path in both sets.
    rvm.main()
    assert (tmp_path / "rb_missing_files.csv").is_file()
