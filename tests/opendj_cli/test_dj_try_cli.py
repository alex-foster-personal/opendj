"""TRYER-11: ``opendj dj try`` end to end through the real /dj/try routes.

The CLI's HTTP client is pointed at the real FastAPI router (an httpx transport
that hands each request to a TestClient); only ``try_pair``, the execution
layer below the route, is a double.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.autodj.propose import propose_transition
from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_UNCONFIRMED, dj_try_cli
from apps.opendj_cli.dj_cli import run as dj_run
from apps.opendj_cli.dj_service import DjError
from apps.opendj_cli.dj_try_service import DjTryService
from apps.opendj_cli.helptext import LIBRARY_EPILOG
from apps.opendj_cli.mcp_dj_try_tools import register_try_tools
from apps.opendj_cli.mcp_sdk import CallToolResult, MCPServer, ToolError
from apps.opendj_cli.mcp_server import _engine_identity_mismatch, _engine_not_running, _tool_error, create_server
from apps.shared.engine_origin import EngineOrigin
from apps.transition_score.contract import TryResult
from apps.transition_score.http import MAX_COLUMNS
from apps.transition_score.tryer import TryError
from apps.webui.server.routes import dj_try
from apps.webui.server.routes.dj_try import TrackUnavailable
from tests.autodj.fixtures import anlz_payload, profile, track_row
from tests.transition_score.arrangements import ARRANGEMENT, IN_ARRANGEMENT
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread
from tests.webui.dj_try_support import OVERRIDE, _candidate, _job_dir, _wav, dj_try_client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, list[dict[str, Any]]]]:
    """The real try router behind the CLI's HTTP client, with ``try_pair`` replaced by the contract-typed double."""
    with dj_try_client(tmp_path) as pair:
        yield pair


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
    client: tuple[TestClient, list[dict[str, Any]]],
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
    client: tuple[TestClient, list[dict[str, Any]]],
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
    client: tuple[TestClient, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """[if] the try route answers 422 try_failed or track_unavailable [then] the CLI exits 1 (failure), never 4 (refused), [else stop]."""
    test_client, _ = client
    OVERRIDE["raise"] = error
    code, document = _run(["--out", "trk-out", "--in", "trk-in", "-n", "2"], test_client, capsys)
    assert code == EXIT_FAILED, document


def _mcp_server(test_client: TestClient) -> MCPServer:
    server = MCPServer("try-test")
    register_try_tools(
        server,
        lock_path=lambda: None,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
        engine_identity_mismatch=_engine_identity_mismatch,
        service_factory=_factory(test_client),
    )
    return server


def _mcp_get(server: MCPServer, job_id: str) -> dict[str, Any]:
    result = asyncio.run(server.call_tool("get_transition_try", {"job_id": job_id}))
    assert isinstance(result, CallToolResult) and result.structured_content is not None, result
    return result.structured_content


@pytest.mark.requirement("TRYER-12")
def test_mcp_get_transition_try_reads_the_job_show_reads(
    client: tuple[TestClient, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] an MCP agent names an existing try job [then] it gets the same document and etag as dj try show, [else stop]."""
    test_client, _ = client
    _code, job = _run(["--out", "o1", "--in", "i1", "-n", "2"], test_client, capsys)
    _code, shown = _run(["show", job["job_id"], "--json"], test_client, capsys)
    read = _mcp_get(_mcp_server(test_client), job["job_id"])
    assert read == shown and read["etag"] == job["etag"] and read["pick"] is None
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    annotations = tools["get_transition_try"].annotations
    assert annotations is not None and annotations.read_only_hint is True


@pytest.mark.requirement("TRYER-12")
def test_mcp_get_transition_try_shows_another_clients_change(
    client: tuple[TestClient, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] another client changes a picked job [then] the MCP read returns the new etag, which a guarded pick accepts, [else stop]."""
    test_client, _ = client
    server = _mcp_server(test_client)
    _code, job = _run(["--out", "o1", "--in", "i1", "-n", "2"], test_client, capsys)
    _code, picked = _run(["pick", job["job_id"], "A", "--etag", job["etag"]], test_client, capsys)
    seen = _mcp_get(server, job["job_id"])
    assert seen["etag"] == picked["etag"] and seen["pick"]["picked_candidate_id"] == "A"
    code, undone = _run(["undo", job["job_id"], "--etag", picked["etag"]], test_client, capsys)
    assert code == EXIT_CONFIRMED
    fresh = _mcp_get(server, job["job_id"])
    assert fresh["etag"] == undone["etag"] != seen["etag"] and fresh["pick"] is None
    repick = asyncio.run(
        server.call_tool("pick_transition", {"job_id": job["job_id"], "candidate_id": "B", "etag": fresh["etag"]})
    )
    assert isinstance(repick, CallToolResult) and repick.structured_content is not None
    assert repick.structured_content["pick"]["picked_candidate_id"] == "B"


@pytest.mark.requirement("TRYER-12")
def test_mcp_get_transition_try_refuses_a_missing_job_with_the_route_code(
    client: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    """[if] an MCP agent names a missing job [then] the tool raises job_not_found with the route's 404, [else stop]."""
    test_client, _ = client
    with pytest.raises(ToolError) as refused:
        asyncio.run(_mcp_server(test_client).call_tool("get_transition_try", {"job_id": "nope"}))
    document = json.loads(str(refused.value).removeprefix("Error executing tool get_transition_try: "))
    assert document["error"] == "job_not_found" and document["status_code"] == 404


# ----- get_transition_lanes over a real engine app on loopback ---------------------------------
LANE_OUT, LANE_IN, LANE_BPM = "fixture-out", "fixture-in", 124.0
#: The pair the loopback engine serves: (key, arrangement) per stable id, as tests/transition_score/test_lanes.py.
LANE_TRACKS = {LANE_OUT: ("8A", ARRANGEMENT), LANE_IN: ("9A", IN_ARRANGEMENT)}


def _keep_one_crossfade(out_id: str, in_id: str, *, keep_renders_in: Path, **_kwargs: Any) -> TryResult:
    """Stands in for try_pair: keeps one real crossfade plan with its mix and both deck renders on disk."""
    out_key, out_arrangement = LANE_TRACKS[out_id]
    in_key, in_arrangement = LANE_TRACKS[in_id]
    proposals = propose_transition(
        profile(out_id, bpm=LANE_BPM, key=out_key, arrangement=out_arrangement),
        profile(in_id, bpm=LANE_BPM, key=in_key, arrangement=in_arrangement),
        out_deck=1,
        in_deck=2,
        top=12,
    )["proposals"]
    plan = next(p["plan"] for p in proposals if p["plan"]["style"] == "crossfade")
    folder = keep_renders_in / "A"
    for deck in (1, 2):
        _wav(_job_dir(folder / "decks") / f"deck{deck}.wav")
    kept = _candidate(folder, "A", 0.8)
    return TryResult(
        out_id=out_id,
        in_id=in_id,
        candidates=(dataclasses.replace(kept, plan_json=json.dumps(plan), style="crossfade"),),
    )


@contextmanager
def _lanes_engine(tmp_path: Path) -> Iterator[tuple[MCPServer, str, str]]:
    """The real try router plus the track and anlz payloads it profiles from, served on loopback; yields (server, url, job)."""
    app = FastAPI()
    (tmp_path / "state").mkdir()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    app.include_router(dj_try.router, prefix="/api/v1")
    app.dependency_overrides[dj_try.get_try_runner] = lambda: _keep_one_crossfade

    @app.get("/api/v1/tracks/{stable_id}")
    def track(stable_id: str) -> dict[str, Any]:
        key, arrangement = LANE_TRACKS[stable_id]
        return track_row(stable_id, bpm=LANE_BPM, key=key, bars=sum(row[1] for row in arrangement))

    @app.get("/api/v1/tracks/{stable_id}/anlz")
    def anlz(stable_id: str) -> dict[str, Any]:
        return anlz_payload(stable_id, bpm=LANE_BPM, arrangement=LANE_TRACKS[stable_id][1])

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    engine, thread = start_uvicorn_in_thread(config, what="try-er lanes engine")
    port = engine.servers[0].sockets[0].getsockname()[1]
    origin = dataclasses.replace(ORIGIN, host="127.0.0.1", port=port)
    try:
        job = httpx.post(
            f"{origin.base_url}/api/v1/dj/try", json={"out": LANE_OUT, "in": LANE_IN, "n": 1}, timeout=30.0
        )
        assert job.status_code == 200, job.text
        server = MCPServer("try-lanes-test")
        register_try_tools(
            server,
            lock_path=lambda: None,
            tool_error=_tool_error,
            engine_not_running=_engine_not_running,
            engine_identity_mismatch=_engine_identity_mismatch,
            service_factory=lambda _lock: DjTryService(origin=origin),
        )
        yield server, origin.base_url, job.json()["job_id"]
    finally:
        stop_uvicorn_in_thread(engine, thread, what="try-er lanes engine")


def _mcp_refusal(server: MCPServer, arguments: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(ToolError) as refused:
        asyncio.run(server.call_tool("get_transition_lanes", arguments))
    document: dict[str, Any] = json.loads(
        str(refused.value).removeprefix("Error executing tool get_transition_lanes: ")
    )
    return document


@pytest.mark.requirement("TRYER-12")
def test_mcp_get_transition_lanes_returns_the_panels_lanes_for_a_kept_candidate(tmp_path: Path) -> None:
    """[if] an MCP agent asks for lanes for a kept candidate [then] it gets the route's lane document, [else stop]."""
    with _lanes_engine(tmp_path) as (server, url, job_id):
        result = asyncio.run(
            server.call_tool("get_transition_lanes", {"job_id": job_id, "candidate_id": "A", "columns": 40})
        )
        route = httpx.get(f"{url}/api/v1/dj/try/{job_id}/candidates/A/lanes?columns=40", timeout=30.0)
    assert isinstance(result, CallToolResult) and result.structured_content is not None, result
    lanes = result.structured_content
    assert route.status_code == 200 and lanes == route.json(), (
        "if the tool's lanes differ then agents see another panel"
    )
    assert lanes["candidate"] == "A" and [deck["deck"] for deck in lanes["decks"]] == [1, 2]
    assert all(len(deck["peaks"]) == 40 for deck in lanes["decks"]), (
        "if peaks ignore columns then the bound is not passed"
    )
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    annotations = tools["get_transition_lanes"].annotations
    assert annotations is not None and annotations.read_only_hint is True


@pytest.mark.requirement("TRYER-12")
def test_mcp_get_transition_lanes_refuses_an_unknown_candidate_with_the_route_code(tmp_path: Path) -> None:
    """[if] an MCP agent asks for an unknown candidate [then] the tool raises candidate_not_found with the route's 404, [else stop]."""
    with _lanes_engine(tmp_path) as (server, _url, job_id):
        document = _mcp_refusal(server, {"job_id": job_id, "candidate_id": "Z"})
    assert document["error"] == "candidate_not_found" and document["status_code"] == 404


@pytest.mark.requirement("TRYER-12")
def test_mcp_get_transition_lanes_refuses_more_columns_than_supported(tmp_path: Path) -> None:
    """[if] an MCP agent asks for more than the supported lane columns [then] it raises invalid_columns with the route's 422, [else stop]."""
    with _lanes_engine(tmp_path) as (server, _url, job_id):
        document = _mcp_refusal(server, {"job_id": job_id, "candidate_id": "A", "columns": MAX_COLUMNS + 1})
    assert document["error"] == "invalid_columns" and document["status_code"] == 422


# ----- error taxonomy: the CLI and MCP callers keep the route's code and status (#12503) ------
@dataclasses.dataclass(frozen=True)
class _Refused:
    """One request the route refuses, spelled once per caller: raw HTTP, ``opendj dj try`` argv, MCP tool call."""

    http: tuple[str, str, dict[str, Any] | None, dict[str, str]]
    argv: list[str]
    tool: str
    arguments: dict[str, Any]


#: The try_pair failures the route maps to its own 422 codes.
_TRY_FAILURES: dict[str, Exception] = {
    "try_failed": TryError("renderer exited 3"),
    "track_unavailable": TrackUnavailable("trk-in has no audio"),
}


def _refused_request(code: str, test_client: TestClient, capsys: pytest.CaptureFixture[str]) -> _Refused:
    """Stages the real router so the returned request is refused with ``code``; repeatable, a refusal writes nothing."""
    if code in _TRY_FAILURES:
        OVERRIDE["raise"] = _TRY_FAILURES[code]
        return _Refused(
            http=("POST", "/api/v1/dj/try", {"out": "trk-out", "in": "trk-in", "n": 2, "context_bars": 4}, {}),
            argv=["--out", "trk-out", "--in", "trk-in", "-n", "2"],
            tool="try_transitions",
            arguments={"out_stable_id": "trk-out", "in_stable_id": "trk-in", "n": 2},
        )
    if code == "etag_mismatch":
        _code, job = _run(["--out", "o1", "--in", "i1", "-n", "2"], test_client, capsys)
        _code, _picked = _run(["pick", job["job_id"], "A", "--etag", job["etag"]], test_client, capsys)
        stale = job["etag"]  # the pre-pick etag: an undo against it never deletes the pick it never saw
        return _Refused(
            http=("DELETE", f"/api/v1/dj/try/{job['job_id']}/pick", None, {"If-Match": stale}),
            argv=["undo", job["job_id"], "--etag", stale],
            tool="undo_transition_pick",
            arguments={"job_id": job["job_id"], "etag": stale},
        )
    raise AssertionError(f"no staging for route code {code!r}")


@pytest.mark.requirement("TRYER-11")
@pytest.mark.requirement("TRYER-12")
@pytest.mark.parametrize(
    ("code", "status_code", "exit_code"),
    [
        pytest.param("try_failed", 422, EXIT_FAILED, id="try_failed"),
        pytest.param("track_unavailable", 422, EXIT_FAILED, id="track_unavailable"),
        pytest.param("etag_mismatch", 412, EXIT_UNCONFIRMED, id="etag_mismatch"),
    ],
)
def test_cli_and_mcp_keep_the_route_error_code_and_status(
    client: tuple[TestClient, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    code: str,
    status_code: int,
    exit_code: int,
) -> None:
    """[if] the route refuses a try call with a code [then] CLI and MCP both report that code and HTTP status, [else stop]."""
    test_client, _ = client
    refused = _refused_request(code, test_client, capsys)
    method, path, body, headers = refused.http
    route = test_client.request(method, path, json=body, headers=headers)
    assert (route.status_code, route.json()["error"]) == (status_code, code), route.text
    cli_exit, cli = _run(refused.argv, test_client, capsys)
    assert (cli["error"], cli["status_code"], cli_exit) == (code, status_code, exit_code), cli
    with pytest.raises(ToolError) as raised:
        asyncio.run(_mcp_server(test_client).call_tool(refused.tool, refused.arguments))
    mcp = json.loads(str(raised.value).removeprefix(f"Error executing tool {refused.tool}: "))
    assert (mcp["error"], mcp["status_code"]) == (code, status_code), mcp
    assert mcp == cli, "if the MCP document differs from the CLI's then the two callers disagree on the refusal"


# ----- opendj dj try lanes over the same loopback engine (#12499) ---------------------------
def _run_lanes(url: str, argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    """Runs ``opendj dj try lanes`` against the loopback engine at ``url``; returns (exit code, printed document)."""
    port = httpx.URL(url).port
    assert port is not None, url
    origin = dataclasses.replace(ORIGIN, host="127.0.0.1", port=port)
    code = dj_try_cli.run(["lanes", *argv], lock=None, service_factory=lambda _lock: DjTryService(origin=origin))
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.requirement("TRYER-11")
def test_cli_lanes_prints_the_panels_lane_document_for_a_kept_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a CLI user asks for a kept candidate's lanes [then] it exits 0 with the route's lane document, [else stop]."""
    with _lanes_engine(tmp_path) as (_server, url, job_id):
        code, lanes = _run_lanes(url, [job_id, "A", "--columns", "40", "--json"], capsys)
        route = httpx.get(f"{url}/api/v1/dj/try/{job_id}/candidates/A/lanes?columns=40", timeout=30.0)
    assert code == EXIT_CONFIRMED and route.status_code == 200, lanes
    assert lanes == route.json(), "if the CLI's lanes differ then a shell agent sees another panel"
    assert lanes["candidate"] == "A" and [deck["deck"] for deck in lanes["decks"]] == [1, 2]
    assert all(len(deck["peaks"]) == 40 for deck in lanes["decks"]), "if peaks ignore --columns then it is not passed"


@pytest.mark.requirement("TRYER-11")
def test_cli_lanes_refuses_a_missing_candidate_with_the_route_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a CLI user asks for a missing candidate's lanes [then] it exits 1 with candidate_not_found and 404, [else stop]."""
    with _lanes_engine(tmp_path) as (_server, url, job_id):
        code, document = _run_lanes(url, [job_id, "Z"], capsys)
    assert (document["error"], document["status_code"], code) == ("candidate_not_found", 404, EXIT_FAILED), document


@pytest.mark.requirement("TRYER-11")
@pytest.mark.parametrize("columns", [0, MAX_COLUMNS + 1])
def test_cli_lanes_refuses_invalid_columns_with_the_route_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], columns: int
) -> None:
    """[if] a CLI user passes an invalid columns value [then] it exits 1 with invalid_columns and the route's 422, [else stop]."""
    with _lanes_engine(tmp_path) as (_server, url, job_id):
        code, document = _run_lanes(url, [job_id, "A", "--columns", str(columns)], capsys)
    assert (document["error"], document["status_code"], code) == ("invalid_columns", 422, EXIT_FAILED), document


# ----- opendj dj try save-wav over the same loopback engine (#12500) ------------------------
def _run_save_wav(url: str, argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    """Runs ``opendj dj try save-wav`` against the loopback engine at ``url``; returns (exit code, printed document)."""
    port = httpx.URL(url).port
    assert port is not None, url
    origin = dataclasses.replace(ORIGIN, host="127.0.0.1", port=port)
    code = dj_try_cli.run(["save-wav", *argv], lock=None, service_factory=lambda _lock: DjTryService(origin=origin))
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.requirement("TRYER-11")
def test_cli_save_wav_writes_the_bytes_the_audio_route_serves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a kept candidate is saved to a new WAV path [then] it exits 0 and the file holds the route's exact bytes, [else stop]."""
    target = tmp_path / "saved" / "A.wav"
    target.parent.mkdir()
    (tmp_path / "engine").mkdir()
    with _lanes_engine(tmp_path / "engine") as (_server, url, job_id):
        code, saved = _run_save_wav(url, [job_id, "A", "--out", str(target), "--json"], capsys)
        route = httpx.get(f"{url}/api/v1/dj/try/{job_id}/candidates/A/audio", timeout=30.0)
    assert code == EXIT_CONFIRMED and route.status_code == 200, saved
    assert target.read_bytes() == route.content, "if the saved WAV differs then a review lane keeps another audition"
    assert (saved["path"], saved["bytes"]) == (str(target.resolve()), len(route.content)), saved


@pytest.mark.requirement("TRYER-11")
def test_cli_save_wav_refuses_an_existing_target_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the save-wav output path already exists [then] it exits 4 output_exists, no request made, file untouched, [else stop]."""
    target = tmp_path / "keep.wav"
    target.write_bytes(b"precious")

    def no_service(_lock: Path | None) -> DjTryService:
        raise AssertionError("if save-wav builds a service then it refused only after contacting the engine")

    code = dj_try_cli.run(["save-wav", "job", "A", "--out", str(target)], lock=None, service_factory=no_service)
    document = json.loads(capsys.readouterr().out)
    assert (document["error"], code) == ("output_exists", EXIT_UNCONFIRMED), document
    assert target.read_bytes() == b"precious", "if the target changed then save-wav overwrote a file"


@pytest.mark.requirement("TRYER-11")
def test_cli_save_wav_maps_a_pruned_render_to_the_route_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the render was pruned before download [then] save-wav exits 1 with render_missing and 404 and writes nothing, [else stop]."""
    target = tmp_path / "pruned.wav"
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    with _lanes_engine(engine_dir) as (_server, url, job_id):
        # The route's own prune: with one job kept, the next try deletes this (older) job's renders.
        monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 1)
        marker = engine_dir / dj_try.TRY_JOB_RENDERS / job_id / dj_try.JOB_DONE_MARKER
        os.utime(marker, (1_000_000, 1_000_000))
        newer = httpx.post(f"{url}/api/v1/dj/try", json={"out": LANE_OUT, "in": LANE_IN, "n": 1}, timeout=30.0)
        assert newer.status_code == 200 and not marker.parent.exists(), newer.text
        code, document = _run_save_wav(url, [job_id, "A", "--out", str(target)], capsys)
    assert (document["error"], document["status_code"], code) == ("render_missing", 404, EXIT_FAILED), document
    assert not target.exists(), "if a file was written then a failed download left a partial WAV"


# ----- stale etag recovery: refused pick, show, retry (#12512) ------------------------------
def _job_changed_by_another_client(test_client: TestClient, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Tries a job via the CLI, then another client picks and undoes it over HTTP; returns (job_id, the CLI's stale etag)."""
    _code, job = _run(["--out", "o1", "--in", "i1", "-n", "2"], test_client, capsys)
    picked = test_client.post(
        f"/api/v1/dj/try/{job['job_id']}/pick", json={"candidate": "A"}, headers={"If-Match": job["etag"]}
    )
    assert picked.status_code == 200, picked.text
    undone = test_client.delete(f"/api/v1/dj/try/{job['job_id']}/pick", headers={"If-Match": picked.json()["etag"]})
    assert undone.status_code == 200 and undone.json()["etag"] != job["etag"], undone.text
    return job["job_id"], job["etag"]


@pytest.mark.requirement("TRYER-11")
def test_pick_with_a_stale_etag_is_refused_after_another_client_changed_the_job(
    client: tuple[TestClient, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] pick sends the etag of a job another client changed since [then] it exits 4 etag_mismatch with 412, [else stop]."""
    test_client, _ = client
    job_id, stale = _job_changed_by_another_client(test_client, capsys)
    code, refused = _run(["pick", job_id, "B", "--etag", stale], test_client, capsys)
    assert (refused["error"], refused["status_code"], code) == ("etag_mismatch", 412, EXIT_UNCONFIRMED), refused


@pytest.mark.requirement("TRYER-11")
def test_show_after_a_stale_pick_reads_the_current_job_and_etag(
    client: tuple[TestClient, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] show runs after a refused stale pick [then] it returns the route's current etag and no pick, [else stop]."""
    test_client, _ = client
    job_id, stale = _job_changed_by_another_client(test_client, capsys)
    _code, _refused = _run(["pick", job_id, "B", "--etag", stale], test_client, capsys)
    code, shown = _run(["show", job_id], test_client, capsys)
    current = test_client.get(f"/api/v1/dj/try/{job_id}").json()
    assert code == EXIT_CONFIRMED and shown == current, shown
    assert shown["etag"] != stale and shown["pick"] is None, "if a pick is shown then the refused pick was applied"


@pytest.mark.requirement("TRYER-11")
def test_retry_with_the_etag_show_returned_records_the_pick(
    client: tuple[TestClient, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] pick is retried with the etag show returned after a stale refusal [then] it exits 0 with that pick, [else stop]."""
    test_client, _ = client
    job_id, stale = _job_changed_by_another_client(test_client, capsys)
    _code, _refused = _run(["pick", job_id, "B", "--etag", stale], test_client, capsys)
    _code, shown = _run(["show", job_id], test_client, capsys)
    code, picked = _run(["pick", job_id, "B", "--etag", shown["etag"]], test_client, capsys)
    assert code == EXIT_CONFIRMED and picked["pick"]["picked_candidate_id"] == "B", picked
    assert picked["etag"] != shown["etag"], "if the etag did not move then the next stale writer would be accepted"
    assert test_client.get(f"/api/v1/dj/try/{job_id}").json() == picked, "if show differs then the pick did not persist"


# ----- a 2xx the try-er contract cannot read is a typed error (#12504) -----------------------
@contextmanager
def _engine_answering_200(body: bytes) -> Iterator[DjTryService]:
    """A real loopback HTTP server answering every request 200 application/json with ``body``; yields a service on it."""

    class Answer(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = _answer

        def log_message(self, format: str, *args: Any) -> None:  # stdlib signature: silence per-request logs
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    thread = threading.Thread(target=server.serve_forever, name="try-200-engine", daemon=True)
    thread.start()
    try:
        yield DjTryService(origin=dataclasses.replace(ORIGIN, host="127.0.0.1", port=server.server_address[1]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10.0)


def _run_against(
    service: DjTryService, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    """Runs ``opendj dj try`` with ``service``; returns (exit code, printed document)."""
    code = dj_try_cli.run(argv, lock=None, service_factory=lambda _lock: service)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.requirement("TRYER-11")
@pytest.mark.parametrize("body", [b'[{"job_id": "j1"}]', b'"j1"', b"null"], ids=["array", "string", "null"])
def test_a_200_whose_json_is_not_an_object_is_an_upstream_error(
    capsys: pytest.CaptureFixture[str], body: bytes
) -> None:
    """[if] a try route answers 200 with a JSON array, string or null [then] show exits 1 upstream_error with 200, [else stop]."""
    with _engine_answering_200(body) as service:
        code, document = _run_against(service, ["show", "j1"], capsys)
        with pytest.raises(DjError) as raised:
            service.pick("j1", "A", "etag")
    assert (document["error"], document["status_code"], code) == ("upstream_error", 200, EXIT_FAILED), document
    assert raised.value.code == "upstream_error", "if pick passes a non-object through then MCP callers crash on it"


@pytest.mark.requirement("TRYER-11")
def test_a_200_whose_body_is_not_json_is_an_upstream_error(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a try route answers 200 with a body that is not JSON [then] show exits 1 upstream_error, not a traceback, [else stop]."""
    with _engine_answering_200(b"<html>proxy login</html>") as service:
        code, document = _run_against(service, ["show", "j1"], capsys)
    assert (document["error"], document["status_code"], code) == ("upstream_error", 200, EXIT_FAILED), document
    assert "not JSON" in document["message"], document


@pytest.mark.requirement("TRYER-11")
def test_a_200_with_a_job_object_is_printed_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a try route answers 200 with a job object [then] show exits 0 and prints exactly that object, [else stop]."""
    job: dict[str, Any] = {"job_id": "j1", "etag": 'W/"r1"', "candidates": [], "pick": None}
    with _engine_answering_200(json.dumps(job).encode()) as service:
        code, document = _run_against(service, ["show", "j1"], capsys)
    assert (code, document) == (EXIT_CONFIRMED, job), document


# ----- the etag pick and undo require is documented where a user looks (#12516) -------------
def _try_help(action: str, capsys: pytest.CaptureFixture[str]) -> str:
    """Runs ``opendj dj try <action> --help`` through the dj CLI; returns the help it prints after its clean exit."""
    with pytest.raises(SystemExit) as exited:
        dj_run(["try", action, "--help"], lock=None)
    assert exited.value.code == 0, "if help does not exit 0 then a shell agent reads it as a failure"
    return " ".join(capsys.readouterr().out.split())


@pytest.mark.requirement("TRYER-11")
def test_try_pick_help_names_the_required_etag_and_show_as_its_source(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a user runs try pick help [then] it shows --etag as required and names try show as its source, [else stop]."""
    shown = _try_help("pick", capsys)
    assert "--etag ETAG [--json] job_id candidate_id" in shown and "[--etag" not in shown, shown
    assert "required: the etag of the job document you reviewed" in shown and "opendj dj try show JOB" in shown, shown
    assert (
        "opendj dj try pick <job_id> <cand> --etag <etag>" in LIBRARY_EPILOG and "etag from try show" in LIBRARY_EPILOG
    )


@pytest.mark.requirement("TRYER-11")
def test_try_undo_help_names_the_required_etag_and_show_as_its_source(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a user runs try undo help [then] it shows --etag as required and names try show as its source, [else stop]."""
    shown = _try_help("undo", capsys)
    assert "--etag ETAG [--json] job_id" in shown and "[--etag" not in shown, shown
    assert "required: the etag of the job document you reviewed" in shown and "opendj dj try show JOB" in shown, shown
    assert "opendj dj try undo <job_id> --etag <etag>" in LIBRARY_EPILOG
    assert "opendj dj try show <job_id>" in LIBRARY_EPILOG, (
        "if show is not listed then the etag's source is undocumented"
    )


@pytest.mark.requirement("TRYER-11")
@pytest.mark.parametrize("argv", [["pick", "job", "A"], ["undo", "job"]], ids=["pick", "undo"])
def test_pick_or_undo_without_an_etag_is_refused_before_any_request(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """[if] a user omits --etag from pick or undo [then] it exits 1 with a usage error naming --etag, no request, [else stop]."""

    def no_service(_lock: Path | None) -> DjTryService:
        raise AssertionError("if a service is built then the missing etag reached the engine")

    code = dj_try_cli.run(argv, lock=None, service_factory=no_service)
    document = json.loads(capsys.readouterr().out)
    assert (document["error"], code) == ("usage", EXIT_FAILED) and "--etag" in document["message"], document
