"""HTTP parity tests for SYNC-01..06 rb-djay sync wiring (issue #2764)."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.audit import match_rb_djay
from apps.shared.djay_db import DjayTrack
from apps.shared.normalised import NormalisedAnalysis
from apps.webui.server.routes import rb_djay_sync

pytestmark = pytest.mark.requirement("SYNC-03")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(rb_djay_sync.router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.requirement("SYNC-01")
def test_status_includes_fingerprint_flag(client: TestClient) -> None:
    resp = client.get("/api/v1/rb-djay-sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "fingerprint_available" in body
    assert "writeback_enabled" in body


@pytest.mark.requirement("SYNC-02")
def test_match_with_injected_tracks(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRB:
        def __init__(self) -> None:
            self.id = "rb-1"
            self.title = "Song"
            self.artist = "A"
            self.isrc = "USA111"
            self.file_path = tmp_path / "song.mp3"
            self.duration_s = 180.0

    rb = _FakeRB()
    rb.file_path.touch()
    dj = DjayTrack(
        uuid="u-1",
        title="Song",
        artist="A",
        isrc="USA111",
        source_uri="",
        file_path=rb.file_path,
        is_local=True,
        rating=0,
        duration_s=180.0,
        play_count=0,
        color_index=None,
    )

    def _fake_run(**kwargs):  # noqa: ANN003
        out_dir = kwargs["out_dir"]
        result = match_rb_djay.run(
            out_dir=out_dir,
            use_fingerprint=False,
            rb_tracks=[rb],
            dj_tracks=[dj],
        )
        matches_csv = out_dir / "matches.csv"
        return {
            "matches_csv": str(matches_csv),
            "stats": dict(result.stats),
            "matched": len(result.matched),
            "review": len(result.review),
            "rb_only": len(result.rb_only),
            "djay_only": len(result.djay_only),
        }

    monkeypatch.setattr("apps.webui.server.routes.rb_djay_sync.run_match", _fake_run)
    resp = client.post(
        "/api/v1/rb-djay-sync/match",
        json={"use_fingerprint": False, "out_dir": str(tmp_path / "sync")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["matches_csv"].startswith(str(tmp_path))
    assert Path(body["matches_csv"]).exists()


def test_playlist_plan_with_fixture_matches(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    matches = tmp_path / "matches.csv"
    matches.parent.mkdir(parents=True, exist_ok=True)
    with matches.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "rb_id",
                "djay_uuid",
                "confidence",
                "signals_fired",
                "status",
                "rb_title",
                "rb_artist",
                "djay_title",
                "djay_artist",
                "rationale",
            ]
        )

    def _fake_plan(**kwargs):  # noqa: ANN003
        out_dir = kwargs.get("out_dir") or tmp_path / "sync"
        out_dir.mkdir(parents=True, exist_ok=True)
        plan_path = out_dir / "playlist-plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-09-15T00:00:00+00:00",
                    "match_set_sha256": hashlib.sha256(matches.read_bytes()).hexdigest(),
                    "playlists": [],
                    "djay_only": [],
                }
            ),
            encoding="utf-8",
        )
        return {
            "plan_path": str(plan_path),
            "patch_csv": str(out_dir / "playlist-patch.csv"),
            "diff_md": str(out_dir / "playlist-diff.md"),
            "op_total": 0,
            "summary": {
                "create": 0,
                "update": 0,
                "noop": 0,
                "djay_only_playlists": 0,
                "membership_adds": 0,
                "membership_removes": 0,
            },
        }

    monkeypatch.setattr("apps.webui.server.routes.rb_djay_sync.run_playlist_plan", _fake_plan)
    resp = client.post(
        "/api/v1/rb-djay-sync/playlists/plan",
        json={"matches_path": str(matches)},
    )
    assert resp.status_code == 200
    assert resp.json()["op_total"] == 0


def test_playlist_apply_dry_run(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apps.webui.server.routes.rb_djay_sync.run_playlist_apply",
        lambda **kwargs: {"dry_run": True, "totals": {"noop": 1}, "playlists": []},
    )
    resp = client.post("/api/v1/rb-djay-sync/playlists/apply", json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True


def test_playlist_apply_live_without_risks_flag_is_refused(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/rb-djay-sync/playlists/apply",
        json={"dry_run": False, "live": True, "i_understand_the_risks": False},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "LIVE_WRITE_REFUSED"


@pytest.mark.requirement("SYNC-05")
@pytest.mark.requirement("SYNC-06")
def test_metadata_plan_writes_diff_csv_paths(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    matches = tmp_path / "matches.csv"
    with matches.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "rb_id",
                "djay_uuid",
                "confidence",
                "signals_fired",
                "status",
                "rb_title",
                "rb_artist",
                "djay_title",
                "djay_artist",
                "rationale",
            ]
        )
        writer.writerow(["rb-1", "dj-1", "0.95", "4", "matched", "A", "B", "A", "B", "test"])

    rb_analysis = {
        "rb-1": NormalisedAnalysis(
            uuid_or_id="rb-1",
            source="rb",
            bpm=120.0,
            manual_bpm=None,
            key_camelot="8A",
            energy=5,
            tags=None,
            modified_at=None,
        )
    }
    djay_analysis = {
        "dj-1": NormalisedAnalysis(
            uuid_or_id="dj-1",
            source="djay",
            bpm=122.0,
            manual_bpm=None,
            key_camelot="8A",
            energy=6,
            tags=None,
            modified_at=None,
        )
    }

    def _fake_metadata_plan(**kwargs):  # noqa: ANN003
        from apps.audit import sync_diff

        out_dir = kwargs.get("out_dir") or tmp_path / "sync"
        out_dir.mkdir(parents=True, exist_ok=True)
        pairs = [("rb-1", "dj-1")]
        analysis_rows, rating_rows = sync_diff.build_analysis_diff(
            rb_analysis,
            djay_analysis,
            {"rb-1": 4},
            {"dj-1": 5},
            pairs,
            prefer="newest",
        )
        analysis_csv = out_dir / "analysis-diff.csv"
        ratings_csv = out_dir / "ratings-diff.csv"
        sync_diff.write_diff_csv(analysis_rows, analysis_csv)
        sync_diff.write_diff_csv(rating_rows, ratings_csv)
        return {
            "analysis_diff_csv": str(analysis_csv),
            "ratings_diff_csv": str(ratings_csv),
            "analysis_row_count": len(analysis_rows),
            "ratings_row_count": len(rating_rows),
            "analysis_summary": sync_diff.summarise(analysis_rows),
            "ratings_summary": sync_diff.summarise(rating_rows),
            "cue_diff_csv": None,
            "cue_row_count": 0,
        }

    monkeypatch.setattr("apps.webui.server.routes.rb_djay_sync.run_metadata_plan", _fake_metadata_plan)
    resp = client.post(
        "/api/v1/rb-djay-sync/metadata/plan",
        json={"matches_path": str(matches)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert Path(body["analysis_diff_csv"]).exists()
    assert Path(body["ratings_diff_csv"]).exists()


def test_openapi_contract_contains_rb_djay_sync_paths() -> None:
    schema = json.loads(Path("apps/webui/openapi.json").read_text(encoding="utf-8"))
    paths = [p for p in schema["paths"] if "rb-djay-sync" in p]
    assert paths, "openapi.json must document /api/v1/rb-djay-sync routes"
