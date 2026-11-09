"""``opendj track related``: the CLI twin of ``/api/v1/tracks/{id}/related`` (LIBUX-10046).

[if] an agent runs `opendj track related <id> bpm-percent 4 genre 0` [then] it prints the route's JSON with those criteria, [else stop].

Regression one-liners:
  - if `track related` drops a flag then agents cannot set criteria per call
  - if a malformed call reaches the engine instead of printing usage then broken
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.verbs import InvocationError, parse_invocation
from tests.waits import start_uvicorn_in_thread
from tests.webui.test_related_route import related_client  # noqa: F401  (fixture)

pytestmark = pytest.mark.requirement("LIBUX-10046")


@pytest.fixture
def engine(related_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    base_url = f"http://127.0.0.1:{int(listener.getsockname()[1])}"
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(related_client.app, log_level="warning"), what="the engine", sockets=[listener]
    )
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_related_prints_the_routes_json(engine: str, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] `track related` drops its flags [then] CLI parity is broken, [else stop]."""
    assert main(["track", "related", "seed", "bpm-percent", "4", "genre", "0"]) == api_cli.EXIT_OK, (
        capsys.readouterr().err
    )
    body = json.loads(capsys.readouterr().out)
    assert body["seed_stable_id"] == "seed"
    assert body["criteria"]["bpm_percent"] == 4 and body["criteria"]["genre"] is False


@pytest.mark.parametrize("argv", [[], ["seed", "key", "2"], ["seed", "bpm-percent"], ["seed", "nope", "1"]])
def test_malformed_calls_print_usage(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a malformed `track related` reaches the engine [then] usage is broken, [else stop]."""
    assert main(["track", "related", *argv]) == 1
    assert "opendj track related <stable_id>" in capsys.readouterr().err


def test_browser_related_verb_builds_the_bus_command() -> None:
    assert parse_invocation(["browser_related", "true"]).command == {"type": "browser_related", "active": True}
    assert parse_invocation(["browser_related", "false"]).command == {"type": "browser_related", "active": False}


def test_browser_related_criteria_verbs_build_the_bus_commands() -> None:
    assert parse_invocation(["browser_related_criteria", "artist=true", "bpm_percent=4"]).command == {
        "type": "browser_related_criteria",
        "bpm_percent": 4,
        "artist": True,
    }
    assert parse_invocation(["browser_related_criteria_reset"]).command == {"type": "browser_related_criteria_reset"}
    with pytest.raises(InvocationError):
        parse_invocation(["browser_related_criteria", "bpm_percent=21"])
    with pytest.raises(InvocationError):
        parse_invocation(["browser_related_criteria", "bpm_percent=4.5"])
