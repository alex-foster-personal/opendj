"""POST /api/v1/tracks/{stable_id}:reveal regression tests (issue #2286)."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.backend import InMemoryBackend


def _set_file_path(backend: InMemoryBackend, stable_id: str, file_path: str | None) -> None:
    track = backend.get_track(stable_id)
    track.file_path = file_path


def test_reveal_unknown_track_404(client: TestClient) -> None:
    r = client.post("/api/v1/tracks/missing-track-id:reveal")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_reveal_streaming_uri_422(client: TestClient, seed_backend: InMemoryBackend) -> None:
    _set_file_path(seed_backend, "track-001", "spotify:track:abc")
    r = client.post("/api/v1/tracks/track-001:reveal")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "not_a_local_file"


def test_reveal_empty_file_path_422(client: TestClient, seed_backend: InMemoryBackend) -> None:
    _set_file_path(seed_backend, "track-001", None)
    r = client.post("/api/v1/tracks/track-001:reveal")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "not_a_local_file"


def test_reveal_missing_file_422(
    client: TestClient, seed_backend: InMemoryBackend, tmp_path: Path
) -> None:
    missing = tmp_path / "gone.flac"
    _set_file_path(seed_backend, "track-001", str(missing))
    r = client.post("/api/v1/tracks/track-001:reveal")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "file_missing"


def test_reveal_happy_path_204(
    client: TestClient,
    seed_backend: InMemoryBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "target.flac"
    audio.write_bytes(b"fixture-audio")
    _set_file_path(seed_backend, "track-001", str(audio))
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    r = client.post("/api/v1/tracks/track-001:reveal")
    assert r.status_code == 204
    assert r.content == b""
    assert len(calls) == 1
    system = platform.system()
    if system == "Darwin":
        assert calls[0] == ["open", "-R", str(audio)]
    elif system == "Windows":
        assert calls[0] == ["explorer", f"/select,{audio}"]
    else:
        assert calls[0] == ["xdg-open", str(audio.parent)]


def test_openapi_reveal_route(client: TestClient) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    path = spec["paths"].get("/api/v1/tracks/{stable_id}:reveal")
    assert path is not None
    assert path["post"]["operationId"] == "reveal_track"
