"""``opendj track auto-gain``: the CLI twin of ``/api/v1/auto-gain`` (AUTOGAIN-06).

[if] an agent runs auto-gain verbs [then] it reads, sets and clears the user value, [else stop].

Runs the real CLI entry point against a real in-process engine app over a real
socket, on the hermetic library from ``tests/webui/test_auto_gain_route.py``.

Regression lines:
- if `auto-gain <id>` prints anything but the route's own JSON then broken
- if `auto-gain <id> set <dB>` does not make the user's value win then broken
- if `auto-gain <id> clear` does not hand the track back to the analysis then broken
"""
from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread
from tests.webui.test_auto_gain_route import LOUD, _make_state_db

pytestmark = pytest.mark.requirement("AUTOGAIN-06")


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    state_path = tmp_path / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    _make_state_db(state_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=SqliteBackend(state_path),
        state_db_path=str(state_path),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )
    app.state.data_dir = tmp_path
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the engine",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> dict:
    assert main(["track", *argv]) == api_cli.EXIT_OK, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def test_read_set_and_clear(engine: str, capsys: pytest.CaptureFixture[str]) -> None:
    read = _cli(capsys, "auto-gain", LOUD)
    assert (read["status"], read["effective_offset_db"]) == ("analysed", -6.0)
    user = _cli(capsys, "auto-gain", LOUD, "set", "-1.5")
    assert (user["status"], user["effective_offset_db"]) == ("user", -1.5)
    assert _cli(capsys, "auto-gain", LOUD)["effective_offset_db"] == -1.5
    cleared = _cli(capsys, "auto-gain", LOUD, "clear")
    assert (cleared["status"], cleared["effective_offset_db"]) == ("analysed", -6.0)


@pytest.mark.parametrize(
    "argv",
    [["auto-gain"], ["auto-gain", LOUD, "set"], ["auto-gain", LOUD, "set", "loud"], ["auto-gain", LOUD, "wipe"]],
)
def test_malformed_verbs_are_usage_errors(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["track", *argv]) == 1
    assert "opendj track auto-gain" in capsys.readouterr().err
