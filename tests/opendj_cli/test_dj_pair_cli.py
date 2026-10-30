"""MEMORY-02: ``opendj dj pair-transition`` is the routes on the command line.

[if] a plan is set from a file [then] get prints it back, played counts it, clear forgets it, [else stop].
[if] a pair has no saved plan [then] get exits 4 naming pair_transition_not_found, [else stop].
[if] the arguments are wrong [then] it exits 1 with a usage error, never a call, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_UNCONFIRMED
from apps.opendj_cli.dj_cli import run as dj_run
from apps.opendj_cli.dj_pair_cli import DjPairService, run
from apps.opendj_cli.origin import EngineOrigin
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server.routes import pair_transition
from tests.autodj.test_pair_memory import plan_document

pytestmark = pytest.mark.requirement("MEMORY-02")

PAIR = ["--out", "out-track", "--in", "in-track"]


class _SyncAsgi(httpx.BaseTransport):
    """The real routes in-process, driven through the sync client the CLI uses."""

    def __init__(self, app: FastAPI) -> None:
        self._client = TestClient(app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        answer = self._client.request(
            request.method, str(request.url), content=request.read(), headers=dict(request.headers)
        )
        return httpx.Response(answer.status_code, content=answer.content, headers=answer.headers)


@pytest.fixture
def service(tmp_path: Path) -> Iterator[DjPairService]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    conn.close()
    app = FastAPI()
    app.state.state_db_path = path
    app.include_router(pair_transition.router, prefix="/api/v1")
    origin = EngineOrigin(host="testserver", port=80, lock_path=path, pid=None, role=None, boot_id=None)
    yield DjPairService(origin=origin, transport=_SyncAsgi(app))


def _run(service: DjPairService, argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = run(argv, lock=None, service_factory=lambda _lock: service)
    return code, json.loads(capsys.readouterr().out)


def test_set_get_played_clear(service: DjPairService, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_document()), encoding="utf-8")
    code, saved = _run(service, ["set", str(plan), "--source", "edited"], capsys)
    assert code == EXIT_CONFIRMED
    assert (saved["meta"]["source"], saved["meta"]["style"]) == ("edited", "crossfade")
    code, got = _run(service, ["get", *PAIR, "--out-deck", "2", "--in-deck", "1"], capsys)
    assert code == EXIT_CONFIRMED
    assert got["plan"]["out"]["deck"] == 2
    assert _run(service, ["played", *PAIR], capsys)[1]["meta"]["times_played"] == 1
    assert _run(service, ["capture"], capsys)[1]["state"] == "idle"
    assert _run(service, ["clear", *PAIR], capsys) == (EXIT_CONFIRMED, {"cleared": True})


def test_played_with_a_plan_saves_it(
    service: DjPairService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_document()), encoding="utf-8")
    code, saved = _run(service, ["played", *PAIR, str(plan), "--source", "planner"], capsys)
    assert code == EXIT_CONFIRMED
    assert (saved["meta"]["source"], saved["meta"]["times_played"]) == ("planner", 1)


def test_a_miss_is_a_refusal(service: DjPairService, capsys: pytest.CaptureFixture[str]) -> None:
    code, document = _run(service, ["get", *PAIR], capsys)
    assert code == EXIT_UNCONFIRMED
    assert document["error"] == "pair_transition_not_found"


def test_a_plan_without_a_source_is_a_usage_error(
    service: DjPairService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_document()), encoding="utf-8")
    code, document = _run(service, ["played", *PAIR, str(plan)], capsys)
    assert (code, document["error"]) == (EXIT_FAILED, "usage")


def test_opendj_dj_routes_the_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    code = dj_run(["pair-transition", "get", "--out", "x"], lock=None)
    assert (code, json.loads(capsys.readouterr().out)["error"]) == (EXIT_FAILED, "usage")
