"""The title-fix API key must be owner-only to be used, and every CLI request is bounded in time."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from apps.tags import title_fix_store
from apps.webui import title_fix_cli


@pytest.mark.requirement("TFIX-04")
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; the Windows ACL path is covered by apps.shared.private_files")
def test_a_key_file_other_accounts_can_read_is_refused_with_remediation(tmp_path: Path) -> None:
    """[if] the saved key file is group or world readable [then] resolving the key raises ConfigError, [else stop]."""
    state_db = tmp_path / "data" / "state" / "state.db"
    paths = title_fix_store.StorePaths(state_db)
    title_fix_store.save_user_key(paths, "sk-test-" + "x" * 20)
    assert title_fix_store.resolve_key(paths)[1].source == "user", "control: an owner-only key file is used"
    paths.key.chmod(0o644)
    with pytest.raises(title_fix_store.ConfigError, match="readable by other accounts"):
        title_fix_store.resolve_key(paths)
    paths.key.chmod(0o600)
    assert title_fix_store.resolve_key(paths)[1].source == "user", "control: restoring owner-only access works again"


@pytest.mark.requirement("TFIX-04")
@pytest.mark.parametrize("content", ["", "   \n"])
def test_an_empty_saved_key_file_is_refused_not_replaced_by_the_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """[if] the saved key file is empty or blank [then] resolving the key raises ConfigError, never the env key, [else stop]."""
    paths = title_fix_store.StorePaths(tmp_path / "data" / "state" / "state.db")
    paths.key.parent.mkdir(parents=True)
    paths.key.write_text(content, encoding="utf-8")
    if os.name != "nt":
        paths.key.chmod(0o600)
    monkeypatch.setenv(title_fix_store.CFG.OPENROUTER_KEY_ENV, "sk-env-" + "y" * 20)
    with pytest.raises(title_fix_store.ConfigError, match="is empty"):
        title_fix_store.resolve_key(paths)
    paths.key.unlink()
    assert title_fix_store.resolve_key(paths)[1].source == "env", "control: with no saved key the environment key is used"


@pytest.mark.requirement("TFIX-06")
def test_every_cli_request_carries_a_finite_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the CLI sends a request [then] urlopen gets a finite timeout, [else stop]."""
    seen: list[float | None] = []

    class Reply:
        def __enter__(self) -> Reply:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(_request: Any, timeout: float | None = None) -> Reply:
        seen.append(timeout)
        return Reply()

    monkeypatch.setattr(title_fix_cli, "urlopen", fake_urlopen)
    title_fix_cli._request("http://127.0.0.1:1", "GET", "/title-fix/config")
    assert seen == [title_fix_cli.CFG.REQUEST_TIMEOUT_S] and seen[0] is not None, "if no timeout is passed a stalled daemon hangs the CLI"


@pytest.mark.requirement("TFIX-06")
def test_a_wait_poll_never_outlasts_the_remaining_wait_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan --wait polls near its deadline [then] the poll timeout is capped by the remaining budget, [else stop]."""
    monkeypatch.setattr(title_fix_cli.CFG, "WAIT_MAX_S", 5)
    monkeypatch.setattr(title_fix_cli.CFG, "POLL_S", 0.0)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    timeouts: list[float] = []

    def poll(_url: str, _method: str, _path: str, body: Any = None, *, timeout_s: float | None = None) -> dict[str, Any]:
        assert timeout_s is not None
        timeouts.append(timeout_s)
        return {"scan_id": "s", "state": "done", "processed": 1, "requested": 1, "cost_usd": 0.0, "spend_cap_usd": 1.0}

    monkeypatch.setattr(title_fix_cli, "_request", poll)
    running = {"scan_id": "s", "state": "running", "processed": 0, "requested": 1, "cost_usd": 0.0, "spend_cap_usd": 1.0}
    assert title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)["state"] == "done"
    assert timeouts and timeouts[0] <= 5.0 < title_fix_cli.CFG.REQUEST_TIMEOUT_S, (
        f"if the poll timeout ignores the remaining budget then --wait can outlast its limit: {timeouts}")


@pytest.mark.requirement("TFIX-06")
def test_a_waited_scan_that_ends_failed_is_a_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan --wait ends in the failed state [then] the CLI raises with the recorded reason, [else stop]."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    failed = {"scan_id": "s1", "state": "failed", "error": "scan process exited before finishing",
              "processed": 0, "requested": 1, "cost_usd": 0.0, "spend_cap_usd": 1.0}
    monkeypatch.setattr(title_fix_cli, "_request", lambda *_a, **_k: failed)
    running = {**failed, "state": "running"}
    with pytest.raises(RuntimeError, match="scan s1 failed: scan process exited before finishing"):
        title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)
    done = {**failed, "state": "done"}
    monkeypatch.setattr(title_fix_cli, "_request", lambda *_a, **_k: done)
    assert title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)["state"] == "done", "control: a done scan still returns"


@pytest.mark.requirement("TFIX-06")
def test_a_wait_that_is_past_its_deadline_after_sleeping_makes_no_further_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the wait is already past its deadline after the sleep [then] no request is sent, [else stop]."""
    monkeypatch.setattr(title_fix_cli.CFG, "WAIT_MAX_S", 1)
    clock = iter([0.0, 0.5, 5.0, 5.0])  # start, loop check, after the sleep, spare
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    requests: list[str] = []
    def record_request(*_a: Any, **_k: Any) -> dict[str, Any]:
        requests.append("sent")
        return {}

    monkeypatch.setattr(title_fix_cli, "_request", record_request)
    running = {"scan_id": "s", "state": "running", "processed": 0, "requested": 1, "cost_usd": 0.0, "spend_cap_usd": 1.0}
    with pytest.raises(RuntimeError, match="still running after"):
        title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)
    assert requests == [], "if an expired budget is floored to one second then --wait outlasts its limit by another request"


@pytest.mark.requirement("TFIX-06")
def test_an_unreachable_daemon_is_a_one_line_runtime_error_not_a_traceback() -> None:
    """[if] the daemon is not listening [then] the CLI raises RuntimeError with the address, [else stop]."""
    with pytest.raises(RuntimeError, match=r"cannot reach http://127\.0\.0\.1:1"):
        title_fix_cli._request("http://127.0.0.1:1", "GET", "/title-fix/config", timeout_s=2.0)
