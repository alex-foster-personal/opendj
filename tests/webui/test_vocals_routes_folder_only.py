"""Vocals for a library with no rekordbox: folder imports, no master.plain.db.

A new user who imports a folder has tracks with the app's own stable id, no
rekordbox vendor mapping, and no decrypted rekordbox copy on disk. Before
VOCALS-03, ``POST /vocals/analyze`` opened ``master.plain.db`` first and
answered a bare 500 (``FileNotFoundError: MASTER_DB missing on disk``), and
the status route reported ``not_analyzed`` for such a track whatever the
vocal-cache held.

[if] a library has no rekordbox [then] vocals are requested and served by stable id with a typed answer per track, [else stop].

Real state.db (the production schema and writer), real WAV stem bundle, real
vocal-cache; nothing is stubbed.

Regression one-liners:
  - if POST /vocals/analyze answers 5xx because master.plain.db is absent then broken
  - if a folder-imported track with a stem bundle isn't claimed and cached then broken
  - if a folder-imported track with no stem bundle isn't refused missing_stems then broken
  - if a folder-imported track whose audio is gone isn't refused missing_file then broken
  - if an id that is no live track isn't refused unknown_track then broken
  - if a folder-imported track's valid vocal-cache entry isn't GET-visible as demucs then broken
  - if a folder-imported track with no cache entry reports anything but not_analyzed then broken
  - if a master.plain.db that exists but is unreadable is treated as "no rekordbox" then broken
  - if a rekordbox copy behind a directory the engine cannot enter answers a bare 500 or falls back then broken
  - if a rekordbox copy that vanishes after it was seen turns the request into a no-rekordbox one then broken
  - if a vocal-cache path that is there but broken reads as not_analyzed then broken
  - if a state.db failure is reported as MASTER_DB_UNREADABLE then broken
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.stems.artifacts import STEM_PARTS
from apps.vocals import cache as vcache
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import vocals as vocals_routes

pytestmark = pytest.mark.requirement("VOCALS-03")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder-only library: three imported tracks, no rekordbox anywhere.

      hasstem  audio on disk, stem bundle    -> derivable
      nostem   audio on disk, no stem bundle -> needs stems first
      gone     audio path not on disk        -> missing_file
    """
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    media = tmp_path / "media"
    media.mkdir()
    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    for sid in ("hasstem", "nostem", "gone"):
        audio = media / f"{sid}.mp3"
        if sid != "gone":
            audio.write_bytes(b"audio " + sid.encode())
        writer.upsert_track(
            stable_id=sid,
            stable_id_tier="inferred",
            title=sid.title(),
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=4_000,
            file_path=str(audio),
        )
    conn.commit()
    conn.close()
    assert not (data / "master.plain.db").exists()

    monkeypatch.setattr(vocals_routes, "DATA_DIR", data)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data / "master.plain.db")
    monkeypatch.setattr(rb_config, "STATE_DB", data / "state" / "state.db")
    monkeypatch.setattr(rb_config, "VOCAL_CACHE_DIR", vcache.cache_dir(data))
    return data


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(data_dir / "state" / "state.db"),
        mount_frontend=False,
        stem_roots=(data_dir / "configured-stems",),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def stem_bundle(data_dir: Path) -> None:
    """A real 4-stem bundle for ``hasstem`` with two seconds of loud vocals."""
    import numpy as np
    sf = pytest.importorskip("soundfile", reason="needs the optional soundfile package")

    bundle = data_dir / "configured-stems" / "hasstem"
    bundle.mkdir(parents=True)
    sr = 44100
    n = sr * 4
    t = np.arange(n, dtype=np.float32) / sr
    vocals = np.zeros(n, dtype=np.float32)
    vocals[sr : 3 * sr] = 0.4 * np.sin(2 * np.pi * 440 * t[sr : 3 * sr])
    quiet = 0.02 * np.sin(2 * np.pi * 110 * t)
    for part, mono in (
        ("vocals", vocals), ("drums", quiet), ("bass", quiet), ("other", quiet),
    ):
        sf.write(str(bundle / f"{part}.wav"), mono, sr, subtype="PCM_16")
    manifest = {
        "schema_version": 1,
        "stable_id": "hasstem",
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/tmp/source.wav", "sha256": "a" * 64},
        "files": {p: f"{p}.wav" for p in STEM_PARTS},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_analyze_without_master_db_answers_per_track_never_500(
    client: TestClient, data_dir: Path, stem_bundle: None
) -> None:
    """[if] there is no master.plain.db [then] every requested id gets a typed answer, [else stop]."""
    response = client.post(
        "/api/v1/vocals/analyze",
        json={"stable_ids": ["hasstem", "nostem", "gone", "ghost"], "mode": "from-stems"},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "claimed": ["hasstem"],
        "refused": {
            "nostem": "missing_stems",
            "gone": "missing_file",
            "ghost": "unknown_track",
        },
    }


def test_folder_track_with_stems_gets_a_cache_entry_under_its_stable_id(
    client: TestClient, data_dir: Path, stem_bundle: None
) -> None:
    """[if] a folder-imported track has a stem bundle [then] its vocal-cache entry is written and served, [else stop]."""
    analyze = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
    )
    assert analyze.status_code == 202, analyze.text

    entry = vcache.load_valid_entry(
        vcache.cache_path(data_dir, "hasstem"), data_dir.parent / "media" / "hasstem.mp3"
    )
    assert entry is not None
    assert entry["regions"]
    status = client.get("/api/v1/vocals/hasstem/status")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "demucs"
    assert status.json()["regions"]


