"""Folder import rejects corrupt and non-audio files before row insert."""
from __future__ import annotations

import os
import struct
import wave
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.ingest import folder
from apps.shared.state.writer import StateWriter


def _write_wav(path: Path, seconds: float = 0.05) -> Path:
    """A real, playable wav. Not a stub: mutagen has to be able to read it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


def _build_corrupt_fixture_dir(root: Path) -> None:
    _write_wav(root / "control-valid.wav")

    (root / "zero-bytes.wav").write_bytes(b"")

    header_only = root / "header-only.wav"
    with wave.open(str(header_only), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)

    truncated = root / "truncated.wav"
    _write_wav(truncated, seconds=1.0)
    truncated.write_bytes(truncated.read_bytes()[:5000])

    (root / "actually-text.wav").write_text(
        "This is prose, not audio. " * 100, encoding="utf-8"
    )

    (root / "riff-then-garbage.wav").write_bytes(b"RIFF" + os.urandom(4000))

    corrupt_fmt = root / "corrupt-fmt.wav"
    _write_wav(corrupt_fmt, seconds=0.05)
    raw = bytearray(corrupt_fmt.read_bytes())
    fmt_idx = raw.find(b"fmt ")
    if fmt_idx >= 0:
        for index in range(fmt_idx + 8, min(fmt_idx + 24, len(raw))):
            raw[index] ^= 0xFF
    corrupt_fmt.write_bytes(raw)


def _ingest(root: Path, state_path: Path) -> folder.FolderIngestReport:
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test-unplayable")
    try:
        return folder.ingest_folder(writer, [root], dry_run=False)
    finally:
        writer.close()
        conn.close()


def _track_count(state_path: Path) -> int:
    conn = state_db.open_rw(state_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()


def test_folder_import_rejects_corrupt_wav_fixtures(tmp_path: Path) -> None:
    """[if] corrupt wav fixtures [then] no rows [else stop]."""
    root = tmp_path / "fixtures"
    root.mkdir()
    _build_corrupt_fixture_dir(root)
    state_path = tmp_path / "state.db"

    report = _ingest(root, state_path)

    assert _track_count(state_path) == 1
    assert report.tracks_inserted == 1
    assert report.files_seen == 7
    assert report.files_rejected_unplayable == 6
    assert report.files_without_tags == 1


def test_valid_untagged_wav_imports_without_rejection(tmp_path: Path) -> None:
    """[if] valid untagged wav [then] imports with files_without_tags only [else stop]."""
    root = tmp_path / "music"
    root.mkdir()
    _write_wav(root / "untagged.wav")
    state_path = tmp_path / "state.db"

    report = _ingest(root, state_path)

    assert _track_count(state_path) == 1
    assert report.files_rejected_unplayable == 0
    assert report.files_without_tags == 1
    assert report.tracks_inserted == 1


@pytest.mark.requires_mutagen
def test_folder_import_rejects_corrupt_wav_with_mutagen_installed(
    tmp_path: Path,
) -> None:
    """[if] mutagen present [then] corrupt fixtures still rejected [else stop]."""
    root = tmp_path / "fixtures"
    root.mkdir()
    _build_corrupt_fixture_dir(root)
    state_path = tmp_path / "state.db"

    report = _ingest(root, state_path)

    assert _track_count(state_path) == 1
    assert report.files_rejected_unplayable == 6
    assert report.files_without_tags == 1
