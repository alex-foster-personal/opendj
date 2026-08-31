"""The no-rekordbox path, and the two ways a folder lies about being empty.

A tester without rekordbox has to be able to reach a working library, and
the library they reach has to be honest that nothing in it is analysed.
Separately, macOS answers a permission-blocked directory listing with an
EMPTY listing, so "0 files" is a sentence this code is not allowed to say
without first proving it was allowed to look.

Single-line intent:
  - if a folder import writes a bpm or a key then a tag-only library is
    being passed off as an analysed one
  - if a denied folder reports 0 audio files then the operator is told their
    music is missing when it is only unreadable
  - if an evicted iCloud placeholder is opened then reading the library
    triggers a download of it
  - if the denied list is dropped from the outcome then a count taken behind
    a permission wall travels without its caveat
"""

from __future__ import annotations

import os
import struct
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.config import EngineConfig
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.setup import detect, importer, record
from apps.engine_core.setup.api import router
from apps.engine_core.setup.jobs import SetupPayloadError, build_argv
from apps.shared import fs_access
from apps.shared.state import db as state_db
from apps.shared.state.ingest import folder as folder_ingest
from apps.shared.state.writer import StateWriter
from tests.engine_core.conftest import build_identity
from tests.platform_capabilities import posix_permission_denial_supported

API = "/api/v1/setup"


_CAN_TEST_PERMISSION_DENIAL = posix_permission_denial_supported(
    os.name, getattr(os, "geteuid", None)
)


