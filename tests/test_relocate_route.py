"""Contract tests for the relocate-files router (LANE reconcile-router).

Requirements (node ``relocate-files``):

* [if] a broken track's dead path has a matching file under the configured
  music roots [then] ``GET /relocate/candidates/{id}`` returns it ranked by
  the same triple-validation scoring as ``apps.reconcile.locate``.
* [if] no music roots are configured/present (cloud sandbox, CI) [then]
  candidates comes back empty -- never an error -- since this is a real
  filesystem scan, not mocked data.
* [if] a track is streaming or pathless [then] candidates comes back empty
  with ``original_path: null`` -- nothing local to relocate.
* [if] ``POST /relocate/{id}/apply`` is called without ``confirm: true``, or
  with a ``new_path`` that doesn't exist on disk [then] 422, no write.
* [if] the track has no rekordbox vendor mapping [then] apply patches the
  state-layer ``file_path`` via the same If-Match contract as
  ``PATCH /tracks/{id}``.
* [if] the track has a rekordbox vendor mapping but this environment has no
  live rekordbox database [then] apply 503s cleanly (local-verify-deferred)
  -- it must never silently no-op or crash.

Uses InMemoryBackend + real tmp files (disk truth, no mocked stat results);
rb_vendor.bulk_rb_meta is monkeypatched, same pattern as
tests/test_reconcile_route.py.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.reconcile import locate
from apps.shared import paths as shared_paths
from apps.webui.server import rb_vendor
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Track

# This module exercises live-write MECHANICS against tmp fixtures, so it runs
# with the one-way rekordbox import gate ON (root conftest reads the marker).
# It never touches a real rekordbox target.
pytestmark = pytest.mark.rekordbox_writeback


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """One real candidate file under a fake music root; one dead path."""
    music_root = tmp_path / "music"
    music_root.mkdir()
    candidate = music_root / "track.mp3"
    candidate.write_bytes(b"\x00" * 100)
    monkeypatch.setattr(shared_paths, "MUSIC_ROOTS", [music_root])
    monkeypatch.setattr(rb_vendor, "bulk_rb_meta", lambda stable_ids: {})
    return {
        "candidate": candidate,
        "gone": tmp_path / "elsewhere" / "track.mp3",
    }


@pytest.fixture
def backend(library: dict[str, Path]) -> InMemoryBackend:
    b = InMemoryBackend()
    b.seed_track(
        Track(
            stable_id="t-gone",
            title="Vanished",
            artist="A",
            duration_ms=200_000,
            file_path=str(library["gone"]),
        )
    )
    b.seed_track(
        Track(
            stable_id="t-stream", title="Streamy", artist="B", file_path="tidal:12345"
        )
    )
    b.seed_track(
        Track(stable_id="t-nopath", title="Pathless", artist="C", file_path=None)
    )
    return b


@pytest.fixture
def client(backend: InMemoryBackend) -> Iterator[TestClient]:
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


def _current_etag(backend: InMemoryBackend, stable_id: str) -> str:
    from apps.webui.server.etag import compute_etag

    track = backend.get_track(stable_id)
    return compute_etag(track.stable_id, track.updated_at)


def _candidate_identity(path: Path) -> str:
    stat_result = path.stat()
    return ":".join(
        str(value)
        for value in (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )
    )


def _apply_body(
    library: dict[str, Path],
    *,
    confirm: bool = True,
    vendor_id: str | None = None,
) -> dict[str, object]:
    return {
        "new_path": str(library["candidate"]),
        "expected_candidate_identity": _candidate_identity(library["candidate"]),
        "expected_original_path": str(library["gone"]),
        "expected_vendor_id": vendor_id,
        "confirm": confirm,
    }


def _vendor_meta(vendor_id: str, original_path: Path) -> rb_vendor.RbRowMeta:
    return rb_vendor.RbRowMeta(
        vendor_id=vendor_id,
        folder_path=str(original_path),
        analysis_data_path=None,
        comment=None,
        genre=None,
        play_count=0,
    )


def _swap_candidate_after_scan(
    monkeypatch: pytest.MonkeyPatch,
    candidate: Path,
) -> None:
    original_find = locate.find_candidates

    def _swap(*args: Any, **kwargs: Any) -> list[locate.Candidate]:
        found = original_find(*args, **kwargs)
        replacement = candidate.with_suffix(".replacement")
        replacement.write_bytes(b"replacement bytes")
        replacement.replace(candidate)
        return found

    monkeypatch.setattr(locate, "find_candidates", _swap)


def _swap_candidate_after_mutation_guard_opens(
    monkeypatch: pytest.MonkeyPatch,
    candidate: Path,
) -> None:
    """Replace the pathname after the mutation-boundary FD has opened."""
    from apps.webui.server.routes import relocate as relocate_routes

    original_open = relocate_routes._open_candidate_file
    opens = 0

    def _open_then_swap(*args: object, **kwargs: object):
        nonlocal opens
        checked, descriptor = original_open(*args, **kwargs)
        opens += 1
        if opens == 2:
            replacement = candidate.with_suffix(".guard-replacement")
            replacement.write_bytes(b"replacement after guard open")
            replacement.replace(candidate)
        return checked, descriptor

    monkeypatch.setattr(relocate_routes, "_open_candidate_file", _open_then_swap)


# ------------------------------------------------------------------ candidates


@pytest.mark.requirement("RECON-02")
def test_candidates_finds_basename_match(
    client: TestClient,
    library: dict[str, Path],
) -> None:
    r = client.get("/api/v1/relocate/candidates/t-gone")
    assert r.status_code == 200
    body = r.json()
    assert body["original_path"] == str(library["gone"])
    assert body["vendor_id"] is None
    assert body["total"] == 1
    cand = body["candidates"][0]
    assert cand["path"] == str(library["candidate"])
    assert cand["identity_token"] == _candidate_identity(library["candidate"])
    assert "basename_exact" in cand["signals"]


@pytest.mark.requirement("RECON-02")
def test_candidates_empty_when_no_music_roots(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No configured/present music roots -> empty, not an error."""
    monkeypatch.setattr(shared_paths, "MUSIC_ROOTS", [tmp_path / "does-not-exist"])
    r = client.get("/api/v1/relocate/candidates/t-gone")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "stable_id": "t-gone",
        "original_path": body["original_path"],
        "vendor_id": None,
        "total": 0,
        "candidates": [],
    }


