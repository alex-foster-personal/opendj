"""LIBUX-10719: agent parity for library path columns (issue #10030).

- [if] GET /tracks/{id} [then] file_path is readable for derived columns, [else stop].
- [if] POST /tracks/{id}:reveal [then] agents reveal without the table UI, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.backend import InMemoryBackend
from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.requirement("LIBUX-10719")


def test_track_get_exposes_file_path_for_agents(
    client: TestClient, seed_backend: InMemoryBackend, tmp_path: Path
) -> None:
    """[if] GET /tracks/{id} [then] file_path is present for client-side columns, [else stop]."""
    audio = tmp_path / "music" / "deep" / "song.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3")
    track = seed_backend.get_track("track-001")
    track.file_path = str(audio)
    body = client.get("/api/v1/tracks/track-001").json()
    assert body["file_path"].endswith("song.mp3")


def test_reveal_route_is_registered_for_folder_reveal(
    client: TestClient, seed_backend: InMemoryBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] POST tracks/{id}:reveal [then] the reveal seam is callable without the UI, [else stop]."""
    import subprocess

    audio = tmp_path / "music" / "deep" / "song.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3")
    track = seed_backend.get_track("track-001")
    track.file_path = str(audio)
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    response = client.post("/api/v1/tracks/track-001:reveal")
    assert response.status_code == 204
    assert len(calls) == 1
