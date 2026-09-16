"""Severity: red means stop, orange means read this, green means working.

Recorded Wed 16 Sep 2026 from the maintainer after a first run on a fresh Mac: the
welcome screen painted every non-passing row the same red, so "some checks
aren't so important" had no way to show, and an empty library -- where every
new user starts -- looked like a fault.

The distinction under test is per OUTCOME, not per row. The same
``library-attached`` row is advisory when it is merely empty and blocking
when the setup record is corrupt, so a test that only checked the row id
would pass while the real behavior was wrong either way.

- if an empty library holds the boot gate then a new user is locked behind a
  red screen for having no music yet -> broken.
- if a corrupt setup record is downgraded to orange then a real defect is
  painted as a normal state -> broken, and worse than the bug being fixed.
- if a row carries no explainer then hovering it teaches the user nothing ->
  broken.

[if] a preflight row fails [then] its severity follows the outcome and it is explained, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.engine_core.setup import record as setup_record
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.preflight_checks import CHECK_EXPLAINER

pytestmark = pytest.mark.requirement("PREFLIGHT-04")


def _build_client(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> TestClient:
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_db.open_rw(state_path).close()
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data_dir / "absent-master.db")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        state_db_path=str(state_path),
    )
    return TestClient(app, raise_server_exceptions=False)


def _check(body: dict, check_id: str) -> dict:
    matches = [c for c in body["checks"] if c["id"] == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id!r} row, got {matches}"
    return matches[0]


def test_empty_library_is_advisory_and_does_not_hold_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _build_client(monkeypatch, tmp_path)
    body = client.get("/api/v1/preflight").json()

    library = _check(body, "library-attached")
    assert library["status"] == "fail", "the row still reports the truth"
    assert library["severity"] == "advisory", library
    # The PRESENCE of a cleared gate, not merely the absence of the word fail.
    assert body["status"] == "pass", body
    assert body["advisories"] >= 1, body


def test_corrupt_setup_record_stays_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "setup.json").write_text("{ this is not json", encoding="utf-8")
    client = _build_client(monkeypatch, tmp_path)
    body = client.get("/api/v1/preflight").json()

    library = _check(body, "library-attached")
    assert library["status"] == "fail"
    assert library["severity"] == "blocking", library
    assert body["status"] == "fail", body


def test_every_row_carries_an_explainer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _build_client(monkeypatch, tmp_path)
    body = client.get("/api/v1/preflight").json()

    for check in body["checks"]:
        assert check["explainer"], f"{check['id']} has no hover explainer"
        assert check["explainer"] == CHECK_EXPLAINER[check["id"]]
        assert check["severity"] in {"blocking", "advisory"}


def test_engine_and_database_rows_are_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the change must not quietly downgrade the rows that matter.

    Without this, making everything advisory would satisfy the first test
    perfectly, which is the failure mode the whole change risks.
    """
    client = _build_client(monkeypatch, tmp_path)
    body = client.get("/api/v1/preflight").json()

    assert _check(body, "engine-alive")["severity"] == "blocking"
    assert _check(body, "state-db")["severity"] == "blocking"


def test_dismissed_empty_library_is_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user said "continue without importing". That is not a fault."""
    setup_record.set_dismissed(tmp_path, True)
    client = _build_client(monkeypatch, tmp_path)
    body = client.get("/api/v1/preflight").json()
    library = _check(body, "library-attached")
    assert library["status"] == "pending"
    assert library["severity"] == "advisory"
    assert body["status"] == "pass"