@pytest.mark.requirement("RECON-02")
def test_candidates_empty_for_streaming_and_pathless(client: TestClient) -> None:
    for sid in ("t-stream", "t-nopath"):
        r = client.get(f"/api/v1/relocate/candidates/{sid}")
        assert r.status_code == 200
        body = r.json()
        assert body["original_path"] is None
        assert body["total"] == 0
        assert body["candidates"] == []


@pytest.mark.requirement("RECON-02")
def test_candidates_unknown_track_404(client: TestClient) -> None:
    r = client.get("/api/v1/relocate/candidates/nope")
    assert r.status_code == 404


@pytest.mark.requirement("RECON-02")
def test_candidates_uses_rekordbox_vendor_mapping(
    backend: InMemoryBackend,
    library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rekordbox FolderPath + vendor_id win over the state-layer path."""
    dead_rb_path = str(library["gone"].parent / "moved.mp3")
    meta = rb_vendor.RbRowMeta(
        vendor_id="99",
        folder_path=dead_rb_path,
        analysis_data_path=None,
        comment=None,
        genre=None,
        play_count=0,
    )
    monkeypatch.setattr(
        rb_vendor,
        "bulk_rb_meta",
        lambda stable_ids: {"t-gone": meta} if "t-gone" in stable_ids else {},
    )
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        body = c.get("/api/v1/relocate/candidates/t-gone").json()
    assert body["original_path"] == dead_rb_path
    assert body["vendor_id"] == "99"


# ------------------------------------------------------------------ apply


@pytest.mark.requirement("RECON-04")
def test_apply_requires_confirm(client: TestClient, library: dict[str, Path]) -> None:
    r = client.post(
        "/api/v1/relocate/t-gone/apply", json=_apply_body(library, confirm=False)
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CONFIRM_REQUIRED"


@pytest.mark.requirement("RECON-04")
def test_apply_rejects_nonexistent_candidate_path(
    client: TestClient,
    library: dict[str, Path],
    tmp_path: Path,
) -> None:
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json={
            "new_path": str(tmp_path / "nope.mp3"),
            "expected_candidate_identity": "stale",
            "expected_original_path": str(library["gone"]),
            "expected_vendor_id": None,
            "confirm": True,
        },
        headers={"If-Match": _current_etag(client.app.state.backend, "t-gone")},
    )
    assert r.status_code == 422


@pytest.mark.requirement("RECON-04")
def test_apply_unknown_track_404(client: TestClient, library: dict[str, Path]) -> None:
    r = client.post(
        "/api/v1/relocate/nope/apply",
        json=_apply_body(library),
        headers={"If-Match": '"anything"'},
    )
    assert r.status_code == 404


@pytest.mark.requirement("RECON-04")
def test_apply_state_layer_requires_if_match(
    client: TestClient,
    library: dict[str, Path],
) -> None:
    r = client.post("/api/v1/relocate/t-gone/apply", json=_apply_body(library))
    assert r.status_code == 428


@pytest.mark.requirement("RECON-04")
def test_apply_state_layer_patches_file_path(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
) -> None:
    etag = _current_etag(backend, "t-gone")
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=_apply_body(library),
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "state"
    assert body["new_path"] == str(library["candidate"])
    assert backend.get_track("t-gone").file_path == str(library["candidate"])


@pytest.mark.requirement("RECON-04")
def test_apply_state_layer_stale_etag_conflicts(
    client: TestClient,
    library: dict[str, Path],
) -> None:
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=_apply_body(library),
        headers={"If-Match": '"stale-etag"'},
    )
    assert r.status_code == 409


@pytest.mark.requirement("RECON-04")
def test_apply_rejects_path_outside_music_roots(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")
    body = _apply_body(library)
    body["new_path"] = str(outside)
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=body,
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CANDIDATE_PATH_OUTSIDE_MUSIC_ROOTS"
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_apply_rejects_dataless_stub_candidate(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
) -> None:
    """iCloud-style sparse stubs must not become the new FolderPath."""
    import os
    import sys

    stub = library["candidate"].with_name("dataless-stub.mp3")
    with open(stub, "wb") as handle:
        handle.truncate(1_048_576)
    if os.stat(stub).st_blocks != 0:
        pytest.skip(
            "filesystem does not support sparse files; cannot mimic a placeholder"
        )
    if sys.platform != "darwin":
        pytest.skip("dataless gate is Darwin-scoped")
    body = _apply_body(library)
    body["new_path"] = str(stub)
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=body,
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CANDIDATE_PATH_NOT_MATERIALISED"
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_apply_rejects_final_component_symlink(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
) -> None:
    symlink = library["candidate"].with_name("linked-track.mp3")
    symlink.symlink_to(library["candidate"])
    body = _apply_body(library)
    body["new_path"] = str(symlink)
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=body,
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CANDIDATE_PATH_UNSAFE"
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_candidate_guard_closes_fd_when_music_root_resolution_is_denied(
    library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.webui.server.routes import relocate as relocate_routes

    music_root = library["candidate"].parent
    original_resolve = Path.resolve
    original_open = relocate_routes.os.open
    original_close = relocate_routes.os.close
    opened: list[int] = []
    closed: list[int] = []

    def _resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == music_root:
            raise PermissionError("forced root resolution denial")
        return original_resolve(path, *args, **kwargs)

    def _open(*args: object, **kwargs: object) -> int:
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def _close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(Path, "resolve", _resolve)
    monkeypatch.setattr(relocate_routes.os, "open", _open)
    monkeypatch.setattr(relocate_routes.os, "close", _close)
    with pytest.raises(HTTPException) as raised:
        relocate_routes._open_candidate_file(str(library["candidate"]))
    assert raised.value.detail["code"] == "CANDIDATE_PATH_UNSAFE"
    assert opened
    assert closed == opened


@pytest.mark.requirement("RECON-04")
def test_apply_rejects_stale_recorded_path(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
) -> None:
    body = _apply_body(library)
    body["expected_original_path"] = "/stale/path.mp3"
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=body,
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RELOCATE_TARGET_CHANGED"
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_apply_rejects_changed_vendor_mapping(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rb_vendor,
        "bulk_rb_meta",
        lambda _: {"t-gone": _vendor_meta("new-vendor", library["gone"])},
    )
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=_apply_body(library, vendor_id="old-vendor"),
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RELOCATE_VENDOR_MAPPING_CHANGED"
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_apply_state_rejects_candidate_swap_before_mutation(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _apply_body(library)
    _swap_candidate_after_scan(monkeypatch, library["candidate"])
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=body,
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RELOCATE_CANDIDATE_CHANGED"
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_apply_state_rejects_swap_after_guard_open_without_persisting(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _apply_body(library)
    _swap_candidate_after_mutation_guard_opens(monkeypatch, library["candidate"])
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=body,
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RELOCATE_CANDIDATE_CHANGED"
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_apply_state_postcheck_mismatch_rolls_back_published_update(
    client: TestClient,
    backend: InMemoryBackend,
    library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swap after the precheck must undo the state mutation on postcheck."""
    from apps.webui.server.routes import relocate as relocate_routes

    body = _apply_body(library)
    original_assert = relocate_routes._assert_candidate_path_identity
    checks = 0

    def _check_then_swap(*args: object, **kwargs: object) -> None:
        nonlocal checks
        original_assert(*args, **kwargs)
        checks += 1
        if checks == 1:
            replacement = library["candidate"].with_suffix(".postcheck-replacement")
            replacement.write_bytes(b"replacement before postcheck")
            replacement.replace(library["candidate"])

    monkeypatch.setattr(
        relocate_routes,
        "_assert_candidate_path_identity",
        _check_then_swap,
    )
    r = client.post(
        "/api/v1/relocate/t-gone/apply",
        json=body,
        headers={"If-Match": _current_etag(backend, "t-gone")},
    )
    assert r.status_code == 409
    assert checks == 1
    assert backend.get_track("t-gone").file_path == str(library["gone"])


@pytest.mark.requirement("RECON-04")
def test_apply_rekordbox_verify_failure_restores_backup(
    backend: InMemoryBackend,
    library: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit verify failure must restore the unique live-db backup."""
    live_db = tmp_path / "master.db"
    original_db = b"original database bytes"
    live_db.write_bytes(original_db)
    content = SimpleNamespace(FolderPath=str(library["gone"]))

    class FakeRekordboxDatabase:
        opens = 0

        def __init__(self, path: str) -> None:
            assert path == str(live_db)
            type(self).opens += 1
            self._verify = type(self).opens > 1

        def get_content(self, ID: str):
            assert ID == "99"
            if self._verify:
                raise RuntimeError("forced verify read failure")
            return content

        def commit(self) -> None:
            live_db.write_bytes(b"mutated database bytes")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        rb_vendor,
        "bulk_rb_meta",
        lambda _: {"t-gone": _vendor_meta("99", library["gone"])},
    )
    monkeypatch.setattr(shared_paths, "REKORDBOX_LIVE_DB", live_db)
    monkeypatch.setattr(shared_paths, "DATA_DIR", tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "pyrekordbox",
        SimpleNamespace(Rekordbox6Database=FakeRekordboxDatabase),
    )
    from apps.webui.server.routes import relocate as relocate_routes

    monkeypatch.setattr(relocate_routes, "_assert_rekordbox_not_running", lambda: None)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/relocate/t-gone/apply",
            json=_apply_body(library, vendor_id="99"),
            headers={"If-Match": _current_etag(backend, "t-gone")},
        )
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "RELOCATE_WRITE_ROLLED_BACK"
    assert live_db.read_bytes() == original_db


@pytest.mark.requirement("RECON-04")
def test_apply_rekordbox_primary_close_failure_restores_committed_backup(
    backend: InMemoryBackend,
    library: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful commit followed by primary close failure must roll back."""
    live_db = tmp_path / "master.db"
    original_db = b"original database bytes"
    live_db.write_bytes(original_db)
    content = SimpleNamespace(FolderPath=str(library["gone"]))

    class FakeRekordboxDatabase:
        opens = 0

        def __init__(self, path: str) -> None:
            assert path == str(live_db)
            type(self).opens += 1

        def get_content(self, ID: str):
            assert ID == "99"
            return content

        def commit(self) -> None:
            live_db.write_bytes(b"committed database bytes")

        def close(self) -> None:
            raise RuntimeError("forced primary close failure")

    monkeypatch.setattr(
        rb_vendor,
        "bulk_rb_meta",
        lambda _: {"t-gone": _vendor_meta("99", library["gone"])},
    )
    monkeypatch.setattr(shared_paths, "REKORDBOX_LIVE_DB", live_db)
    monkeypatch.setattr(shared_paths, "DATA_DIR", tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "pyrekordbox",
        SimpleNamespace(Rekordbox6Database=FakeRekordboxDatabase),
    )
    from apps.webui.server.routes import relocate as relocate_routes

    monkeypatch.setattr(relocate_routes, "_assert_rekordbox_not_running", lambda: None)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/relocate/t-gone/apply",
            json=_apply_body(library, vendor_id="99"),
            headers={"If-Match": _current_etag(backend, "t-gone")},
        )
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "RELOCATE_WRITE_ROLLED_BACK"
    assert FakeRekordboxDatabase.opens == 1
    assert live_db.read_bytes() == original_db


@pytest.mark.requirement("RECON-04")
def test_apply_rekordbox_postcommit_path_swap_restores_backup(
    backend: InMemoryBackend,
    library: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_db = tmp_path / "master.db"
    live_db.write_bytes(b"original database bytes")
    committed = False
    content = SimpleNamespace(FolderPath=str(library["gone"]))

    class FakeRekordboxDatabase:
        def __init__(self, path: str) -> None:
            assert path == str(live_db)

        def get_content(self, ID: str):
            assert ID == "99"
            return content

        def commit(self) -> None:
            nonlocal committed
            committed = True
            live_db.write_bytes(b"mutated database bytes")
            replacement = library["candidate"].with_suffix(".commit-replacement")
            replacement.write_bytes(b"replacement during commit")
            replacement.replace(library["candidate"])

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        rb_vendor,
        "bulk_rb_meta",
        lambda _: {"t-gone": _vendor_meta("99", library["gone"])},
    )
    monkeypatch.setattr(shared_paths, "REKORDBOX_LIVE_DB", live_db)
    monkeypatch.setattr(shared_paths, "DATA_DIR", tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "pyrekordbox",
        SimpleNamespace(Rekordbox6Database=FakeRekordboxDatabase),
    )
    from apps.webui.server.routes import relocate as relocate_routes

    monkeypatch.setattr(relocate_routes, "_assert_rekordbox_not_running", lambda: None)
    body = _apply_body(library, vendor_id="99")
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/relocate/t-gone/apply",
            json=body,
            headers={"If-Match": _current_etag(backend, "t-gone")},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RELOCATE_CANDIDATE_CHANGED"
    assert committed
    assert live_db.read_bytes() == b"original database bytes"


@pytest.mark.requirement("RECON-04")
def test_apply_rekordbox_rejects_swap_after_guard_open_without_commit(
    backend: InMemoryBackend,
    library: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_db = tmp_path / "master.db"
    original_db = b"original database bytes"
    live_db.write_bytes(original_db)
    committed = False
    content = SimpleNamespace(FolderPath=str(library["gone"]))

    class FakeRekordboxDatabase:
        def __init__(self, path: str) -> None:
            assert path == str(live_db)

        def get_content(self, ID: str):
            assert ID == "99"
            return content

        def commit(self) -> None:
            nonlocal committed
            committed = True
            live_db.write_bytes(b"mutated database bytes")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        rb_vendor,
        "bulk_rb_meta",
        lambda _: {"t-gone": _vendor_meta("99", library["gone"])},
    )
    monkeypatch.setattr(shared_paths, "REKORDBOX_LIVE_DB", live_db)
    monkeypatch.setattr(shared_paths, "DATA_DIR", tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "pyrekordbox",
        SimpleNamespace(Rekordbox6Database=FakeRekordboxDatabase),
    )
    from apps.webui.server.routes import relocate as relocate_routes

    monkeypatch.setattr(relocate_routes, "_assert_rekordbox_not_running", lambda: None)
    body = _apply_body(library, vendor_id="99")
    _swap_candidate_after_mutation_guard_opens(monkeypatch, library["candidate"])
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/relocate/t-gone/apply",
            json=body,
            headers={"If-Match": _current_etag(backend, "t-gone")},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RELOCATE_CANDIDATE_CHANGED"
    assert not committed
    assert live_db.read_bytes() == original_db


@pytest.mark.requirement("RECON-04")
def test_apply_rekordbox_branch_503s_without_live_db(
    backend: InMemoryBackend,
    library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Vendor-mapped track: a cloud sandbox has no live rekordbox database,
    so apply must 503 cleanly rather than silently no-op or crash."""
    meta = rb_vendor.RbRowMeta(
        vendor_id="99",
        folder_path=str(library["gone"]),
        analysis_data_path=None,
        comment=None,
        genre=None,
        play_count=0,
    )
    monkeypatch.setattr(
        rb_vendor,
        "bulk_rb_meta",
        lambda stable_ids: {"t-gone": meta} if "t-gone" in stable_ids else {},
    )
    monkeypatch.setattr(
        shared_paths, "REKORDBOX_LIVE_DB", tmp_path / "no-such-master.db"
    )
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/relocate/t-gone/apply",
            json=_apply_body(library, vendor_id="99"),
            headers={"If-Match": _current_etag(backend, "t-gone")},
        )
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "REKORDBOX_DB_UNAVAILABLE"
