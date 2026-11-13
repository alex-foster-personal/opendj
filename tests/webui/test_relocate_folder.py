"""LIBM-72: folder relocate previews every rewrite first, then applies only that plan.

[if] a folder relocate writes before apply, writes a stale plan or matches sibling folders [then] fail, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import track_cli
from apps.opendj_cli.track_relocate_folder import relocate_folder
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.relocate_folder_plan import RelocateSourceRow, plan_folder_relocate
from apps.webui.server.sqlite_backend import SqliteBackend

PREVIEW = "/api/v1/relocate-folder/preview"
APPLY = "/api/v1/relocate-folder/apply"

pytestmark = pytest.mark.requirement("LIBM-13875")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """Three missing tracks under old/Set, their files under new/Set, one more with no new file."""
    for name in ("a.mp3", "b.mp3", "sub/c.mp3"):
        target = tmp_path / "new" / "Set" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
    db = tmp_path / "state" / "state.db"
    db.parent.mkdir()
    _seed(
        db,
        {
            "t-a": tmp_path / "old" / "Set" / "a.mp3",
            "t-b": tmp_path / "old" / "Set" / "b.mp3",
            "t-c": tmp_path / "old" / "Set" / "sub" / "c.mp3",
        },
    )
    return tmp_path


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


@pytest.fixture
def client(library: Path) -> Iterator[TestClient]:
    db = library / "state" / "state.db"
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


def _prefixes(library: Path) -> dict[str, str]:
    return {"from_prefix": str(library / "old" / "Set"), "to_prefix": str(library / "new" / "Set")}


def _file_path(client: TestClient, stable_id: str) -> str:
    r = client.get(f"/api/v1/tracks/{stable_id}")
    assert r.status_code == 200, r.text
    return r.json()["file_path"]


def test_preview_lists_three_found_rows_and_writes_nothing(client: TestClient, library: Path) -> None:
    """[if] 3 missing tracks sit under the old prefix with files under the new one [then] preview lists 3 found rows, writes nothing, [else stop]."""
    r = client.post(PREVIEW, json=_prefixes(library))

    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["stable_id"] for row in body["rows"]] == ["t-a", "t-b", "t-c"]
    assert all(row["new_exists"] for row in body["rows"])
    assert body["rows"][2]["new_path"] == str(library / "new" / "Set" / "sub" / "c.mp3")
    assert body["counts"] == {"rows": 3, "new_exists": 3, "missing_target": 0, "skipped_rekordbox": 0}
    assert _file_path(client, "t-a") == str(library / "old" / "Set" / "a.mp3")


def test_row_without_a_new_file_is_flagged_and_skipped_by_apply(client: TestClient, library: Path) -> None:
    """[if] one candidate has no file under the new prefix [then] its row says new_exists false and apply skips and reports it, [else stop]."""
    (library / "new" / "Set" / "b.mp3").unlink()
    preview = client.post(PREVIEW, json=_prefixes(library)).json()
    by_id = {row["stable_id"]: row for row in preview["rows"]}
    assert by_id["t-b"]["new_exists"] is False

    r = client.post(APPLY, json={**_prefixes(library), "plan_id": preview["plan_id"]})

    assert r.status_code == 200, r.text
    assert [row["stable_id"] for row in r.json()["relocated"]] == ["t-a", "t-c"]
    assert [row["stable_id"] for row in r.json()["skipped"]] == ["t-b"]
    assert _file_path(client, "t-b") == str(library / "old" / "Set" / "b.mp3")


def test_apply_after_a_row_path_changed_answers_409_and_writes_nothing(client: TestClient, library: Path) -> None:
    """[if] apply runs with a plan_id after a row's path changed [then] it answers 409 and writes nothing, [else stop]."""
    preview = client.post(PREVIEW, json=_prefixes(library)).json()
    _seed(library / "state" / "state.db", {"t-a": library / "old" / "Set" / "renamed.mp3"})

    r = client.post(APPLY, json={**_prefixes(library), "plan_id": preview["plan_id"]})

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "RELOCATE_PLAN_STALE"
    assert _file_path(client, "t-b") == str(library / "old" / "Set" / "b.mp3")
    assert _file_path(client, "t-c") == str(library / "old" / "Set" / "sub" / "c.mp3")


