"""Folder import records audio-hash duplicates into the dedup cluster tables.

[if] folder ingest sees another path's audio hash [then] dedup tables get a cluster, [else stop].
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.dedup import schema as dedup_schema
from apps.dedup.http import HashDuplicateRecorder, open_hash_duplicate_recorder
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("SETPREP-10626")

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "phase7-dedup" / "src-v2.mp3"


@pytest.fixture
def dedup_db(tmp_path: Path) -> Path:
    path = tmp_path / "dedup" / "phase7.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def open_recorder(dedup_db: Path) -> Callable[[], HashDuplicateRecorder]:
    """The opener a caller above both packages injects (folder ingest never imports apps.dedup)."""
    return lambda: open_hash_duplicate_recorder(dedup_db)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path))
    return tmp_path


def test_folder_import_writes_an_audio_hash_duplicate_cluster(
    state_conn, data_dir: Path, dedup_db: Path, open_recorder: Callable[[], HashDuplicateRecorder]
) -> None:
    """[if] a second path carries the same audio payload [then] dedup tables get one audio_hash cluster, [else stop]."""
    assert FIXTURE.is_file()
    root = data_dir / "music"
    root.mkdir()
    first = root / "a" / FIXTURE.name
    second = root / "b" / FIXTURE.name
    first.parent.mkdir()
    second.parent.mkdir()
    shutil.copy2(FIXTURE, first)
    shutil.copy2(FIXTURE, second)

    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="test")
    first_report = folder.ingest_folder(writer, [first.parent], dry_run=False, open_duplicate_recorder=open_recorder)
    assert first_report.content_duplicates == 0
    second_report = folder.ingest_folder(writer, [second.parent], dry_run=False, open_duplicate_recorder=open_recorder)
    assert second_report.content_duplicates == 1

    conn = dedup_schema.ensure_schema(dedup_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM duplicate_clusters").fetchone()[0] == 1
        assert conn.execute("SELECT rationale FROM duplicate_clusters").fetchone()[0] == "audio_hash"
        assert conn.execute("SELECT COUNT(*) FROM track_aliases").fetchone()[0] == 1
    finally:
        conn.close()

    repeat = folder.ingest_folder(writer, [second.parent], dry_run=False, open_duplicate_recorder=open_recorder)
    assert repeat.content_duplicates == 0
    conn = dedup_schema.ensure_schema(dedup_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM track_aliases").fetchone()[0] == 1
    finally:
        conn.close()


def test_folder_import_logs_when_audio_hash_missing(
    state_conn,
    data_dir: Path,
    open_recorder: Callable[[], HashDuplicateRecorder],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] dedup is on but hashing fails [then] the ingest log names the skip reason, [else stop]."""
    assert FIXTURE.is_file()
    root = data_dir / "music"
    root.mkdir()
    track = root / FIXTURE.name
    shutil.copy2(FIXTURE, track)
    monkeypatch.setattr(
        "apps.shared.state.ingest.folder.hashing.sha256_audio_payload",
        lambda _path: "",
    )

    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="test")
    with caplog.at_level(logging.INFO, logger="apps.shared.state.ingest.folder"):
        folder.ingest_folder(writer, [root], dry_run=False, open_duplicate_recorder=open_recorder)

    needle = "content duplicate check skipped"
    assert any(
        needle in rec.message and str(track) in rec.message and "no audio hash" in rec.message for rec in caplog.records
    )


def test_folder_import_without_a_recorder_records_nothing(state_conn, data_dir: Path, dedup_db: Path) -> None:
    """[if] no recorder is injected [then] ingest counts no duplicates and opens no dedup DB, [else stop]."""
    root = data_dir / "music"
    for sub in ("a", "b"):
        (root / sub).mkdir(parents=True)
        shutil.copy2(FIXTURE, root / sub / FIXTURE.name)

    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="test")
    report = folder.ingest_folder(writer, [root], dry_run=False)
    assert report.tracks_inserted == 2
    assert report.content_duplicates == 0
    assert not dedup_db.exists()


def test_dry_run_never_opens_the_recorder(state_conn, data_dir: Path) -> None:
    """[if] a dry run gets a recorder opener [then] the opener is never called, [else stop]."""
    root = data_dir / "music"
    root.mkdir()
    shutil.copy2(FIXTURE, root / FIXTURE.name)

    def _refuse() -> HashDuplicateRecorder:
        raise AssertionError("dry run opened the duplicate recorder")

    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="test")
    report = folder.ingest_folder(writer, [root], dry_run=True, open_duplicate_recorder=_refuse)
    assert report.content_duplicates == 0
