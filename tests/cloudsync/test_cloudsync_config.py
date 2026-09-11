"""Persisted CloudSync config: file, env overrides, HTTP routes and the CLI twin.

Contract: ``apps/sync_hub/config.py`` (file shape, env-wins precedence),
``apps/webui/server/routes/cloudsync_config.py`` (GET/PUT) and
``apps/sync_hub/config_cli.py`` (``python -m apps.sync_hub config``). Real
files in a temp data dir, the real webui app over ASGI; nothing is mocked.

  - [if] PUT config then GET does not return what was saved [then] broken, [else stop].
  - [if] an env override wins silently without naming its source [then] broken, [else stop].
  - [if] a malformed config file reads as off instead of failing [then] broken, [else stop].
  - [if] the CLI twin prints a different object than GET [then] broken, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import config as sync_config
from apps.sync_hub import config_cli, maintenance
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("CAT-04")

_HUB = "http://hub.example.test:8686"
_OTHER_HUB = "https://other-hub.example.test"
_PATH = "/api/v1/cloudsync/config"


@pytest.fixture(autouse=True)
def _no_cloudsync_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's shell must not steer precedence in these tests."""
    for name in (sync_config.SCHEDULER_ENV, sync_config.ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def http(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "state" / "state.db"
    state_db.open_rw(db_path, apply_schema=True).close()
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="cloudsync-config-test",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    # A loopback peer and Host: the routes are local-operator only, and
    # TestClient's default ``testserver`` Host is refused by that guard.
    with TestClient(app, base_url="http://127.0.0.1:8686", client=("127.0.0.1", 50123)) as client:
        yield client


def _body(enabled: bool, hub_url: str | None, machine_name: str | None) -> dict[str, Any]:
    return {"enabled": enabled, "hub_url": hub_url, "machine_name": machine_name}


def test_put_then_get_returns_the_saved_config(http: TestClient, tmp_path: Path) -> None:
    """[if] GET after PUT differs from the saved body or file [then] broken, [else stop]."""
    saved = _body(True, _HUB, "silver")
    put = http.put(_PATH, json=saved)
    assert put.status_code == 200, put.text

    got = http.get(_PATH)
    assert got.status_code == 200
    body = got.json()
    assert body["file"] == saved
    assert body["effective"] == {
        **saved,
        "configured": True,
        "enabled_source": "file",
        "hub_url_source": "file",
    }
    assert body["path"] == str(tmp_path / sync_config.CONFIG_FILENAME)
    assert json.loads((tmp_path / sync_config.CONFIG_FILENAME).read_text()) == saved


def test_get_with_no_file_reports_default_off(http: TestClient) -> None:
    """[if] a machine with no config file reads as configured [then] broken, [else stop]."""
    body = http.get(_PATH).json()
    assert body["file"] is None
    assert body["effective"]["configured"] is False
    assert body["effective"]["enabled_source"] == "default"


def test_env_override_wins_and_the_payload_says_so(
    http: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] env loses to the file, or the winner is unnamed [then] broken, [else stop]."""
    assert http.put(_PATH, json=_body(False, _HUB, None)).status_code == 200
    monkeypatch.setenv(sync_config.SCHEDULER_ENV, "1")
    monkeypatch.setenv(sync_config.ENDPOINT_ENV, _OTHER_HUB)

    body = http.get(_PATH).json()
    assert body["file"]["hub_url"] == _HUB  # the file is untouched
    effective = body["effective"]
    assert (effective["enabled"], effective["enabled_source"]) == (True, "env")
    assert (effective["hub_url"], effective["hub_url_source"]) == (_OTHER_HUB, "env")

    status = http.get("/api/v1/cloudsync/status").json()
    assert (status["enabled_source"], status["endpoint_source"]) == ("env", "env")
    assert status["endpoint"] == _OTHER_HUB


def test_env_zero_disables_a_file_enabled_machine(
    http: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] SCHEDULER=0 cannot switch off a file-enabled machine [then] broken, [else stop]."""
    assert http.put(_PATH, json=_body(True, _HUB, None)).status_code == 200
    monkeypatch.setenv(sync_config.SCHEDULER_ENV, "0")
    effective = http.get(_PATH).json()["effective"]
    assert (effective["enabled"], effective["configured"]) == (False, False)
    assert effective["enabled_source"] == "env"


def test_malformed_file_fails_loudly(http: TestClient, tmp_path: Path) -> None:
    """[if] a malformed or extra-key file reads as off [then] broken, [else stop]."""
    path = tmp_path / sync_config.CONFIG_FILENAME
    path.write_text("{not json", encoding="utf-8")
    response = http.get(_PATH)
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "CLOUDSYNC_CONFIG_INVALID"

    path.write_text(json.dumps({**_body(False, None, None), "surprise": 1}), encoding="utf-8")
    with pytest.raises(sync_config.CloudSyncConfigError, match="surprise"):
        sync_config.read_config(tmp_path)

    # Status does not crash on it either: it SHOWS the error as the reason.
    status = http.get("/api/v1/cloudsync/status").json()
    assert status["enabled"] is False
    assert "invalid" in status["reason"]


def test_invalid_scheduler_env_value_raises(tmp_path: Path) -> None:
    """[if] MDT_CLOUDSYNC_SCHEDULER=true is read as a boolean guess [then] broken, [else stop]."""
    with pytest.raises(sync_config.CloudSyncConfigError, match="use 1 or 0"):
        sync_config.resolve_config(tmp_path, env={sync_config.SCHEDULER_ENV: "true"})


@pytest.mark.parametrize(
    "body",
    [
        _body(True, None, None),
        _body(False, "ftp://hub.example.test", None),
        _body(False, "not a url", None),
        _body(False, None, "   "),
        {"enabled": "yes", "hub_url": None, "machine_name": None},
    ],
)
def test_put_refuses_an_invalid_body(
    http: TestClient, tmp_path: Path, body: dict[str, Any]
) -> None:
    """[if] PUT persists an unusable config [then] broken, [else stop]."""
    response = http.put(_PATH, json=body)
    assert response.status_code == 422, response.text
    assert not (tmp_path / sync_config.CONFIG_FILENAME).exists()


def test_cli_set_and_show_match_the_http_payload(
    http: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] config set/show print a different object than GET [then] broken, [else stop]."""
    exit_code = maintenance.main(
        ["config", "set", "--data-dir", str(tmp_path), "--enabled", "--hub", _HUB, "--name", "air"]
    )
    assert exit_code == 0
    printed_by_set = json.loads(capsys.readouterr().out)

    assert maintenance.main(["config", "show", "--data-dir", str(tmp_path)]) == 0
    printed_by_show = json.loads(capsys.readouterr().out)

    over_http = http.get(_PATH).json()
    assert printed_by_set == printed_by_show == over_http
    assert over_http["file"] == _body(True, _HUB, "air")


def test_cli_set_merges_and_refuses_without_touching_the_file(tmp_path: Path) -> None:
    """[if] a refused set rewrites the file or set drops fields [then] broken, [else stop]."""
    config_cli.set_config(tmp_path, enabled=None, hub_url=_HUB, machine_name="bifrost2")
    config_cli.set_config(tmp_path, enabled=True, hub_url=None, machine_name=None)
    assert sync_config.read_config(tmp_path) == sync_config.CloudSyncConfig(
        enabled=True, hub_url=_HUB, machine_name="bifrost2"
    )
    before = (tmp_path / sync_config.CONFIG_FILENAME).read_bytes()

    with pytest.raises(sync_config.CloudSyncConfigError, match="refused"):
        config_cli.set_config(tmp_path, enabled=None, hub_url="nope", machine_name=None)
    with pytest.raises(sync_config.CloudSyncConfigError, match="at least one"):
        config_cli.set_config(tmp_path, enabled=None, hub_url=None, machine_name=None)

    assert (tmp_path / sync_config.CONFIG_FILENAME).read_bytes() == before
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_refuses_a_missing_data_dir(tmp_path: Path) -> None:
    """[if] a typo'd data dir is silently created by a config write [then] broken, [else stop]."""
    missing = tmp_path / "no-such-dir"
    with pytest.raises(sync_config.CloudSyncConfigError, match="not a directory"):
        sync_config.write_config(
            missing, sync_config.CloudSyncConfig(enabled=False, hub_url=None, machine_name=None)
        )
    assert not missing.exists()