def test_refused_folder_tracks_write_nothing_and_report_not_analyzed(
    client: TestClient, data_dir: Path
) -> None:
    """[if] a folder-imported track has no stem bundle [then] nothing is cached and its status stays not_analyzed, [else stop]."""
    analyze = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["nostem"], "mode": "from-stems"}
    )
    assert analyze.status_code == 202, analyze.text
    assert analyze.json() == {"claimed": [], "refused": {"nostem": "missing_stems"}}

    assert not vcache.cache_path(data_dir, "nostem").exists()
    status = client.get("/api/v1/vocals/nostem/status")
    assert status.status_code == 200, status.text
    assert status.json() == {
        "stable_id": "nostem", "status": "not_analyzed", "fps": None, "regions": []
    }


def test_already_derived_folder_track_is_refused_cached(
    client: TestClient, stem_bundle: None
) -> None:
    """[if] a folder-imported track already has a valid entry [then] a second request refuses cached_demucs, [else stop]."""
    body = {"stable_ids": ["hasstem"], "mode": "from-stems"}
    assert client.post("/api/v1/vocals/analyze", json=body).json()["claimed"] == ["hasstem"]

    again = client.post("/api/v1/vocals/analyze", json=body)

    assert again.status_code == 202, again.text
    assert again.json() == {"claimed": [], "refused": {"hasstem": "cached_demucs"}}


# ----- a rekordbox copy that is present but broken is NOT "no rekordbox" -----


def _as_directory(path: Path) -> None:
    path.mkdir()


def _as_dangling_link(path: Path) -> None:
    path.symlink_to(path.with_name("nowhere.db"))


def _as_empty_file(path: Path) -> None:
    path.write_bytes(b"")


def _as_garbage_file(path: Path) -> None:
    path.write_bytes(b"this is not a sqlite database, at all" * 64)


@pytest.mark.parametrize(
    "break_copy", [_as_directory, _as_dangling_link, _as_empty_file, _as_garbage_file]
)
def test_broken_rekordbox_copy_fails_typed_and_claims_nothing(
    data_dir: Path, stem_bundle: None, break_copy: Callable[[Path], None]
) -> None:
    """[if] master.plain.db exists but cannot be read [then] the request fails 503 MASTER_DB_UNREADABLE and writes nothing, [else stop]."""
    break_copy(data_dir / "master.plain.db")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(data_dir / "state" / "state.db"),
        mount_frontend=False,
        stem_roots=(data_dir / "configured-stems",),
    )

    # An unhandled error must surface as its response, not as a test crash.
    with TestClient(app, raise_server_exceptions=False) as broken_client:
        response = broken_client.post(
            "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "MASTER_DB_UNREADABLE"
    assert not vcache.cache_path(data_dir, "hasstem").exists()


def test_status_of_a_mapped_track_does_not_fall_back_on_a_dangling_copy(
    client: TestClient, data_dir: Path, stem_bundle: None
) -> None:
    """[if] a mapped track's status is read while the copy is a dangling link [then] it fails typed, with no stable-id fallback, [else stop]."""
    analyze = client.post(
        "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
    )
    assert analyze.json()["claimed"] == ["hasstem"]  # a fallback WOULD have an entry to serve
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
        "VALUES ('hasstem', 'rekordbox', '1')"
    )
    conn.commit()
    conn.close()

    # Control: with the copy truly absent the same mapped track IS served by stable id.
    absent = client.get("/api/v1/vocals/hasstem/status")
    assert absent.status_code == 200, absent.text
    assert absent.json()["status"] == "demucs"

    _as_dangling_link(data_dir / "master.plain.db")
    broken = client.get("/api/v1/vocals/hasstem/status")

    assert broken.status_code == 503, broken.text
    assert broken.json()["detail"]["code"] == "MASTER_DB_UNREADABLE"


