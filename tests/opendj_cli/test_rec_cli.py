"""Installed REC dispatch uses the shared HTTP recovery operation.

[if] opendj rec recover runs [then] it preserves recovery guards and errors, [else stop].
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

pytestmark = pytest.mark.requirement("SET-12844")


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("state", ["stale", "live", "idle", "changed"])
def test_recovery_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    as_json: bool,
    state: str,
) -> None:
    lock = tmp_path / "engine.lock"
    calls: list[str] = []

    def resolve(*, lock_path: Path | None) -> _BackendTarget:
        assert lock_path == lock
        return _BackendTarget("http://recorder")

    def call(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        assert base_url == "http://recorder"
        calls.append(method)
        if method == "GET":
            return {"active": state != "idle", "recoverable": state != "live", "session_id": "set", "pid": 123}
        assert path == "/api/sets/recorder/set/recover"
        assert body == {"expected_pid": 123}
        if state == "changed":
            raise sets_rec_cli.RecCommandFailed('409 {"detail":"recorder ownership changed"}')
        return {"active": False, "session_id": None}

    monkeypatch.setattr(rec_cli, "resolve_backend_base_url", resolve)
    monkeypatch.setattr(sets_rec_cli, "_call", call)
    argv = ["--lock", str(lock), "rec", "recover"]
    code = main(["--json", *argv] if as_json else argv)
    output = capsys.readouterr()
    assert code == (0 if state == "stale" else 1)
    assert calls == (["GET", "POST"] if state in ("stale", "changed") else ["GET"])
    if state == "stale":
        assert json.loads(output.out) == {"active": False, "session_id": None}
    else:
        message = json.loads(output.out)["error"]["message"] if as_json else output.err
        if state == "live":
            assert "123" in message and "set" in message
        elif state == "idle":
            assert "nothing to recover" in message
        elif state == "changed":
            assert '409 {"detail":"recorder ownership changed"}' in message


@pytest.mark.parametrize("rest", [[], ["recover", "extra"]])
def test_bad_usage_never_resolves_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], rest: list[str]
) -> None:
    def resolve(**kwargs: Any) -> _BackendTarget:
        raise AssertionError("bad usage attempted backend discovery")

    monkeypatch.setattr(rec_cli, "resolve_backend_base_url", resolve)
    assert main(["--json", "rec", *rest]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_missing_engine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "--lock", str(tmp_path / "missing.lock"), "rec", "recover"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "engine_not_running"


def test_help_needs_no_engine(capsys: pytest.CaptureFixture[str]) -> None:
    assert rec_cli.run(["--help"]) == 0
    assert "opendj rec recover" in capsys.readouterr().out
