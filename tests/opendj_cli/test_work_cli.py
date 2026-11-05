"""``opendj work``: each verb is the same call the UI makes (PRD R-API-1)."""
from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from apps.opendj_cli import EXIT_FAILED, EXIT_OK, api_cli, work_cli
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server.deps import get_read_state, get_write_state
from apps.webui.server.routes import library_org, library_works
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

T0 = "2026-10-30T00:00:00Z"


@pytest.mark.parametrize(("argv", "method", "path", "body"), [
    (["list"], "GET", "/api/v1/works", None),
    (["create", "--title", "Lacrimosa", "--kind", "classical", "--composer", "Mozart"], "POST",
     "/api/v1/works",
     {"kind": "classical", "aliases": [], "title": "Lacrimosa", "composer": "Mozart"}),
    (["edit", "w1", "--title", "New"], "PATCH", "/api/v1/works/w1", {"title": "New"}),
    (["link-many", "a", "b", "--title", "X", "--role", "cover"], "POST", "/api/v1/works:link",
     {"member_ids": ["a", "b"], "kind": "song", "role": "cover", "title": "X"}),
    (["unlink", "w1", "a"], "DELETE", "/api/v1/works/w1/members/track/a", None),
    (["role", "w1", "a", "live"], "PATCH", "/api/v1/works/w1/members/track/a", {"role": "live"}),
    (["move", "w1", "b", "a"], "POST", "/api/v1/works/w1/members:move",
     {"member_ids": ["b", "a"]}),
    (["merge", "w1", "w2"], "POST", "/api/v1/works/w1:merge", {"into": "w2"}),
    (["stack", "a", "--playlist", "p"], "GET", "/api/v1/works/stack?member=a&playlist_id=p", None),
    (["classical", "a"], "GET", "/api/v1/tracks/a/classical", None),
    (["classical", "a", "--label", "DG", "--recording-year", "1962"], "PATCH",
     "/api/v1/tracks/a/classical", {"label": "DG", "recording_year": 1962}),
    (["undo"], "POST", "/api/v1/undo", None),
])
def test_verbs_map_to_the_routes(
    argv: list[str], method: str, path: str, body: dict[str, object] | None
) -> None:
    request, _ = work_cli.build_request(argv)
    assert (request.method, request.path, request.body) == (method, path, body)


def test_an_edit_never_sends_a_field_the_caller_left_out() -> None:
    request, _ = work_cli.build_request(["edit", "w1", "--composer", "Mozart"])
    assert request.body == {"composer": "Mozart"}


def test_a_track_id_cannot_escape_its_path_segment() -> None:
    request, _ = work_cli.build_request(["classical", "../../etc"])
    assert request.path == "/api/v1/tracks/..%2F..%2Fetc/classical"


def test_an_unknown_verb_fails_loudly(capsys: pytest.CaptureFixture[str]) -> None:
    assert work_cli.run(["nope"]) == EXIT_FAILED
    assert "opendj work" in capsys.readouterr().err


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    for stable_id in ("a", "b"):
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, created_at,"
            " updated_at) VALUES (?, 'inferred', 'Lacrimosa', '[\"X\"]', ?, ?)",
            (stable_id, T0, T0),
        )
    conn.commit()
    conn.close()
    (tmp_path / "machine-id").write_text("a" * 32)
    app = FastAPI()
    app.state.state_db_path = path
    app.include_router(library_org.router, prefix="/api/v1")
    app.include_router(library_works.router, prefix="/api/v1")
    app.dependency_overrides[get_read_state] = lambda: None
    app.dependency_overrides[get_write_state] = lambda: None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(
            base_url=f"http://127.0.0.1:{port}"
        ),
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server, thread = start_uvicorn_in_thread(config, what="work cli engine")
    yield port
    stop_uvicorn_in_thread(server, thread, what="work cli engine")


def test_a_real_round_trip_links_shows_and_undoes(
    engine: int, capsys: pytest.CaptureFixture[str]
) -> None:
    assert work_cli.run(["link-many", "a", "b", "--title", "Lacrimosa"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["undo_id"]
    assert work_cli.run(["stack", "a"]) == EXIT_OK
    assert {m["member_id"] for m in json.loads(capsys.readouterr().out)["members"]} == {"a", "b"}
    assert work_cli.run(["undo"]) == EXIT_OK
    capsys.readouterr()
    assert work_cli.run(["stack", "a"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["members"] == []


def test_a_server_refusal_is_a_nonzero_exit(engine: int) -> None:
    assert work_cli.run(["classical", "ghost", "--label", "DG"]) != EXIT_OK
