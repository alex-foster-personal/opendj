"""LIBMX-12: smartlist CRUD through documented ``opendj api`` invocations.

[if] an agent wants to create a smartlist from a script [then] one documented opendj invocation does it, [else stop]
[if] opendj --help (or its verb listing) is inspected [then] smartlist operations appear alongside other library verbs, [else stop]
"""
from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import _parser, main
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("LIBMX-12")

_BPM_RULE = {"field": "bpm", "op": ">=", "value": 120}
_ENERGY_RULE = {"field": "energy", "op": ">=", "value": 8}


@pytest.fixture
def library_daemon(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    conn.close()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        mount_frontend=False,
        enable_cors=False,
    )
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the library daemon",
        sockets=[listener],
    )
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, db_path
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


def _patch_backend(
    monkeypatch: pytest.MonkeyPatch, base_url: str,
) -> None:
    monkeypatch.setattr(
        api_cli, "resolve_backend_base_url", lambda environ=None: base_url,
    )


def test_api_post_creates_smartlist(
    library_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _db = library_daemon
    _patch_backend(monkeypatch, base_url)
    code = main([
        "api", "POST", "/api/v1/smartlists",
        "--json", json.dumps({"name": "CLI House", "rule": _BPM_RULE}),
    ])
    assert code == api_cli.EXIT_OK
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "CLI House"
    assert created["id"]

    list_code = main(["api", "GET", "/api/v1/smartlists"])
    assert list_code == api_cli.EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert any(row["id"] == created["id"] for row in rows)


def test_api_get_and_delete_smartlist(
    library_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _db = library_daemon
    _patch_backend(monkeypatch, base_url)
    create = httpx.post(
        f"{base_url}/api/v1/smartlists",
        json={"name": "To Delete", "rule": _BPM_RULE},
        timeout=30,
    )
    assert create.status_code == 201
    smartlist_id = create.json()["id"]

    list_code = main(["api", "GET", "/api/v1/smartlists"])
    assert list_code == api_cli.EXIT_OK
    capsys.readouterr()

    get_code = main(["api", "GET", f"/api/v1/smartlists/{smartlist_id}"])
    assert get_code == api_cli.EXIT_OK
    one = json.loads(capsys.readouterr().out)
    assert one["id"] == smartlist_id

    delete_code = main(["api", "DELETE", f"/api/v1/smartlists/{smartlist_id}"])
    assert delete_code == api_cli.EXIT_OK
    assert capsys.readouterr().out == ""

    after_code = main(["api", "GET", "/api/v1/smartlists"])
    assert after_code == api_cli.EXIT_OK
    remaining = json.loads(capsys.readouterr().out)
    assert all(row["id"] != smartlist_id for row in remaining)

    missing_code = main(["api", "DELETE", "/api/v1/smartlists/missing-id"])
    assert missing_code == api_cli.EXIT_FAILED


def test_api_put_smartlist_with_if_match(
    library_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _db = library_daemon
    _patch_backend(monkeypatch, base_url)
    create = httpx.post(
        f"{base_url}/api/v1/smartlists",
        json={"name": "To Update", "rule": _BPM_RULE},
        timeout=30,
    )
    assert create.status_code == 201
    smartlist_id = create.json()["id"]
    etag = create.headers["etag"]

    put_code = main([
        "api", "PUT", f"/api/v1/smartlists/{smartlist_id}",
        "-H", f"If-Match: {etag}",
        "--json", json.dumps({"rule": _ENERGY_RULE}),
    ])
    assert put_code == api_cli.EXIT_OK
    updated = json.loads(capsys.readouterr().out)
    assert updated["rule"] == _ENERGY_RULE

    stale_code = main([
        "api", "PUT", f"/api/v1/smartlists/{smartlist_id}",
        "-H", f"If-Match: {etag}",
        "--json", json.dumps({"rule": _BPM_RULE}),
    ])
    assert stale_code == api_cli.EXIT_CONFLICT


def test_help_lists_smartlist_ops_alongside_playlists() -> None:
    help_text = _parser().format_help()
    assert "opendj api POST /api/v1/smartlists" in help_text
    assert "GET /api/v1/smartlists" in help_text
    assert "PUT /api/v1/smartlists" in help_text
    assert "DELETE /api/v1/smartlists" in help_text
    assert "/api/v1/playlists" in help_text


def test_list_verbs_footer_and_json_bus_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--list-verbs"]) == api_cli.EXIT_OK
    text_out = capsys.readouterr().out
    assert "play" in text_out
    assert "smartlists" in text_out

    assert main(["--json", "--list-verbs"]) == api_cli.EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list)
    assert all("smartlist" not in row.get("command", "") for row in rows)
