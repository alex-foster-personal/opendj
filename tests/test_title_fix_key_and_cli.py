"""The title-fix API key must be owner-only to be used, and every CLI request is bounded in time."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from apps.shared.state import db as state_db
from apps.tags import title_fix
from apps.tags import title_fix_store
from apps.webui import title_fix_cli
from apps.webui.server import title_fix_service as title_fix_svc
from apps.webui.server.title_fix_core import TitleFixError

_REPO = Path(__file__).resolve().parents[1]


@pytest.mark.requirement("TFIX-04")
@pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits; the Windows ACL path is covered by apps.shared.private_files"
)
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
    assert title_fix_store.resolve_key(paths)[1].source == "env", (
        "control: with no saved key the environment key is used"
    )


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
    assert seen == [title_fix_cli.CFG.REQUEST_TIMEOUT_S] and seen[0] is not None, (
        "if no timeout is passed a stalled daemon hangs the CLI"
    )


@pytest.mark.requirement("TFIX-06")
def test_a_wait_poll_never_outlasts_the_remaining_wait_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan --wait polls near its deadline [then] the poll timeout is capped by the remaining budget, [else stop]."""
    monkeypatch.setattr(title_fix_cli.CFG, "WAIT_MAX_S", 5)
    monkeypatch.setattr(title_fix_cli.CFG, "POLL_S", 0.0)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    timeouts: list[float] = []

    def poll(
        _url: str, _method: str, _path: str, body: Any = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        assert timeout_s is not None
        timeouts.append(timeout_s)
        return {"scan_id": "s", "state": "done", "processed": 1, "requested": 1, "cost_usd": 0.0, "spend_cap_usd": 1.0}

    monkeypatch.setattr(title_fix_cli, "_request", poll)
    running = {
        "scan_id": "s",
        "state": "running",
        "processed": 0,
        "requested": 1,
        "cost_usd": 0.0,
        "spend_cap_usd": 1.0,
    }
    assert title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)["state"] == "done"
    assert timeouts and timeouts[0] <= 5.0 < title_fix_cli.CFG.REQUEST_TIMEOUT_S, (
        f"if the poll timeout ignores the remaining budget then --wait can outlast its limit: {timeouts}"
    )


@pytest.mark.requirement("TFIX-06")
def test_a_waited_scan_that_ends_failed_is_a_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan --wait ends in the failed state [then] the CLI raises with the recorded reason, [else stop]."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    failed = {
        "scan_id": "s1",
        "state": "failed",
        "error": "scan process exited before finishing",
        "processed": 0,
        "requested": 1,
        "cost_usd": 0.0,
        "spend_cap_usd": 1.0,
    }
    monkeypatch.setattr(title_fix_cli, "_request", lambda *_a, **_k: failed)
    running = {**failed, "state": "running"}
    with pytest.raises(RuntimeError, match="scan s1 failed: scan process exited before finishing"):
        title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)
    done = {**failed, "state": "done"}
    monkeypatch.setattr(title_fix_cli, "_request", lambda *_a, **_k: done)
    assert title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)["state"] == "done", (
        "control: a done scan still returns"
    )


@pytest.mark.requirement("TFIX-06")
def test_a_wait_that_is_past_its_deadline_after_sleeping_makes_no_further_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    running = {
        "scan_id": "s",
        "state": "running",
        "processed": 0,
        "requested": 1,
        "cost_usd": 0.0,
        "spend_cap_usd": 1.0,
    }
    with pytest.raises(RuntimeError, match="still running after"):
        title_fix_cli._wait_for_scan("http://127.0.0.1:1", running)
    assert requests == [], (
        "if an expired budget is floored to one second then --wait outlasts its limit by another request"
    )


@pytest.mark.requirement("TFIX-07")
def test_title_fix_modules_stay_under_the_file_size_gate() -> None:
    """[if] title_fix and title_fix_spend are measured [then] each is at most 1500 lines, [else stop]."""
    tags_dir = _REPO / "apps" / "tags"
    for name in ("title_fix.py", "title_fix_spend.py"):
        lines = len((tags_dir / name).read_text(encoding="utf-8").splitlines())
        assert lines <= 1500, f"{name} is {lines} lines"


@pytest.mark.requirement("TFIX-07")
def test_spend_budget_reserves_before_settle_and_raises_spend_cap_reached() -> None:
    """[if] the cap is nearly spent [then] reserve raises SpendCapReached, [else stop]."""
    cap = title_fix.CFG.CALL_RESERVE_USD * 1.5
    budget = title_fix.SpendBudget(cap)
    reserve = budget.reserve(title_fix.CFG.CALL_RESERVE_USD, "jev")
    budget.settle(reserve, title_fix.CFG.CALL_RESERVE_USD, "jev")
    with pytest.raises(title_fix.SpendCapReached):
        budget.reserve(title_fix.CFG.CALL_RESERVE_USD, "jev")


@pytest.mark.requirement("QUALITY-12069")
def test_split_removes_title_fix_from_the_strict_over_limit_set() -> None:
    """[if] #9686 split title_fix on main [then] spend lives in title_fix_spend and openrouter shrinks, [else stop]."""
    tags = _REPO / "apps" / "tags"
    for name in ("title_fix.py", "title_fix_spend.py"):
        assert len((tags / name).read_text(encoding="utf-8").splitlines()) <= 1500, name
    openrouter_lines = len((tags / "title_fix_openrouter.py").read_text(encoding="utf-8").splitlines())
    assert openrouter_lines < 448, "title_fix_openrouter.py must be below its pre-#11226 size on main (448 lines)"


