"""SET-12 v1 fallback: a build can switch set recording off, cleanly.

Recording ships in v1 only if CORE's packaged verify passes. If it does not,
it must ship DISABLED, not broken, and with no code change: one flag,
``sets.recording`` (default ON), set off in a profile or feature-flags.json.

[if] the flag is on (the declared default) [then] REC starts as before, [else stop].
[if] the flag is off [then] POST /api/sets/recorder/start answers 403 with
    "set recording is disabled in this build", and nothing starts, [else stop].
[if] the flag is off [then] the CLI's ``rec start`` fails with that same reason
    (it surfaces the route's non-2xx body), [else stop].
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.feature_flags import FLAGS, load_flags
from apps.sets import rec_cli
from apps.sets.api import RECORDING_DISABLED, RECORDING_FLAG_ID, router
from apps.sets.recorder_service import RecorderService

pytestmark = pytest.mark.requirement("SET-12")

START = {"source": "none", "sources": []}


def _client(tmp_path: Path, overrides: dict[str, bool] | None) -> tuple[TestClient, RecorderService]:
    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()
    if overrides is not None:
        (flag_dir / "feature-flags.json").write_text(json.dumps(overrides))
    service = RecorderService(
        sets_root=tmp_path / "sets", db_path=tmp_path / "sets" / "sets.db", capture_enabled=False
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.state.feature_flags = load_flags(flag_dir)
    app.include_router(router)
    return TestClient(app), service


def test_the_flag_is_declared_and_on_by_default() -> None:
    [flag] = [f for f in FLAGS if f.flag_id == RECORDING_FLAG_ID]
    assert flag.default is True


def test_with_the_flag_on_rec_starts(tmp_path: Path) -> None:
    client, service = _client(tmp_path, None)
    with client:
        started = client.post("/api/sets/recorder/start", json=START)
        assert started.status_code == 201, started.text
        assert service.status()["active"] is True


def test_with_the_flag_off_rec_is_refused_and_nothing_starts(tmp_path: Path) -> None:
    client, service = _client(tmp_path, {RECORDING_FLAG_ID: False})
    with client:
        refused = client.post("/api/sets/recorder/start", json=START)
        assert refused.status_code == 403
        assert refused.json()["detail"] == RECORDING_DISABLED
        assert "disabled in this build" in RECORDING_DISABLED
        assert service.status()["active"] is False


def test_the_cli_fails_with_the_same_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rec start`` turns the route's 403 body into its failure message."""
    import io
    import urllib.error
    import urllib.request

    def refuse(request: urllib.request.Request, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {}, io.BytesIO(json.dumps({"detail": RECORDING_DISABLED}).encode())
        )

    monkeypatch.setattr(rec_cli.urllib.request, "urlopen", refuse)
    with pytest.raises(rec_cli.RecCommandFailed, match="disabled in this build"):
        rec_cli._call("http://127.0.0.1:1", "POST", "/api/sets/recorder/start", START)
