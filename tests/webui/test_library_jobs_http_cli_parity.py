"""HTTP / CLI response parity for library stems jobs (PERFBATCH-05, issue #13220).

[if] HTTP and the CLI enqueue the same stems job [then] both serialize the same
job shape, including ``state`` and ``position``, else stop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.analysis import queue_cli
from tests.webui.test_library_jobs_routes import _seed_library

# Fields every library-job item must carry on every surface.
REQUIRED_ITEM_FIELDS: frozenset[str] = frozenset({"lane", "stable_id", "state", "position"})
# Wall-clock stamps differ between two runs; their presence is compared, not their values.
TIMESTAMP_FIELDS: frozenset[str] = frozenset({"enqueued_at", "started_at", "finished_at"})
SELECTION: list[str] = ["b", "a"]


def _http_client(root: Path) -> TestClient:
    """Build a daemon test client over its own temp ``state.db`` and stems root."""
    from apps.webui.server.app import create_app

    root.mkdir(parents=True, exist_ok=True)
    db = root / "state.db"
    _seed_library(db, ["a", "b"])
    app = create_app()
    app.state.analysis_db_path = db
    app.state.stem_roots = (root / "stems",)
    return TestClient(app)


def _cli_json(root: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    """Run ``queue_cli`` with ``--json`` against ``root/state.db`` and parse its stdout."""
    capsys.readouterr()
    code = queue_cli.main(["--db", str(root / "state.db"), "--json", *argv])
    assert code == queue_cli.EXIT_OK
    return json.loads(capsys.readouterr().out)


def _cli_enqueue(root: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """Enqueue ``SELECTION`` on the stems lane through the CLI."""
    root.mkdir(parents=True, exist_ok=True)
    _seed_library(root / "state.db", ["a", "b"])
    argv = ["user-enqueue", "--lane", "stems", "--placement", "next", "--stems-root", str(root / "stems")]
    for stable_id in SELECTION:
        argv += ["--stable-id", stable_id]
    return _cli_json(root, capsys, *argv)


def assert_library_job_parity(http_body: dict[str, Any], cli_body: dict[str, Any]) -> None:
    """Assert an HTTP and a CLI library-jobs payload describe the same jobs in the same shape."""
    assert set(http_body) == set(cli_body), (sorted(http_body), sorted(cli_body))
    assert http_body["lane"] == cli_body["lane"]
    http_items, cli_items = http_body["items"], cli_body["items"]
    assert len(http_items) == len(cli_items)
    for surface, items in (("http", http_items), ("cli", cli_items)):
        for item in items:
            missing = REQUIRED_ITEM_FIELDS - set(item)
            assert not missing, f"{surface} omits {sorted(missing)} for {item.get('stable_id')}"
            assert item["state"], f"{surface} has empty state for {item['stable_id']}"
            assert isinstance(item["position"], int), f"{surface} position is not an int for {item['stable_id']}"
    for http_item, cli_item in zip(http_items, cli_items, strict=True):
        assert set(http_item) == set(cli_item), (sorted(http_item), sorted(cli_item))
        stable = {k: v for k, v in http_item.items() if k not in TIMESTAMP_FIELDS}
        assert stable == {k: v for k, v in cli_item.items() if k not in TIMESTAMP_FIELDS}
        for key in TIMESTAMP_FIELDS & set(http_item):
            assert (http_item[key] is None) == (cli_item[key] is None), key


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
def test_http_and_cli_enqueue_return_the_same_stems_job_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] HTTP and the CLI enqueue the same stems job [then] their responses match field for field, [else stop]."""
    http = _http_client(tmp_path / "http" / "state")
    resp = http.post(
        "/api/v1/library-jobs/enqueue",
        json={"lane": "stems", "stable_ids": SELECTION, "placement": "next"},
    )
    assert resp.status_code == 201, resp.text
    http_body = resp.json()
    cli_body = _cli_enqueue(tmp_path / "cli" / "state", capsys)

    assert_library_job_parity(http_body, cli_body)
    assert [i["stable_id"] for i in http_body["items"]] == SELECTION
    assert all(i["state"] == "pending" for i in http_body["items"])


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
def test_http_and_cli_list_report_the_same_state_and_position(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the same stems jobs are queued [then] HTTP GET and CLI user-list report the same state and position, [else stop]."""
    http_root = tmp_path / "http" / "state"
    cli_root = tmp_path / "cli" / "state"
    http = _http_client(http_root)
    assert http.post("/api/v1/library-jobs/enqueue", json={"lane": "stems", "stable_ids": SELECTION}).status_code == 201
    _cli_enqueue(cli_root, capsys)

    http_list = http.get("/api/v1/library-jobs", params={"lane": "stems"}).json()
    cli_list = _cli_json(cli_root, capsys, "user-list", "--lane", "stems")

    assert_library_job_parity(http_list, cli_list)
    assert http_list["counts"] == cli_list["counts"]
    positions = [i["position"] for i in http_list["items"]]
    assert positions == sorted(positions)


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
@pytest.mark.parametrize("field", ["state", "position"])
@pytest.mark.parametrize("surface", ["http", "cli"])
def test_parity_assertion_fails_when_a_surface_omits_state_or_position(surface: str, field: str) -> None:
    """[if] either surface omits job state or position [then] the shared parity assertion fails, [else stop]."""
    item = {
        "lane": "stems",
        "stable_id": "a",
        "state": "pending",
        "reason": None,
        "detail": None,
        "position": 0,
        "attempts": 0,
        "enqueued_at": "2026-11-13T00:00:00Z",
        "started_at": None,
        "finished_at": None,
    }
    broken = {k: v for k, v in item.items() if k != field}
    good_body = {"lane": "stems", "items": [item], "already_running": []}
    broken_body = {"lane": "stems", "items": [broken], "already_running": []}
    http_body, cli_body = (broken_body, good_body) if surface == "http" else (good_body, broken_body)

    with pytest.raises(AssertionError, match=field):
        assert_library_job_parity(http_body, cli_body)
