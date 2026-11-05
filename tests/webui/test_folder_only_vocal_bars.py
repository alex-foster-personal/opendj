"""Vocal bars for a library with no rekordbox: the deck waveform and the library row.

A folder-imported track has the app's own stable id, no rekordbox mapping and
no ``master.plain.db``. Once its vocals are derived (VOCALS-03) the entry sits
in the vocal-cache under that id, and both readers that draw vocal bars have to
serve it: ``GET /tracks/{id}/anlz`` (the deck waveform) and the listing row
(``GET /tracks`` and ``POST /library/row-assets``, the library preview strip).

[if] a folder-imported track has a valid vocal-cache entry [then] /anlz and its listing row carry the regions as ``demucs``, [else stop].

Real state.db (the production schema and writer), a real vocal-cache entry
written by the production writer; nothing is stubbed.

Regression one-liners:
  - if /anlz answers not_analyzed for a folder-imported track with a valid vocal-cache entry then broken
  - if /anlz serves regions for a folder-imported track whose audio changed since analysis then broken
  - if /anlz answers anything but not_analyzed for a folder-imported track with no entry then broken
  - if a listing row or row-assets disagrees with /anlz about a folder-imported track then broken
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.vocals import cache as vcache
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("VOCALS-04")

REGIONS = [{"start_s": 1.0, "end_s": 3.0, "confidence": 0.75}]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder-only library: ``sung`` has a vocal-cache entry, ``plain`` has none."""
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    media = tmp_path / "media"
    media.mkdir()
    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    for sid in ("sung", "plain"):
        audio = media / f"{sid}.mp3"
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

    monkeypatch.setattr(rb_config, "DATA_DIR", data)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data / "master.plain.db")
    monkeypatch.setattr(rb_config, "STATE_DB", data / "state" / "state.db")
    monkeypatch.setattr(rb_config, "VOCAL_CACHE_DIR", vcache.cache_dir(data))
    monkeypatch.setattr(
        rb_config, "LOCAL_WAVEFORM_CACHE_DIR", data / "state" / "local-waveform-cache"
    )
    vcache.write_entry(
        vcache.cache_path(data, "sung"),
        {
            "schema": vcache.VOCAL_CACHE_SCHEMA, "source": "demucs-htdemucs", "fps": 2.0,
            "duration_s": 4.0, "coverage_pct": 50.0,
            "regions": REGIONS,
            "params": {"hop_s": 0.5},
        },
        media / "sung.mp3",
    )
    return data


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    state = data_dir / "state" / "state.db"
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _anlz_vocals(client: TestClient, stable_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/tracks/{stable_id}/anlz")
    assert response.status_code == 200, response.text
    vocals: dict[str, Any] = response.json()["vocals"]
    return vocals


def test_anlz_serves_a_folder_tracks_vocal_cache_as_demucs(client: TestClient) -> None:
    """[if] a folder-imported track has a valid vocal-cache entry [then] /anlz carries its regions, [else stop]."""
    vocals = _anlz_vocals(client, "sung")

    assert vocals["status"] == "demucs", vocals
    assert [(r["start_s"], r["end_s"]) for r in vocals["regions"]] == [(1.0, 3.0)]
    # The deck waveform and the status route must be one answer, not two.
    status = client.get("/api/v1/vocals/sung/status").json()
    assert (status["status"], status["fps"], status["regions"]) == (
        vocals["status"], vocals["fps"], vocals["regions"],
    )


def test_anlz_says_not_analyzed_with_no_entry_or_a_changed_file(
    client: TestClient, data_dir: Path
) -> None:
    """[if] there is no entry, or the audio changed since analysis [then] /anlz says not_analyzed, [else stop]."""
    assert _anlz_vocals(client, "plain") == {"status": "not_analyzed"}

    audio = data_dir.parent / "media" / "sung.mp3"
    audio.write_bytes(b"a different recording, longer than before")
    os.utime(audio, (2_000_000_000, 2_000_000_000))

    assert _anlz_vocals(client, "sung") == {"status": "not_analyzed"}


def test_listing_row_and_row_assets_carry_the_same_vocals_as_anlz(client: TestClient) -> None:
    """[if] the library row disagrees with /anlz about a folder-imported track [then] fail, [else stop]."""
    expected = {sid: _anlz_vocals(client, sid) for sid in ("sung", "plain")}
    assert expected["sung"]["status"] == "demucs", "control: the entry must be servable"

    listing = client.get("/api/v1/tracks?limit=10")
    assert listing.status_code == 200, listing.text
    rows = {row["stable_id"]: row["vocals"] for row in listing.json()["items"]}
    assets = client.post("/api/v1/library/row-assets", json={"ids": ["sung", "plain"]})
    assert assets.status_code == 200, assets.text

    assert rows == expected
    assert {sid: a["vocals"] for sid, a in assets.json()["assets"].items()} == expected
