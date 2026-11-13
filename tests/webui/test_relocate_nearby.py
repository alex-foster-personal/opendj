"""LIBM-73: once one missing track is located, propose its folder move for the missing siblings.

[if] nearby proposes absent files, answers for a never-relocated track or writes anything [then] fail, [else stop].

Regression one-liners:
  - if a located track's missing siblings at the new folder are not proposed then broken
  - if a sibling with no file at the new folder is proposed then broken
  - if a never-relocated track gets a proposal instead of a typed 409 then broken
  - if GET nearby changes any track row or location then broken
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import track_cli
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.backend import TrackUpdate
from apps.webui.server.etag import compute_etag
from apps.webui.server.relocate_folder_plan import (
    LocatedTrack,
    NotAFolderMove,
    RelocateSourceRow,
    plan_nearby,
)
from apps.webui.server.sqlite_backend import SqliteBackend

NEARBY = "/api/v1/relocate-folder/nearby/{}"
APPLY = "/api/v1/relocate-folder/apply"

pytestmark = pytest.mark.requirement("LIBM-13910")


def _db(library: Path) -> Path:
    return library / "state" / "state.db"


def _seed(db: Path, paths: dict[str, Path]) -> None:
    conn = state_db.open_rw(db)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for stable_id, path in paths.items():
            writer.upsert_track(
                stable_id=stable_id,
                stable_id_tier="inferred",
                title=stable_id,
                artists=["Artist"],
                album=None,
                isrc=None,
                duration_ms=180_000,
                file_path=str(path),
            )
    finally:
        writer.close()
        conn.close()


def _relocate_by_hand(db: Path, stable_id: str, new_path: Path) -> None:
    """The state write a per-track relocate makes: one file_path update through the backend."""
    backend = SqliteBackend(db)
    track = backend.get_track(stable_id)
    etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    backend.update_tracks([TrackUpdate(stable_id, {"file_path": str(new_path)}, etag)], source="webui")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """t-a was missing under old/Set and is now located under new/Set; t-b and t-c are still missing."""
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        target = tmp_path / "new" / "Set" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
    home = tmp_path / "home" / "kept.mp3"
    home.parent.mkdir()
    home.write_bytes(b"audio")
    db = _db(tmp_path)
    db.parent.mkdir()
    old = tmp_path / "old" / "Set"
    _seed(db, {"t-a": old / "a.mp3", "t-b": old / "b.mp3", "t-c": old / "c.mp3", "t-home": home})
    _relocate_by_hand(db, "t-a", tmp_path / "new" / "Set" / "a.mp3")
    return tmp_path


@pytest.fixture
def client(library: Path) -> Iterator[TestClient]:
    db = _db(library)
    app = create_app(
        backend=SqliteBackend(db),
        state_db_path=str(db),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


def _snapshot(db: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    conn = sqlite3.connect(str(db))
    try:
        tracks = conn.execute("SELECT stable_id, file_path, updated_at FROM tracks ORDER BY stable_id").fetchall()
        locations = conn.execute(
            "SELECT location_id, file_path, updated_at, deleted_at FROM track_locations ORDER BY location_id"
        ).fetchall()
    finally:
        conn.close()
    return tracks, locations


def test_located_track_proposes_both_missing_siblings(client: TestClient, library: Path) -> None:
    """[if] a located track moved old/Set to new/Set and 2 missing siblings exist there [then] both are proposed, [else stop]."""
    r = client.get(NEARBY.format("t-a"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["previous_path"] == str(library / "old" / "Set" / "a.mp3")
    assert (body["from_prefix"], body["to_prefix"]) == (str(library / "old" / "Set"), str(library / "new" / "Set"))
    assert [(row["stable_id"], row["new_path"]) for row in body["proposed"]] == [
        ("t-b", str(library / "new" / "Set" / "b.mp3")),
        ("t-c", str(library / "new" / "Set" / "c.mp3")),
    ]
    assert body["not_found"] == []
    applied = client.post(
        APPLY, json={"from_prefix": body["from_prefix"], "to_prefix": body["to_prefix"], "plan_id": body["plan_id"]}
    )
    assert applied.status_code == 200, applied.text
    assert [row["stable_id"] for row in applied.json()["relocated"]] == ["t-b", "t-c"]


def test_sibling_absent_at_the_new_folder_is_not_proposed(client: TestClient, library: Path) -> None:
    """[if] a missing sibling has no file at the new folder [then] it is not proposed, only reported, [else stop]."""
    _seed(_db(library), {"t-d": library / "old" / "Set" / "d.mp3"})

    body = client.get(NEARBY.format("t-a")).json()

    assert [row["stable_id"] for row in body["proposed"]] == ["t-b", "t-c"]
    assert [row["stable_id"] for row in body["not_found"]] == ["t-d"]
    assert body["counts"]["missing_target"] == 1


def test_never_relocated_track_answers_typed_409(client: TestClient) -> None:
    """[if] the track was never relocated [then] nearby answers 409 RELOCATE_NEARBY_NOT_RELOCATED, [else stop]."""
    r = client.get(NEARBY.format("t-home"))

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "RELOCATE_NEARBY_NOT_RELOCATED"
    still_missing = client.get(NEARBY.format("t-b"))
    assert still_missing.status_code == 409, still_missing.text
    assert still_missing.json()["detail"]["code"] == "RELOCATE_NEARBY_TRACK_MISSING"


def test_nearby_writes_nothing(client: TestClient, library: Path) -> None:
    """[if] nearby is called [then] no track row or location row changes, [else stop]."""
    before = _snapshot(_db(library))

    assert client.get(NEARBY.format("t-a")).status_code == 200
    assert client.get(NEARBY.format("t-home")).status_code == 409

    assert _snapshot(_db(library)) == before


def test_located_in_the_same_folder_is_not_a_folder_move() -> None:
    """[if] the located track was found in its recorded folder [then] plan_nearby refuses with NotAFolderMove, [else stop]."""
    located = LocatedTrack("t-x", previous_path="/Music/A/x.mp3", current_path="/Music/A/x (1).mp3")

    with pytest.raises(NotAFolderMove):
        plan_nearby(located, [RelocateSourceRow("t-y", "/Music/A/y.mp3", "v1")], exists=lambda _p: True)


def test_cli_relocate_nearby_prints_the_proposal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj track relocate-nearby <id> runs [then] it prints the same proposal as the route, [else stop]."""

    class _Target:
        base_url = "http://engine.test"

    def _handler(request: httpx.Request) -> httpx.Response:
        served = client.get(request.url.raw_path.decode())
        return httpx.Response(served.status_code, content=served.content, headers=dict(served.headers))

    monkeypatch.setattr(track_cli.api_cli, "resolve_backend_base_url", _Target)
    monkeypatch.setattr(
        track_cli,
        "engine_client",
        lambda base_url: httpx.Client(base_url=base_url, transport=httpx.MockTransport(_handler)),
    )

    assert track_cli.run(["relocate-nearby", "t-a"], as_json=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["stable_id"] for row in result["proposed"]] == ["t-b", "t-c"]
    assert track_cli.run(["relocate-nearby", "t-home"], as_json=True) != 0
    assert track_cli.run(["relocate-nearby"], as_json=True) != 0