def _write_wav(path: Path, seconds: float = 0.1) -> Path:
    """A real, playable wav. Not a stub: mutagen has to be able to read it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    _write_wav(root / "one.wav")
    _write_wav(root / "nested" / "two.wav")
    _write_wav(root / "nested" / "three.wav")
    (root / "notes.txt").write_text("not audio", encoding="utf-8")
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


@pytest.fixture
def unreadable(tmp_path: Path) -> Iterator[Path]:
    """A directory this process genuinely cannot list.

    chmod 000 rather than a mocked PermissionError: the point of the probe is
    that it survives contact with a real refusal from the OS.
    """
    blocked = tmp_path / "blocked"
    _write_wav(blocked / "hidden.wav")
    blocked.chmod(0o000)
    try:
        yield blocked
    finally:
        blocked.chmod(0o755)


def _emit_sink() -> tuple[list[tuple[float, str]], object]:
    sink: list[tuple[float, str]] = []

    def emit(progress: float, message: str) -> None:
        sink.append((progress, message))

    return sink, emit


@pytest.fixture
def client(data_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    store = JobStore(
        tmp_path / "jobs.db", boot_id="boot-folder", owner_pid=os.getpid()
    )
    store.recover()
    app.state.engine_cfg = EngineConfig(data_dir=data_dir)
    app.state.jobs_store = store
    app.state.build_identity = build_identity("payload")
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        store.close()


# ----- the access probe ---------------------------------------------------


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_a_blocked_directory_reads_as_denied_not_as_empty(
    unreadable: Path,
) -> None:
    probe = fs_access.probe_readable(unreadable)
    assert probe.exists is True
    assert probe.readable is False
    assert probe.denied is True
    assert "refused" in probe.detail


def test_a_readable_directory_reads_as_readable(library: Path) -> None:
    probe = fs_access.probe_readable(library)
    assert (probe.exists, probe.readable, probe.denied) == (True, True, False)


def test_a_missing_directory_is_absent_not_denied(tmp_path: Path) -> None:
    """Three different problems, three different answers."""
    probe = fs_access.probe_readable(tmp_path / "nope")
    assert (probe.exists, probe.readable, probe.denied) == (False, False, False)


def test_a_file_where_a_directory_was_expected_says_so(tmp_path: Path) -> None:
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    probe = fs_access.probe_readable(target)
    assert probe.readable is False
    assert probe.denied is False
    assert "not a directory" in probe.detail


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_denied_roots_is_the_caveat_list(
    library: Path, unreadable: Path
) -> None:
    probes = fs_access.probe_all([library, unreadable])
    assert fs_access.denied_roots(probes) == [str(unreadable)]


def test_probe_all_drops_duplicates_and_keeps_order(library: Path) -> None:
    probes = fs_access.probe_all([library, library])
    assert [probe.path for probe in probes] == [str(library)]


# ----- the folder ingest --------------------------------------------------
def test_the_folder_ingest_writes_every_audio_file(
    library: Path, data_dir: Path
) -> None:
    conn = state_db.open_rw(data_dir / "state" / detect.STATE_DB_NAME)
    writer = StateWriter(conn, actor="test")
    try:
        report = folder_ingest.ingest_folder(writer, [library], dry_run=False)
    finally:
        writer.close()
        conn.close()
    assert report.files_seen == 3  # the .txt is not audio
    assert report.tracks_inserted == 3
    assert detect.library_counts(data_dir).tracks == 3


def test_the_folder_ingest_writes_no_analysed_field_and_says_so(
    library: Path, data_dir: Path
) -> None:
    """The honest part. A tag-only import must not look like an analysed one."""
    state_path = data_dir / "state" / detect.STATE_DB_NAME
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test")
    try:
        report = folder_ingest.ingest_folder(writer, [library], dry_run=False)
        analysed = conn.execute(
            "SELECT COUNT(*) FROM track_fields WHERE field_name IN "
            "('bpm', 'key', 'rating')"
        ).fetchone()[0]
    finally:
        writer.close()
        conn.close()
    assert analysed == 0
    assert report.tracks_without_analysis == report.tracks_inserted


def test_a_dry_run_writes_nothing(library: Path, data_dir: Path) -> None:
    conn = state_db.open_rw(data_dir / "state" / detect.STATE_DB_NAME)
    writer = StateWriter(conn, actor="test")
    try:
        report = folder_ingest.ingest_folder(writer, [library], dry_run=True)
    finally:
        writer.close()
        conn.close()
    assert report.files_seen == 3
    assert detect.library_counts(data_dir).tracks == 0


def test_a_second_run_over_the_same_folder_changes_nothing(
    library: Path, data_dir: Path
) -> None:
    state_path = data_dir / "state" / detect.STATE_DB_NAME
    for _ in range(2):
        conn = state_db.open_rw(state_path)
        writer = StateWriter(conn, actor="test")
        try:
            folder_ingest.ingest_folder(writer, [library], dry_run=False)
        finally:
            writer.close()
            conn.close()
    assert detect.library_counts(data_dir).tracks == 3


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_an_unreadable_root_is_reported_beside_the_count(
    library: Path, unreadable: Path, data_dir: Path
) -> None:
    """HONEST DENOMINATORS: the count and its caveat travel together."""
    conn = state_db.open_rw(data_dir / "state" / detect.STATE_DB_NAME)
    writer = StateWriter(conn, actor="test")
    try:
        report = folder_ingest.ingest_folder(
            writer, [library, unreadable], dry_run=False
        )
    finally:
        writer.close()
        conn.close()
    assert report.files_seen == 3
    assert report.unreadable_roots == [str(unreadable)]


def test_limit_caps_the_walk(library: Path, data_dir: Path) -> None:
    conn = state_db.open_rw(data_dir / "state" / detect.STATE_DB_NAME)
    writer = StateWriter(conn, actor="test")
    try:
        report = folder_ingest.ingest_folder(
            writer, [library], dry_run=False, limit=2
        )
    finally:
        writer.close()
        conn.close()
    assert report.files_seen == 3  # what is there
    assert report.tracks_inserted == 2  # what was taken


# ----- the importer -------------------------------------------------------
def test_run_folder_import_reports_progress_and_records_the_outcome(
    library: Path, data_dir: Path
) -> None:
    sink, emit = _emit_sink()
    outcome = importer.run_folder_import(
        data_dir, emit=emit, roots=[library]
    )
    assert outcome.tracks_written == 3
    assert outcome.tracks_without_analysis == 3
    assert outcome.analysis_available is False
    progresses = [progress for progress, _ in sink]
    assert progresses == sorted(progresses)
    assert progresses[-1] == pytest.approx(1.0)

    saved = record.read(data_dir)
    assert saved.last_import is not None
    assert saved.last_import["kind"] == "folder"
    assert saved.last_import["tracks_without_analysis"] == 3


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_a_wholly_denied_import_refuses_with_the_access_code(
    unreadable: Path, data_dir: Path
) -> None:
    _sink, emit = _emit_sink()
    with pytest.raises(importer.SetupImportError) as caught:
        importer.run_folder_import(data_dir, emit=emit, roots=[unreadable])
    assert caught.value.code == detect.CODE_ACCESS_DENIED
    assert "System Settings" in str(caught.value)


def test_a_missing_folder_refuses_rather_than_importing_nothing(
    tmp_path: Path, data_dir: Path
) -> None:
    _sink, emit = _emit_sink()
    with pytest.raises(importer.SetupImportError) as caught:
        importer.run_folder_import(
            data_dir, emit=emit, roots=[tmp_path / "not-here"]
        )
    assert caught.value.code == detect.CODE_REKORDBOX_NOT_FOUND


def test_an_empty_but_readable_folder_imports_zero_without_complaint(
    tmp_path: Path, data_dir: Path
) -> None:
    """The one case where "0 tracks" is a true statement."""
    empty = tmp_path / "empty"
    empty.mkdir()
    _sink, emit = _emit_sink()
    outcome = importer.run_folder_import(data_dir, emit=emit, roots=[empty])
    assert outcome.files_seen == 0
    assert outcome.tracks_written == 0
    assert outcome.unreadable_roots == []


# ----- the endpoints ------------------------------------------------------
def test_detect_folder_counts_what_is_there(
    client: TestClient, library: Path
) -> None:
    body = client.get(f"{API}/detect/folder", params={"path": str(library)}).json()
    assert body["readable"] is True
    assert body["denied"] is False
    assert body["audio_files"] == 3
    assert len(body["sample"]) == 3


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_detect_folder_never_reports_a_count_for_a_denied_folder(
    client: TestClient, unreadable: Path
) -> None:
    """0 here would be a fabrication; `denied` is what makes it readable as one."""
    body = client.get(
        f"{API}/detect/folder", params={"path": str(unreadable)}
    ).json()
    assert body["denied"] is True
    assert body["readable"] is False
    assert body["audio_files"] == 0
    assert body["sample"] == []
    assert "System Settings" in body["how_to_grant"]


def test_detect_folder_on_a_missing_path_is_not_a_denial(
    client: TestClient, tmp_path: Path
) -> None:
    body = client.get(
        f"{API}/detect/folder", params={"path": str(tmp_path / "gone")}
    ).json()
    assert body["exists"] is False
    assert body["denied"] is False


def test_folder_import_enqueues_a_folder_mode_job(
    client: TestClient, library: Path, data_dir: Path
) -> None:
    response = client.post(
        f"{API}/import/folder", json={"folders": [str(library)]}
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["payload"]["mode"] == "folder"
    assert job["payload"]["roots"] == [str(library)]
    assert job["payload"]["data_dir"] == str(data_dir)


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_folder_import_refuses_a_denied_folder_with_403_and_instructions(
    client: TestClient, unreadable: Path
) -> None:
    response = client.post(
        f"{API}/import/folder", json={"folders": [str(unreadable)]}
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == detect.CODE_ACCESS_DENIED
    assert "System Settings" in detail["message"]


def test_folder_import_refuses_a_missing_folder(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        f"{API}/import/folder", json={"folders": [str(tmp_path / "gone")]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == detect.CODE_REKORDBOX_NOT_FOUND


def test_folder_import_needs_at_least_one_folder(client: TestClient) -> None:
    assert client.post(f"{API}/import/folder", json={"folders": []}).status_code == 422


def test_status_carries_the_folder_stage_list(client: TestClient) -> None:
    """The wizard renders whichever stage list applies, never a hardcoded one."""
    assert client.get(f"{API}/status").json()["folder_stages"] == [
        "detect",
        "scan",
        "ingest",
    ]


def test_status_renders_a_folder_outcome_as_a_folder_outcome(
    client: TestClient, library: Path, data_dir: Path
) -> None:
    """Dispatched on `kind`, so a folder import is not read as a rekordbox one."""
    _sink, emit = _emit_sink()
    importer.run_folder_import(data_dir, emit=emit, roots=[library])
    last = client.get(f"{API}/status").json()["last_import"]
    assert last["kind"] == "folder"
    assert last["tracks_without_analysis"] == 3
    assert last["analysis_available"] is False


def test_permissions_reports_every_probed_root(client: TestClient) -> None:
    body = client.get(f"{API}/permissions").json()
    assert isinstance(body["all_readable"], bool)
    assert len(body["roots"]) >= 1
    assert "System Settings" in body["how_to_grant"]
    for probe in body["roots"]:
        assert set(probe) == {
            "path",
            "exists",
            "readable",
            "denied",
            "detail",
        }


# ----- argv ---------------------------------------------------------------
def test_folder_mode_argv_carries_every_root() -> None:
    argv = build_argv(
        {
            "data_dir": "/tmp/library",
            "mode": "folder",
            "roots": ["/tmp/a", "/tmp/b"],
        }
    )
    assert argv.count("--root") == 2
    assert "--mode" in argv and "folder" in argv


@pytest.mark.parametrize(
    "payload",
    [
        {"data_dir": "/tmp/library", "mode": "sideways"},
        {"data_dir": "/tmp/library", "mode": "folder"},
        {"data_dir": "/tmp/library", "mode": "folder", "roots": []},
        {"data_dir": "/tmp/library", "mode": "folder", "roots": ["relative"]},
        {"data_dir": "/tmp/library", "mode": "folder", "roots": [""]},
        {"data_dir": "/tmp/library", "mode": "folder", "roots": "/tmp/a"},
    ],
)
def test_folder_mode_argv_refuses_what_it_cannot_trust(
    payload: dict[str, object],
) -> None:
    with pytest.raises(SetupPayloadError):
        build_argv(payload)
