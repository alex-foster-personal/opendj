"""Relinking a moved track by its audio: the fingerprint signal in locate.

[if] the duplicate scan recorded a track's fingerprint [then] locate finds and checks its moved file by audio, [else stop].

The dedup database here is a real one (``FingerprintCache``) holding real
chromaprint strings (``tests/fingerprint_fakes.py``), and nothing here decodes
a candidate: every check below is answered from recorded fingerprints alone.
Checks that decode a candidate file (match, veto, unmeasurable) run on the
real engine with real audio in ``tests/dedup/test_engine_fingerprint_real.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.reconcile import fingerprint_evidence as fe
from apps.reconcile import locate
from apps.shared import audio_files
from apps.shared.fingerprints import Fingerprint, FingerprintCache
from tests.fingerprint_fakes import fake_fingerprint

pytestmark = pytest.mark.requirement("RECON-06")

SONG = fake_fingerprint(b"song", b"original")
OTHER_SONG = fake_fingerprint(b"other", b"x")


def _fp(path: Path, fp_str: str) -> Fingerprint:
    return Fingerprint(path=path, duration=200.0, fp_str=fp_str, size=1000, mtime=0.0)


def _db(tmp_path: Path, rows: list[tuple[Path, str, str | None]]) -> Path:
    db = tmp_path / "dedup.sqlite"
    cache = FingerprintCache(db)
    for path, fp_str, sid in rows:
        cache.put(_fp(path, fp_str), stable_id=sid)
    return db


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 1000)
    return path


def _af(path: Path) -> audio_files.AudioFile:
    return audio_files.AudioFile(path=path, size_bytes=1000, mtime=0.0, ext=path.suffix.lower())


def _row(original: Path, **extra: str) -> dict[str, str]:
    row = {
        "id": "1", "title": "Song", "artist": "Artist", "original_path": str(original),
        "basename": original.name, "duration_s": "200", "file_size": "1000",
    }
    row.update(extra)
    return row


def test_a_renamed_move_is_found_by_its_audio(tmp_path: Path) -> None:
    """[if] the scan saw the audio under a new name [then] it is a fingerprint_match candidate, [else stop]."""
    original = tmp_path / "old" / "track.mp3"  # gone
    moved = _file(tmp_path / "new" / "Artist - Song (renamed).mp3")
    unrelated = _file(tmp_path / "new" / "something else.mp3")
    db = _db(tmp_path, [
        (original, SONG, "sid-1"),
        (moved, fake_fingerprint(b"song", b"re-encode"), None),
        (unrelated, OTHER_SONG, None),
    ])
    ev = fe.FingerprintEvidence.open(db)
    assert ev is not None
    idx = locate.FsIndex.build([_af(moved), _af(unrelated)])

    cands = locate.find_candidates(_row(original), idx, {moved: None}, fingerprints=ev)

    assert [c.path for c in cands] == [moved]
    assert "fingerprint_match" in cands[0].signals
    assert "basename_exact" not in cands[0].signals
    # The control that can say no: without the evidence nothing seeds it.
    assert locate.find_candidates(_row(original), idx, {moved: None}) == []


def test_the_recorded_fingerprint_is_found_by_stable_id(tmp_path: Path) -> None:
    """[if] the row names the track's stable_id [then] its recording is used whatever the path, [else stop]."""
    moved = _file(tmp_path / "new" / "renamed.mp3")
    db = _db(tmp_path, [
        (tmp_path / "somewhere" / "else.mp3", SONG, "sid-1"),
        (moved, SONG, None),
    ])
    ev = fe.FingerprintEvidence.open(db)
    assert ev is not None
    row = _row(tmp_path / "old" / "track.mp3", stable_id="sid-1")
    idx = locate.FsIndex.build([_af(moved)])
    assert [c.path for c in locate.find_candidates(row, idx, {moved: None}, fingerprints=ev)] == [moved]
    assert locate.find_candidates(_row(tmp_path / "old" / "track.mp3"), idx, {moved: None}, fingerprints=ev) == []


def test_a_file_another_track_owns_is_not_offered(tmp_path: Path) -> None:
    """[if] the matching file belongs to another library track [then] it is not a candidate, [else stop]."""
    original = tmp_path / "old" / "track.mp3"
    dup = _file(tmp_path / "lib" / "copy.mp3")
    db = _db(tmp_path, [(original, SONG, "sid-1"), (dup, SONG, "sid-2")])
    ev = fe.FingerprintEvidence.open(db)
    assert ev is not None
    idx = locate.FsIndex.build([_af(dup)])
    assert locate.find_candidates(_row(original, stable_id="sid-1"), idx, {dup: None}, fingerprints=ev) == []

    # Control: the same file recorded under THIS track's id is offered.
    db2 = _db(tmp_path / "b", [(original, SONG, "sid-1"), (dup, SONG, "sid-1")])
    ev2 = fe.FingerprintEvidence.open(db2)
    assert ev2 is not None
    cands = locate.find_candidates(_row(original, stable_id="sid-1"), idx, {dup: None}, fingerprints=ev2)
    assert [c.path for c in cands] == [dup]


def test_no_scan_database_means_no_evidence(tmp_path: Path) -> None:
    """[if] no duplicate scan ever ran [then] there is no evidence object, [else stop]."""
    assert fe.FingerprintEvidence.open(tmp_path / "missing.sqlite") is None
