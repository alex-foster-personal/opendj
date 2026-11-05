"""TRYER-11: ``opendj dj try`` end to end through the real /dj/try routes.

The CLI's HTTP client is pointed at the real FastAPI router (an httpx transport
that hands each request to a TestClient); only ``try_pair``, the execution
layer below the route, is a double.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_UNCONFIRMED, dj_try_cli
from apps.opendj_cli.dj_cli import run as dj_run
from apps.opendj_cli.dj_try_service import DjTryService
from apps.opendj_cli.mcp_server import create_server
from apps.shared.engine_origin import EngineOrigin
from apps.transition_score.tryer import TryError
from apps.webui.server.routes.dj_try import TrackUnavailable
from tests.webui.test_dj_try_routes import OVERRIDE, client  # noqa: F401  # the real-router fixture

ORIGIN = EngineOrigin(host="testserver", port=80, lock_path=Path("unused.lock"), pid=None, role=None, boot_id=None)


def _factory(test_client: TestClient) -> Callable[[Path | None], DjTryService]:
    def forward(request: httpx.Request) -> httpx.Response:
        response = test_client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    return lambda _lock: DjTryService(origin=ORIGIN, transport=httpx.MockTransport(forward))


def _run(argv: list[str], test_client: TestClient, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = dj_try_cli.run(argv, lock=None, service_factory=_factory(test_client))
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.requirement("TRYER-11")
def test_try_pick_undo_round_trip(
    client: tuple[TestClient, list[dict[str, Any]]],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] try, pick, undo run in order [then] each exits 0 and the pick appears then clears, [else stop]."""
    test_client, calls = client
    code, job = _run(["--out", "o1", "--in", "i1", "-n", "3", "--json"], test_client, capsys)
    assert code == EXIT_CONFIRMED and calls[0]["n"] == 3 and len(job["candidates"]) == 3
    assert job["best"] == "A" and job["best_reason"] == ""
    code, picked = _run(["pick", job["job_id"], "A", "--etag", job["etag"]], test_client, capsys)
    assert code == EXIT_CONFIRMED and picked["pick"]["picked_candidate_id"] == "A"
    # An agent still holding the pre-pick etag is refused, never applied to the pick it never saw.
    code, stale = _run(["undo", job["job_id"], "--etag", job["etag"]], test_client, capsys)
    assert code == EXIT_UNCONFIRMED and stale["error"] == "etag_mismatch"
    code, undone = _run(["undo", job["job_id"], "--etag", picked["etag"]], test_client, capsys)
    assert code == EXIT_CONFIRMED and undone["pick"] is None
    code, refused = _run(["undo", job["job_id"], "--etag", undone["etag"]], test_client, capsys)
    assert code == EXIT_UNCONFIRMED and refused["error"] == "no_pick"


@pytest.mark.requirement("TRYER-11")
def test_show_reads_job_and_every_action_accepts_json(
    client: tuple[TestClient, list[dict[str, Any]]],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] pick, show and undo are passed --json [then] each exits 0 and show returns the persisted pick, [else stop]."""
    test_client, _calls = client
    code, job = _run(["--out", "o1", "--in", "i1", "-n", "2"], test_client, capsys)
    assert code == EXIT_CONFIRMED
    code, picked = _run(["pick", job["job_id"], "A", "--etag", job["etag"], "--json"], test_client, capsys)
    assert code == EXIT_CONFIRMED and picked["pick"]["picked_candidate_id"] == "A"
    code, shown = _run(["show", job["job_id"], "--json"], test_client, capsys)
    assert code == EXIT_CONFIRMED and shown["pick"]["picked_candidate_id"] == "A" and shown["etag"] == picked["etag"]
    code, undone = _run(["undo", job["job_id"], "--etag", shown["etag"], "--json"], test_client, capsys)
    assert code == EXIT_CONFIRMED and undone["pick"] is None


@pytest.mark.requirement("TRYER-11")
def test_pick_without_an_etag_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] pick omits --etag [then] it exits 1 with a usage document, never fetching a fresh etag, [else stop]."""
    code = dj_try_cli.run(["pick", "job", "A"], lock=None)
    assert code == EXIT_FAILED and json.loads(capsys.readouterr().out)["error"] == "usage"


@pytest.mark.requirement("TRYER-11")
def test_pick_and_undo_docstring_names_required_etag() -> None:
    """[if] pick or undo copied from module docstring [then] includes --etag ETAG and names show as source, [else stop]."""
    doc = dj_try_cli.__doc__ or ""
    assert "opendj dj try pick JOB CANDIDATE --etag ETAG [--json]" in doc
    assert "opendj dj try undo JOB --etag ETAG [--json]" in doc
    assert "Read ``--etag`` from ``opendj dj try show JOB``." in doc
    assert "opendj dj try pick JOB CANDIDATE [--json]" not in doc
    assert "opendj dj try undo JOB [--json]" not in doc


@pytest.mark.requirement("TRYER-11")
def test_dj_cli_routes_try_and_rejects_missing_pair(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] ``opendj dj try`` lacks --out [then] it exits 1 with a usage document before any engine call, [else stop]."""
    code = dj_run(["try", "--in", "x"], lock=None)
    assert code == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


@pytest.mark.requirement("TRYER-12")
def test_mcp_lists_try_tools_as_mutating_not_destructive() -> None:
    """[if] the MCP server lists its tools [then] the three try-er tools are present, mutating and non-destructive, [else stop]."""
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    for name in ("try_transitions", "pick_transition", "undo_transition_pick"):
        assert name in tools, name
        annotations = tools[name].annotations
        assert annotations is not None, name
        assert annotations.read_only_hint is False and annotations.destructive_hint is False, name


@pytest.mark.requirement("TRYER-11")
@pytest.mark.parametrize(
    "error", [TryError("renderer exited 3"), TrackUnavailable("track trk-in has no audio on this server")]
)
def test_a_try_that_cannot_run_exits_1_like_every_dj_failure(
    client: tuple[TestClient, list[dict[str, Any]]],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """[if] the try route answers 422 try_failed or track_unavailable [then] the CLI exits 1 (failure), never 4 (refused), [else stop]."""
    test_client, _ = client
    OVERRIDE["raise"] = error
    code, document = _run(["--out", "trk-out", "--in", "trk-in", "-n", "2"], test_client, capsys)
    assert code == EXIT_FAILED, document