def test_sibling_folder_with_a_shared_name_prefix_never_matches() -> None:
    """[if] the old prefix is /Music/A and a track sits under /Music/AB [then] only /Music/A children are planned, [else stop]."""
    rows = [
        RelocateSourceRow("t-in", "/Music/A/x.mp3", "v1"),
        RelocateSourceRow("t-sibling", "/Music/AB/y.mp3", "v1"),
        RelocateSourceRow("t-self", "/Music/A", "v1"),
    ]
    plan = plan_folder_relocate(
        rows, from_prefix="/Music/A/", to_prefix="/Disk/A", exists=lambda p: p.startswith("/Disk")
    )

    assert [(row.stable_id, row.new_path) for row in plan.rows] == [("t-in", "/Disk/A/x.mp3")]


def test_apply_success_reads_back_every_new_path(client: TestClient, library: Path) -> None:
    """[if] apply succeeds [then] every rewritten row resolves to its new path on the next read, [else stop]."""
    preview = client.post(PREVIEW, json=_prefixes(library)).json()

    r = client.post(APPLY, json={**_prefixes(library), "plan_id": preview["plan_id"]})

    assert r.status_code == 200, r.text
    for row in preview["rows"]:
        assert _file_path(client, row["stable_id"]) == row["new_path"]
    assert client.post(PREVIEW, json=_prefixes(library)).json()["rows"] == []


def test_present_files_and_relative_prefixes_are_never_planned(client: TestClient, library: Path) -> None:
    """[if] a track's file still exists or a prefix is relative [then] it is not planned or the call answers 422, [else stop]."""
    (library / "old" / "Set").mkdir(parents=True)
    (library / "old" / "Set" / "a.mp3").write_bytes(b"still here")
    rows = client.post(PREVIEW, json=_prefixes(library)).json()["rows"]
    assert [row["stable_id"] for row in rows] == ["t-b", "t-c"]

    r = client.post(PREVIEW, json={"from_prefix": "old/Set", "to_prefix": str(library / "new")})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "RELOCATE_PREFIX_INVALID"


def test_cli_previews_then_applies_the_same_plan(
    client: TestClient, library: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj track relocate-folder OLD NEW apply runs [then] it applies the previewed plan and prints both, [else stop]."""
    prefixes = _prefixes(library)
    dry = relocate_folder(client, prefixes["from_prefix"], prefixes["to_prefix"], apply=False)
    assert dry["applied"] is None
    assert len(dry["preview"]["rows"]) == 3
    assert _file_path(client, "t-a") == str(library / "old" / "Set" / "a.mp3")

    class _Target:
        base_url = "http://engine.test"

    def _handler(request: httpx.Request) -> httpx.Response:
        served = client.post(request.url.path, content=request.content, headers={"content-type": "application/json"})
        return httpx.Response(served.status_code, content=served.content, headers=dict(served.headers))

    monkeypatch.setattr(track_cli.api_cli, "resolve_backend_base_url", _Target)
    monkeypatch.setattr(
        track_cli,
        "engine_client",
        lambda base_url: httpx.Client(base_url=base_url, transport=httpx.MockTransport(_handler)),
    )
    code = track_cli.run(["relocate-folder", prefixes["from_prefix"], prefixes["to_prefix"], "apply"], as_json=True)

    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["stable_id"] for row in result["applied"]["relocated"]] == ["t-a", "t-b", "t-c"]
    assert _file_path(client, "t-a") == str(library / "new" / "Set" / "a.mp3")
    assert track_cli.run(["relocate-folder", prefixes["from_prefix"], prefixes["to_prefix"], "now"], as_json=True) != 0
