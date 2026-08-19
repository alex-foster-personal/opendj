"""Detection: what it reports, and what it must not have touched.

The wizard's second step tells an operator "here is what I found". If that
step opened, copied or migrated anything, the sentence is a lie -- so the
tests assert the absence of side effects as hard as they assert the values.

Single-line intent:
  - if detection mutates the thing it detected then the step is not a report
  - if a missing rekordbox and an unreachable key collapse into one answer
    then the operator is sent to the wrong fix
  - if the plaintext test guesses from the filename then an encrypted file
    named master.plain.db silently reaches the ingest
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.adapters.rekordbox.config import MASTER_PLAIN_DB
from apps.engine_core.setup import detect

FIXTURE_RB: Path = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "rekordbox"
    / "master.plain.db"
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A scratch data dir. The real one is never written by a test."""
    target = tmp_path / "data"
    target.mkdir()
    return target


def _with_plain_copy(data_dir: Path) -> Path:
    destination = data_dir / detect.PLAIN_COPY_NAME
    shutil.copy2(FIXTURE_RB, destination)
    return destination


# ----- names -------------------------------------------------------------
def test_the_plain_copy_name_is_the_same_one_everywhere() -> None:
    """Three modules name master.plain.db. A wizard that reads a different
    file from the one the adapters write is a whole class of bug, so the
    agreement is a test rather than a convention."""
    names = {detect.PLAIN_COPY_NAME, MASTER_PLAIN_DB.name}
    assert names == {"master.plain.db"}


# ----- plaintext detection ------------------------------------------------
def test_a_real_sqlite_file_reads_as_plaintext(data_dir: Path) -> None:
    assert detect.is_plain_sqlite(_with_plain_copy(data_dir)) is True


def test_a_file_without_the_magic_is_not_plaintext(data_dir: Path) -> None:
    """Named like a decrypted copy, encrypted underneath. Content wins."""
    liar = data_dir / detect.PLAIN_COPY_NAME
    liar.write_bytes(b"\x01\x02not a sqlite header at all" * 8)
    assert detect.is_plain_sqlite(liar) is False


def test_a_missing_file_is_not_plaintext(data_dir: Path) -> None:
    assert detect.is_plain_sqlite(data_dir / "nothing-here.db") is False


# ----- probes -------------------------------------------------------------
def test_probe_reports_a_missing_path_instead_of_raising(
    data_dir: Path,
) -> None:
    probe = detect.probe(data_dir / "absent.db")
    assert probe.exists is False
    assert probe.size_bytes is None
    assert probe.path.endswith("absent.db")


def test_probe_reports_size_and_mtime_for_a_real_file(data_dir: Path) -> None:
    probe = detect.probe(_with_plain_copy(data_dir))
    assert probe.exists is True
    assert probe.size_bytes == FIXTURE_RB.stat().st_size
    assert probe.modified_at is not None


# ----- source selection ---------------------------------------------------
def test_an_existing_plain_copy_is_preferred_over_the_live_db(
    data_dir: Path,
) -> None:
    plain = _with_plain_copy(data_dir)
    found = detect.detect_rekordbox(data_dir)
    assert found.import_source == str(plain)
    assert found.import_source_encrypted is False


def test_no_rekordbox_anywhere_is_its_own_blocker(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty machine names rekordbox_not_found, not a generic failure."""
    empty = tmp_path / "no-rekordbox-here"
    empty.mkdir()
    monkeypatch.setattr(
        "apps.shared.platform_paths.rekordbox_app_dir", lambda: empty
    )
    found = detect.detect_rekordbox(data_dir)
    assert found.installed is False
    assert found.import_source is None
    assert detect.CODE_REKORDBOX_NOT_FOUND in found.blockers


def test_a_missing_share_dir_is_reported_separately(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No share dir means no waveforms. It is not the same as no library."""
    _with_plain_copy(data_dir)
    monkeypatch.setattr(
        "apps.shared.platform_paths.compute_share_root",
        lambda: tmp_path / "share-that-does-not-exist",
    )
    found = detect.detect_rekordbox(data_dir)
    assert found.share_dir.exists is False
    assert detect.CODE_SHARE_MISSING in found.blockers
    assert detect.CODE_REKORDBOX_NOT_FOUND not in found.blockers


def test_an_encrypted_source_without_a_key_is_a_key_blocker(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = data_dir / detect.WORKING_COPY_NAME
    working.write_bytes(b"\x00ciphertext" * 32)
    monkeypatch.setattr(
        detect, "key_status", lambda: (False, "no key on this machine")
    )
    found = detect.detect_rekordbox(data_dir)
    assert found.import_source_encrypted is True
    assert detect.CODE_KEY_UNAVAILABLE in found.blockers


# ----- no side effects ----------------------------------------------------
def test_detection_does_not_touch_the_files_it_reports(
    data_dir: Path,
) -> None:
    """The whole promise of the detect step, asserted rather than assumed."""
    plain = _with_plain_copy(data_dir)
    before = plain.stat()
    contents_before = plain.read_bytes()

    detect.detect_rekordbox(data_dir)

    after = plain.stat()
    assert after.st_mtime == before.st_mtime
    assert after.st_size == before.st_size
    assert plain.read_bytes() == contents_before
    # Nothing was created alongside it either: no state dir, no snapshot.
    assert sorted(p.name for p in data_dir.iterdir()) == [
        detect.PLAIN_COPY_NAME
    ]


# ----- library counts -----------------------------------------------------
def test_a_missing_state_db_is_an_empty_library_not_an_error(
    data_dir: Path,
) -> None:
    counts = detect.library_counts(data_dir)
    assert counts.empty is True
    assert counts.tracks == 0
    assert counts.state_db.exists is False


def test_a_state_db_without_tables_still_counts_zero(data_dir: Path) -> None:
    """A half-created state.db must not blow up the status endpoint."""
    import sqlite3

    (data_dir / "state").mkdir()
    sqlite3.connect(data_dir / "state" / detect.STATE_DB_NAME).close()
    counts = detect.library_counts(data_dir)
    assert counts.tracks == 0
    assert counts.playlists == 0
    assert counts.state_db.exists is True


# ----- key probe ----------------------------------------------------------
def test_key_status_always_explains_itself() -> None:
    """True or false, the operator gets a reason they can act on."""
    available, detail = detect.key_status()
    assert isinstance(available, bool)
    assert detail.strip() != ""