@pytest.mark.requirement("QUALITY-12069")
def test_file_size_baseline_records_title_fix_spend_split() -> None:
    """[if] baseline burn_down is read [then] it records the title_fix spend split paydown, [else stop]."""
    burn = json.loads((_REPO / "ops" / "quality" / "baseline.json").read_text(encoding="utf-8"))["burn_down"]
    entry = burn["ISSUE_11226_TITLE_FIX_SPEND_SPLIT"]
    assert "title_fix_spend.py" in entry["what"] and "title_fix.py" in entry["what"]
    assert "file_size.over_limit_python" in entry["paid"]


@pytest.mark.requirement("TFIX-06")
def test_an_unreachable_daemon_is_a_one_line_runtime_error_not_a_traceback() -> None:
    """[if] the daemon is not listening [then] the CLI raises RuntimeError with the address, [else stop]."""
    with pytest.raises(RuntimeError, match=r"cannot reach http://127\.0\.0\.1:1"):
        title_fix_cli._request("http://127.0.0.1:1", "GET", "/title-fix/config", timeout_s=2.0)


def _fake_scan_request(
    seen: list[tuple[str, str, dict[str, Any] | None]],
) -> Any:
    def record(
        base_url: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        seen.append((method, path, body))
        return {
            "scan_id": "scan-test",
            "state": "done",
            "processed": 0,
            "requested": 0,
            "cost_usd": 0.0,
            "spend_cap_usd": 1.0,
        }

    return record


def _parse_scan_argv(argv: list[str]) -> Any:
    return title_fix_cli.build_parser().parse_args(["scan", *argv])


@pytest.mark.requirement("TRACKEDIT-12653")
def test_scan_with_one_id_posts_that_stable_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan --ids has one id [then] POST body carries that stable_id, [else stop]."""
    seen: list[tuple[str, str, dict[str, Any] | None]] = []
    monkeypatch.setattr(title_fix_cli, "_request", _fake_scan_request(seen))
    args = _parse_scan_argv(["--ids", "known-a"])
    title_fix_cli._scan(args, "http://127.0.0.1:9/api/v1")
    assert seen == [("POST", "/title-fix/scans", {"stable_ids": ["known-a"]})]


@pytest.mark.requirement("TRACKEDIT-12653")
def test_scan_with_multiple_ids_preserves_cli_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan --ids lists several ids [then] POST body lists them in order, [else stop]."""
    seen: list[tuple[str, str, dict[str, Any] | None]] = []
    monkeypatch.setattr(title_fix_cli, "_request", _fake_scan_request(seen))
    args = _parse_scan_argv(["--ids", "a", "b"])
    title_fix_cli._scan(args, "http://127.0.0.1:9/api/v1")
    assert seen == [("POST", "/title-fix/scans", {"stable_ids": ["a", "b"]})]


@pytest.mark.requirement("TRACKEDIT-12653")
def test_read_scan_tracks_refuses_unknown_stable_ids(tmp_path: Path) -> None:
    """[if] a stable id is absent from the library [then] read_scan_tracks returns 404, [else stop]."""
    state_db_path = tmp_path / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("live-one", "inferred", "Title", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(TitleFixError) as exc:
        title_fix_svc.read_scan_tracks(state_db_path, ["ghost"], None)
    assert exc.value.code == "title_fix_tracks_not_found" and exc.value.status == 404


@pytest.mark.requirement("TRACKEDIT-12653")
def test_scan_with_unknown_id_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the daemon refuses an unknown id [then] scan exits 2, [else stop]."""
    def refuse_unknown(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError('HTTP 404: {"detail":{"code":"title_fix_tracks_not_found"}}')

    monkeypatch.setattr(title_fix_cli, "_request", refuse_unknown)
    monkeypatch.setattr(
        title_fix_cli,
        "resolve_backend_port",
        lambda _cwd: 9,
    )
    assert title_fix_cli.main(["scan", "--ids", "ghost"]) == 2


@pytest.mark.requirement("TRACKEDIT-12653")
def test_scan_without_ids_or_limit_posts_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan omits --ids and --limit [then] POST body is empty, [else stop]."""
    seen: list[tuple[str, str, dict[str, Any] | None]] = []
    monkeypatch.setattr(title_fix_cli, "_request", _fake_scan_request(seen))
    args = _parse_scan_argv([])
    title_fix_cli._scan(args, "http://127.0.0.1:9/api/v1")
    assert seen == [("POST", "/title-fix/scans", {})]


@pytest.mark.requirement("TRACKEDIT-12653")
def test_scan_limit_sends_limit_not_stable_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] scan passes --limit only [then] POST body has limit and no stable_ids, [else stop]."""
    seen: list[tuple[str, str, dict[str, Any] | None]] = []
    monkeypatch.setattr(title_fix_cli, "_request", _fake_scan_request(seen))
    args = _parse_scan_argv(["--limit", "5"])
    title_fix_cli._scan(args, "http://127.0.0.1:9/api/v1")
    assert seen == [("POST", "/title-fix/scans", {"limit": 5})]


@pytest.mark.requirement("TRACKEDIT-12653")
def test_start_scan_rejects_stable_ids_and_limit_together(tmp_path: Path) -> None:
    """[if] stable_ids and limit are both set [then] start_scan refuses with 422, [else stop]."""
    state_db_path = tmp_path / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.close()

    class _Backend:
        pass

    ctx = title_fix_svc.Ctx.open(state_db_path, _Backend())  # type: ignore[arg-type]
    with pytest.raises(TitleFixError) as exc:
        title_fix_svc.start_scan(ctx, ["a"], 5, background=False)
    assert exc.value.code == "title_fix_scan_ids_and_limit" and exc.value.status == 422
