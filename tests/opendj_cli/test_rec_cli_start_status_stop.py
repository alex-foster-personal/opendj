"""``opendj rec start|status|stop`` is the installed twin of ``python -m apps.sets rec`` (issue #11807).

[if] opendj rec start|status|stop runs [then] it delegates to apps.sets rec_cli.dispatch over a fake transport, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.opendj_cli import rec_cli
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.api_cli import _BackendTarget
from apps.sets import rec_cli as sets_rec_cli

pytestmark = pytest.mark.requirement("SET-11807")

_IDLE: dict[str, Any] = {"active": False, "session_id": None, "capture": None}


def _wire(
    monkeypatch: pytest.MonkeyPatch, replies: dict[tuple[str, str], list[dict[str, Any]]]
) -> list[tuple[str, str, Any]]:
    """Point the CLI at a fake engine whose HTTP answers come from ``replies``; return the call log."""
    calls: list[tuple[str, str, Any]] = []

    def resolve(*, lock_path: Path | None) -> _BackendTarget:
        """Return a fixed engine origin without reading a lock file."""
        return _BackendTarget("http://engine")

    def call(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record the request and pop the next scripted reply."""
        assert base_url == "http://engine"
        calls.append((method, path, body))
        return replies[(method, path)].pop(0)

    monkeypatch.setattr(rec_cli, "resolve_backend_base_url", resolve)
    monkeypatch.setattr(sets_rec_cli, "_call", call)
    return calls


def test_status_prints_recorder_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Status prints the GET /api/sets/recorder body and exits 0.

    [if] opendj rec status runs [then] stdout is the recorder JSON and exit 0 [else stop].
    """
    calls = _wire(monkeypatch, {("GET", "/api/sets/recorder"): [dict(_IDLE)]})
    assert main(["rec", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == _IDLE
    assert calls == [("GET", "/api/sets/recorder", None)]


def test_start_posts_start_body_and_waits_for_recording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Start posts rec_cli.start_body and only returns once capture is recording.

    [if] opendj rec start --source master runs [then] it POSTs start_body and polls past starting [else stop].
    """
    monkeypatch.setattr(sets_rec_cli.time, "sleep", lambda _s: None)
    recording = {"active": True, "session_id": "s1", "capture": "recording"}
    calls = _wire(
        monkeypatch,
        {
            ("POST", "/api/sets/recorder/start"): [{"active": True, "session_id": "s1", "capture": "starting"}],
            ("GET", "/api/sets/recorder"): [recording],
        },
    )
    assert main(["rec", "start", "--source", "master"]) == 0
    assert json.loads(capsys.readouterr().out)["capture"] == "recording"
    expected_body = sets_rec_cli.start_body("master", None, ["djay_monitor", "opendj_decks"])
    assert calls == [
        ("POST", "/api/sets/recorder/start", expected_body),
        ("GET", "/api/sets/recorder", None),
    ]


@pytest.mark.parametrize("as_json", [False, True])
def test_stop_when_idle_fails_without_posting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    """Stop with nothing recording fails and never POSTs a guessed session id.

    [if] nothing is recording and opendj rec stop runs [then] exit 1 with nothing is recording [else stop].
    """
    calls = _wire(monkeypatch, {("GET", "/api/sets/recorder"): [dict(_IDLE)]})
    argv = ["rec", "stop"]
    assert main(["--json", *argv] if as_json else argv) == 1
    output = capsys.readouterr()
    assert "nothing is recording" in output.err
    assert [method for method, _path, _body in calls] == ["GET"]


def test_stop_posts_the_active_session(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Stop posts to the session the engine reports as recording.

    [if] a session is recording and opendj rec stop runs [then] it POSTs that session's stop [else stop].
    """
    calls = _wire(
        monkeypatch,
        {
            ("GET", "/api/sets/recorder"): [{"active": True, "session_id": "s1", "capture": "recording"}],
            ("POST", "/api/sets/recorder/s1/stop"): [dict(_IDLE)],
        },
    )
    assert main(["rec", "stop"]) == 0
    assert calls[-1] == ("POST", "/api/sets/recorder/s1/stop", None)


def test_start_without_source_fails_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Start without --source is a usage failure before discovery or HTTP.

    [if] --source is omitted on start [then] usage fails before any HTTP call [else stop].
    """

    def boom(*args: Any, **kwargs: Any) -> Any:
        """Fail the test if the CLI reaches the engine."""
        raise AssertionError("usage error reached the engine")

    monkeypatch.setattr(rec_cli, "resolve_backend_base_url", boom)
    monkeypatch.setattr(sets_rec_cli, "_call", boom)
    assert main(["--json", "rec", "start"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