# ----- a copy the engine is not permitted to look at is NOT "no rekordbox" -----

_needs_real_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions, so chmod 000 cannot make a path unreadable",
)


def _unhandled_safe_client(data_dir: Path) -> TestClient:
    """A client that returns an unhandled error as its 500 instead of raising in the test."""
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(data_dir / "state" / "state.db"),
        mount_frontend=False,
        stem_roots=(data_dir / "configured-stems",),
    )
    return TestClient(app, raise_server_exceptions=False)


@_needs_real_permissions
def test_copy_under_an_unreadable_directory_fails_typed_not_500(
    data_dir: Path, stem_bundle: None
) -> None:
    """[if] the directory holding master.plain.db cannot be entered [then] analyze answers 503 MASTER_DB_UNREADABLE, [else stop]."""
    with _unhandled_safe_client(data_dir) as locked_client:
        data_dir.chmod(0o000)
        try:
            with pytest.raises(PermissionError):  # control: the lock is real for this process
                (data_dir / "master.plain.db").lstat()
            response = locked_client.post(
                "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
            )
        finally:
            data_dir.chmod(0o755)

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "MASTER_DB_UNREADABLE"
    assert not vcache.cache_path(data_dir, "hasstem").exists()


@_needs_real_permissions
def test_copy_linked_into_an_unreadable_directory_fails_typed_on_both_routes(
    data_dir: Path, tmp_path: Path, stem_bundle: None
) -> None:
    """[if] master.plain.db links into a locked directory [then] analyze and a mapped track's status answer 503 MASTER_DB_UNREADABLE, [else stop]."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "master.plain.db").write_bytes(b"")
    (data_dir / "master.plain.db").symlink_to(locked / "master.plain.db")
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
        "VALUES ('hasstem', 'rekordbox', '1')"
    )
    conn.commit()
    conn.close()

    with _unhandled_safe_client(data_dir) as locked_client:
        locked.chmod(0o000)
        try:
            with pytest.raises(PermissionError):  # control: the lock is real for this process
                (data_dir / "master.plain.db").stat()
            analyze = locked_client.post(
                "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
            )
            status = locked_client.get("/api/v1/vocals/hasstem/status")
        finally:
            locked.chmod(0o755)

    for response in (analyze, status):
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "MASTER_DB_UNREADABLE"
    assert not vcache.cache_path(data_dir, "hasstem").exists()


# ----- "no rekordbox" is decided once: a copy seen and then lost is NOT absent -----


def _healthy_copy(path: Path) -> None:
    """A real, readable rekordbox copy holding no content rows."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, FolderPath TEXT, "
        "AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("vanish_before", ["_open_ro", "master_content_rows"])
def test_copy_that_vanishes_after_the_probe_fails_typed_never_falls_back(
    data_dir: Path, stem_bundle: None, monkeypatch: pytest.MonkeyPatch, vanish_before: str
) -> None:
    """[if] the rekordbox copy disappears after the probe saw it [then] analyze answers 503 MASTER_DB_UNREADABLE and claims nothing, [else stop]."""
    copy = data_dir / "master.plain.db"
    _healthy_copy(copy)
    real = getattr(vocals_routes, vanish_before)
    vanished: list[str] = []

    def vanish_then_call(*args: object, **kwargs: object) -> object:
        # Only the read of the rekordbox copy loses it; state.db reads are untouched.
        reads_the_copy = vanish_before == "master_content_rows" or args[1] == "MASTER_DB"
        if reads_the_copy and copy.exists():
            copy.unlink()
            vanished.append(vanish_before)
        return real(*args, **kwargs)

    monkeypatch.setattr(vocals_routes, vanish_before, vanish_then_call)

    with _unhandled_safe_client(data_dir) as racing_client:
        response = racing_client.post(
            "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
        )

    assert vanished == [vanish_before], "the wrapped read never ran, so nothing was tested"
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "MASTER_DB_UNREADABLE"
    assert not vcache.cache_path(data_dir, "hasstem").exists()


