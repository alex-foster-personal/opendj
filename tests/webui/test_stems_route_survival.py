"""STEM-34: stems parity routes must survive bad input (issue #2957)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.cloud.config import CloudConfig
from apps.cloud.stem_source import DirectR2Source
from apps.webui.server.routes.stem_tiers import router as stem_tiers_router
from apps.webui.server.routes.stems_assets import router as stems_assets_router
from tests.cloudsync.conftest import InMemoryAssetS3


def _cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct",
        r2_access_key_id="id",
        r2_secret_access_key="secret",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="host",
        bind_host="127.0.0.1",
    )


def _seed_playlist_data_dir(tmp_path: Path) -> Path:
    """Minimal data_dir with playlists big/small for unknown-playlist tests."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    media = tmp_path / "media"
    media.mkdir()
    audio = media / "track.mp3"
    audio.write_bytes(b"audio")

    state = sqlite3.connect(data_dir / "state" / "state.db")
    state.executescript(
        """
        CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, title TEXT, deleted_at TEXT);
        CREATE TABLE track_vendor_ids (stable_id TEXT, vendor TEXT, vendor_id TEXT);
        CREATE TABLE playlists (playlist_id TEXT PRIMARY KEY, name TEXT, deleted_at TEXT);
        CREATE TABLE playlist_memberships (
            playlist_id TEXT, stable_id TEXT, position INTEGER, deleted_at TEXT);
        """
    )
    master = sqlite3.connect(data_dir / "master.plain.db")
    master.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
        "FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )
    state.execute(
        "INSERT INTO tracks (stable_id, title) VALUES ('todoB', 'title-todoB')"
    )
    state.execute(
        "INSERT INTO track_vendor_ids VALUES ('todoB', 'rekordbox', 'v0')"
    )
    master.execute(
        "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, 0)",
        ("v0", "title-todoB", 100, str(audio), None),
    )
    state.execute(
        "INSERT INTO playlists (playlist_id, name) VALUES ('plbig', 'big')"
    )
    state.execute(
        "INSERT INTO playlists (playlist_id, name) VALUES ('plsml', 'small')"
    )
    state.commit()
    master.commit()
    state.close()
    master.close()
    return data_dir


@contextmanager
def _parity_client(
    *,
    data_dir: Path,
    hydration_cfg: CloudConfig | None = None,
    hydration_s3: InMemoryAssetS3 | None = None,
):
    app = FastAPI()
    app.state.stem_hydration_data_dir = data_dir
    if hydration_cfg is not None and hydration_s3 is not None:
        app.state.stem_hydration_source = DirectR2Source(
            cfg=hydration_cfg, s3=hydration_s3
        )
        app.state.stem_hydration_cfg = hydration_cfg
        app.state.stem_hydration_s3 = hydration_s3
    else:
        app.state.stem_hydration_source = None
        app.state.stem_hydration_cfg = None
        app.state.stem_hydration_s3 = None

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(stems_assets_router, prefix="/api/v1")
    app.include_router(stem_tiers_router, prefix="/api/v1")
    with TestClient(app) as client:
        yield client


def _assert_survives(client: TestClient, bad_call: Callable[[], Any]) -> None:
    resp = bad_call()
    assert resp.status_code >= 400
    assert resp.headers.get("content-type", "").startswith("application/json")
    assert client.get("/api/v1/health").status_code == 200


@pytest.mark.requirement("STEM-34")
def test_bulk_hydrate_unknown_playlist_returns_404_with_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = _seed_playlist_data_dir(tmp_path)
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )

    with _parity_client(data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.post(
            "/api/v1/stems/bulk-hydrate",
            json={
                "playlist": "all",
                "budget_bytes": 2_000_000_000,
                "refresh_index": False,
                "data_dir": str(data_dir),
            },
        )
        assert resp.status_code == 404
        assert resp.json() == {
            "detail": "unknown playlist 'all'",
            "known": ["big", "small"],
        }
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.requirement("STEM-34")
def test_stems_parity_routes_survive_bad_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = _seed_playlist_data_dir(tmp_path)
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )
    monkeypatch.delenv("MDT_EXTERNAL_STEM_ROOTS", raising=False)

    corrupt_journal = data_dir / "state" / "bad.jsonl"
    corrupt_journal.parent.mkdir(parents=True, exist_ok=True)
    corrupt_journal.write_text("not json\n{}\n", encoding="utf-8")

    bad_calls: list[Callable[[TestClient], Any]] = [
        lambda c: c.post(
            "/api/v1/stems/bulk-hydrate",
            json={
                "playlist": "all",
                "budget_bytes": 1_000,
                "data_dir": str(data_dir),
            },
        ),
        lambda c: c.post(
            "/api/v1/stems/index/build",
            json={
                "journal_path": str(corrupt_journal),
                "data_dir": str(data_dir),
            },
        ),
        lambda c: c.post("/api/v1/stems/push-missing", json={}),
        lambda c: c.post(
            "/api/v1/stems/sid1/hydrate",
            json={"manifest_path": "/nonexistent/manifest.json", "dry_run": True},
        ),
        lambda c: c.post(
            "/api/v1/stems/generate",
            json={"stable_id": "abc", "tier": "NONEXISTENT-TIER"},
        ),
        lambda c: c.get("/api/v1/stems/estimate", params={"seconds": 0}),
    ]

    with _parity_client(data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        for bad_call in bad_calls:
            _assert_survives(client, lambda fn=bad_call: fn(client))
