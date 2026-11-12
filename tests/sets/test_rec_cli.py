"""Sets recorder CLI: explicit start source and status-derived recovery.

[if] rec start omits --source [then] argparse usage and no HTTP, [else stop].
[if] loopback or external omits --device-name [then] fail before HTTP, [else stop].
[if] recovery is requested [then] only the reported stale owner is recovered, [else stop].
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from apps.sets import rec_cli
from apps.sets.__main__ import main as sets_main

pytestmark = [
    pytest.mark.requirement("SET-12844"),
]


@pytest.mark.requirement("SET-12835")
def test_start_without_source_exits_usage_and_skips_http(capsys, monkeypatch) -> None:
    """[if] rec start has no --source [then] exit 2 and zero HTTP, [else stop]."""
    calls: list[tuple] = []

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(rec_cli, "_call", fake_call)
    with pytest.raises(SystemExit) as exc:
        sets_main(["rec", "start", "--base-url", "http://127.0.0.1:1"])
    assert exc.value.code == 2
    assert calls == []
    assert "--source" in capsys.readouterr().err


@pytest.mark.requirement("SET-12835")
def test_start_with_invalid_source_exits_usage_and_skips_http(capsys, monkeypatch) -> None:
    """[if] --source is not a RECORD_SOURCES choice [then] exit 2 and zero HTTP, [else stop]."""
    calls: list[tuple] = []

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(rec_cli, "_call", fake_call)
    with pytest.raises(SystemExit) as exc:
        sets_main(
            [
                "rec",
                "start",
                "--base-url",
                "http://127.0.0.1:1",
                "--source",
                "microphone",
            ]
        )
    assert exc.value.code == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "microphone" in err or "invalid choice" in err.lower()


@pytest.mark.requirement("SET-12835")
@pytest.mark.parametrize("source", ["loopback", "external"])
def test_start_device_source_without_device_name_fails_before_http(source: str, capsys, monkeypatch) -> None:
    """[if] loopback or external omits --device-name [then] no HTTP, [else stop]."""
    calls: list[tuple] = []

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(rec_cli, "_call", fake_call)
    code = sets_main(["rec", "start", "--base-url", "http://127.0.0.1:1", "--source", source])
    assert code == 1
    assert calls == []
    assert "needs --device-name" in capsys.readouterr().err


def test_recover_posts_status_identity(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    finalized = {"active": False, "session_id": None, "pid": None}

    def call(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        assert base_url == "http://recorder"
        calls.append((method, path, body))
        if method == "GET":
            return {"active": True, "recoverable": True, "session_id": "stale-set", "pid": 123}
        return finalized

    monkeypatch.setattr(rec_cli, "_call", call)
    parser = argparse.ArgumentParser()
    rec_cli.add_parser(parser.add_subparsers(required=True))
    args = parser.parse_args(["rec", "recover", "--base-url", "http://recorder"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out) == finalized
    assert calls == [
        ("GET", "/api/sets/recorder", None),
        ("POST", "/api/sets/recorder/stale-set/recover", {"expected_pid": 123}),
    ]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ({"active": False}, "nothing to recover"),
        ({"active": True, "recoverable": False, "session_id": "live-set", "pid": 42}, "live-set"),
        *[
            ({"active": True, "recoverable": True, "session_id": session, "pid": pid}, "invalid")
            for session, pid in [(None, 42), ("", 42), ("set", None), ("set", 0), ("set", True), ("set", "42")]
        ],
    ],
)
def test_recover_refuses_before_post(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: dict[str, Any], reason: str
) -> None:
    def call(base_url: str, method: str, path: str) -> dict[str, Any]:
        assert method == "GET"
        return status

    monkeypatch.setattr(rec_cli, "_call", call)
    assert rec_cli.dispatch(argparse.Namespace(rec_cmd="recover", base_url="http://recorder")) == 1
    error = capsys.readouterr().err
    assert reason in error
    if status.get("recoverable") is False:
        assert "42" in error


def test_recover_does_not_retry_conflict(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] recover POST gets 409 [then ⛔️] stderr keeps detail and exit is 1, [else stop]."""
    calls: list[str] = []

    def call(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append(method)
        if method == "GET":
            return {"active": True, "recoverable": True, "session_id": "set", "pid": 123}
        raise rec_cli.RecCommandFailed('409 {"detail":"recorder ownership changed; refresh before recovery"}')

    monkeypatch.setattr(rec_cli, "_call", call)
    assert rec_cli.dispatch(argparse.Namespace(rec_cmd="recover", base_url="http://recorder")) == 1
    error = capsys.readouterr().err
    assert "409" in error
    assert "ownership changed" in error
    assert calls == ["GET", "POST"]
