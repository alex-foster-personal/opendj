"""HTTP and CLI parity for POST /api/sets/{session_id}/split."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import __main__ as sets_main
from apps.sets import paths as sets_paths_mod
from apps.sets import rec_cli
from apps.sets.manifest import read_manifest
from apps.sets.paths import joined_recording_path
from tests.sets.conftest import mount_sets_router
from tests.sets.test_soundcloud_export import _seed_session
from tests.sets.test_split_tracks import THREE_PLAY_TIMELINE, _segment, _write_wav


@pytest.fixture
def api_test_client(sets_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    app = FastAPI()
    mount_sets_router(app)
    with TestClient(app) as client:
        yield client, sets_root


def _seed_joined(sets_root: Path) -> Path:
    sess = _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    joined = joined_recording_path(sess, "s1")
    _write_wav(
        joined,
        [_segment("a", 3.0, 8000), _segment("b", 3.0, 8000), _segment("c", 3.0, 8000)],
    )
    return sess


def _seed_segments_only(sets_root: Path) -> Path:
    sess = _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    for offset, kind in [(0, "a"), (3, "b"), (6, "c")]:
        name = f"audio_2026-04-17T21-30-{offset:02d}.wav"
        _write_wav(sess / name, [_segment(kind, 3.0, 8000)])
    return sess


@pytest.mark.requirement("SET-12012")
def test_post_split_from_segments_only(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] only rolling WAV segments exist [then] POST split returns 200 with three files, [else stop]."""
    client, sets_root = api_test_client
    _seed_segments_only(sets_root)
    resp = client.post("/api/sets/s1/split", json={})
    assert resp.status_code == 200
    assert len(resp.json()["track_files"]) == 3
    assert joined_recording_path(sets_root / "s1", "s1").is_file()


@pytest.mark.requirement("SET-12012")
def test_post_split_returns_track_files(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] the joined recording exists [then] POST split returns three track_files, [else stop]."""
    client, sets_root = api_test_client
    _seed_joined(sets_root)
    resp = client.post("/api/sets/s1/split", json={})
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["track_files"]) == 3
    manifest = read_manifest(sets_root / "s1")
    assert len(manifest.track_files) == 3


@pytest.mark.requirement("SET-12012")
def test_post_split_409_without_joined(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] no joined WAV [then] POST split is 409 RECORDING_NOT_JOINED, [else stop]."""
    client, sets_root = api_test_client
    _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    resp = client.post("/api/sets/s1/split", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RECORDING_NOT_JOINED"


@pytest.mark.requirement("SET-12012")
def test_post_split_409_without_tracklist(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] no tracklist rows [then] POST split is 409 NO_TRACKLIST, [else stop]."""
    client, sets_root = api_test_client
    sess = _seed_session(sets_root, timeline=[])
    joined = joined_recording_path(sess, "s1")
    _write_wav(joined, [_segment("a", 3.0, 8000)])
    resp = client.post("/api/sets/s1/split", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "NO_TRACKLIST"


@pytest.mark.requirement("SET-12012")
def test_post_split_404_unknown(api_test_client: tuple[TestClient, Path]) -> None:
    """[if] session is unknown [then] POST split is 404, [else stop]."""
    client, _sets_root = api_test_client
    resp = client.post("/api/sets/missing/split", json={})
    assert resp.status_code == 404


@pytest.mark.requirement("SET-12012")
def test_post_split_400_bad_session_id(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] session_id is path syntax [then] POST split is 400, [else stop]."""
    client, _sets_root = api_test_client
    resp = client.post("/api/sets/..%5Coutside/split", json={})
    assert resp.status_code == 400


@pytest.mark.requirement("SET-12012")
def test_post_split_422_relative_out_dir(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] out_dir is relative [then] POST split is 422, [else stop]."""
    client, sets_root = api_test_client
    _seed_joined(sets_root)
    resp = client.post("/api/sets/s1/split", json={"out_dir": "relative/out"})
    assert resp.status_code == 422


@pytest.mark.requirement("SET-12012")
def test_openapi_lists_split_route(api_test_client: tuple[TestClient, Path]) -> None:
    """[if] OpenAPI is generated [then] the split path is present, [else stop]."""
    client, _ = api_test_client
    spec = client.app.openapi()
    assert "/api/sets/{session_id}/split" in json.dumps(spec)


@pytest.mark.requirement("SET-12012")
def test_rec_split_cli_parity(
    api_test_client: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] rec split is invoked [then] it mirrors the HTTP route, [else stop]."""
    client, sets_root = api_test_client
    _seed_joined(sets_root)
    out_dir = tmp_path / "out"

    def fake_call(
        _base_url: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if method == "POST" and path.endswith("/split"):
            resp = client.post(path, json=body or {})
            if resp.status_code >= 400:
                raise rec_cli.RecCommandFailed(f"{method} {path}: {resp.status_code} {resp.text}")
            return resp.json()
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(rec_cli, "_call", fake_call)
    code = sets_main.main(
        [
            "rec",
            "split",
            "s1",
            "--base-url",
            "http://127.0.0.1:1",
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    assert all(line.startswith(str(out_dir.resolve())) for line in lines)
    manifest = read_manifest(sets_root / "s1")
    assert all(Path(tf.path).is_absolute() for tf in manifest.track_files)


@pytest.mark.requirement("SET-12012")
def test_rec_split_cli_refuses_without_joined(
    api_test_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] joined audio is missing [then] rec split exits non-zero, [else stop]."""
    client, sets_root = api_test_client
    _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)

    def fake_call(
        _base_url: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        resp = client.post(path, json=body or {})
        if resp.status_code >= 400:
            raise rec_cli.RecCommandFailed(f"{method} {path}: {resp.status_code} {resp.text}")
        return resp.json()

    monkeypatch.setattr(rec_cli, "_call", fake_call)
    with pytest.raises(rec_cli.RecCommandFailed, match="RECORDING_NOT_JOINED"):
        sets_main.main(["rec", "split", "s1", "--base-url", "http://127.0.0.1:1"])