# ----- a vocal-cache path that is there but broken is NOT "not analyzed" -----


def test_cache_path_that_is_a_directory_or_dangling_link_raises_on_status(
    client: TestClient, data_dir: Path
) -> None:
    """[if] a folder track's vocal-cache path is a directory or a dangling link [then] status raises the cache's ValueError, [else stop]."""
    directory = vcache.cache_path(data_dir, "nostem")
    directory.mkdir(parents=True)
    _as_dangling_link(vcache.cache_path(data_dir, "hasstem"))

    for stable_id in ("nostem", "hasstem"):
        with pytest.raises(ValueError, match=r"vocal-cache entry .* is not a readable regular file"):
            client.get(f"/api/v1/vocals/{stable_id}/status")


def test_cache_path_that_is_a_directory_raises_on_analyze(
    client: TestClient, data_dir: Path, stem_bundle: None
) -> None:
    """[if] a folder track's vocal-cache path is a directory [then] analyze raises the cache's ValueError instead of calling it todo, [else stop]."""
    vcache.cache_path(data_dir, "hasstem").mkdir(parents=True)

    with pytest.raises(ValueError, match=r"vocal-cache entry .* is not a readable regular file"):
        client.post(
            "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
        )


@_needs_real_permissions
def test_cache_under_an_unreadable_directory_raises_on_status(
    client: TestClient, data_dir: Path
) -> None:
    """[if] the vocal-cache directory cannot be entered [then] status raises the cache's ValueError, never not_analyzed, [else stop]."""
    cache_root = vcache.cache_dir(data_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_root.chmod(0o000)
    try:
        with pytest.raises(PermissionError):  # control: the lock is real for this process
            vcache.cache_path(data_dir, "nostem").lstat()
        with pytest.raises(ValueError, match=r"vocal-cache entry .* is not a readable regular file"):
            client.get("/api/v1/vocals/nostem/status")
    finally:
        cache_root.chmod(0o755)


# ----- a state.db failure keeps its own error, never the rekordbox copy's label -----


def test_state_db_failure_is_not_labelled_as_a_rekordbox_failure(
    client: TestClient, data_dir: Path, stem_bundle: None
) -> None:
    """[if] state.db fails while a healthy rekordbox copy is present [then] the error is state.db's own, never MASTER_DB_UNREADABLE, [else stop]."""
    _healthy_copy(data_dir / "master.plain.db")
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    conn.execute("ALTER TABLE track_vendor_ids RENAME TO track_vendor_ids_gone")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.OperationalError, match="no such table: track_vendor_ids"):
        client.post(
            "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
        )
    assert not vcache.cache_path(data_dir, "hasstem").exists()


def test_state_db_failure_and_master_failure_are_told_apart_in_one_library(
    data_dir: Path, stem_bundle: None
) -> None:
    """[if] the rekordbox copy is corrupt and state.db is healthy [then] the same request shape is labelled MASTER_DB_UNREADABLE, [else stop]."""
    # Overshoot control for the test above: narrowing the translation must not
    # stop a real failure of the copy, AFTER state.db was read, being labelled.
    _healthy_copy(data_dir / "master.plain.db")
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
        "VALUES ('hasstem', 'rekordbox', '1')"
    )
    conn.commit()
    conn.close()
    master = sqlite3.connect(data_dir / "master.plain.db")
    master.execute("ALTER TABLE djmdContent DROP COLUMN AnalysisDataPath")
    master.commit()
    master.close()

    with _unhandled_safe_client(data_dir) as broken_client:
        response = broken_client.post(
            "/api/v1/vocals/analyze", json={"stable_ids": ["hasstem"], "mode": "from-stems"}
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "MASTER_DB_UNREADABLE"
    assert "AnalysisDataPath" in response.json()["detail"]["message"]
