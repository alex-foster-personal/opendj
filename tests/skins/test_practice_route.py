"""requirement: SKIN-8871D, SKIN-8871E

if GET /api/v1/practice/devices does not list ddj-flx4 then broken
if GET /api/v1/practice/devices/ddj-flx4 does not return all 77 panel controls then broken
if an unknown or path-traversal device id is not a 404 then broken
if opendj practice builds a URL other than <ui>/practice/<device>[?target=] then broken
if opendj practice accepts zero or two device arguments then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.opendj_cli.practice_cli import PracticeUsage, parse_practice_args, practice_url
from apps.webui.server.routes import practice


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(practice.router, prefix="/api/v1")
    return TestClient(app)


def test_lists_flx4(client: TestClient) -> None:
    body = client.get("/api/v1/practice/devices").json()
    assert "ddj-flx4" in body["devices"]


def test_serves_flx4_panel(client: TestClient) -> None:
    r = client.get("/api/v1/practice/devices/ddj-flx4")
    assert r.status_code == 200
    doc = r.json()
    assert doc["format"] == "opendj-panel-layout/1"
    assert len(doc["controls"]) == 77


@pytest.mark.parametrize("device", ["no-such-device", "..", "ddj-400"])
def test_unknown_device_404(client: TestClient, device: str) -> None:
    # ddj-400 has a fig layout but no panel.json: a negative control for the glob.
    assert client.get(f"/api/v1/practice/devices/{device}").status_code == 404


def test_practice_url() -> None:
    assert practice_url("http://127.0.0.1:9400/", "ddj-flx4", None) == "http://127.0.0.1:9400/practice/ddj-flx4"
    assert practice_url("http://h", "ddj-flx4", "d1-play-pause") == "http://h/practice/ddj-flx4?target=d1-play-pause"


def test_parse_args() -> None:
    assert parse_practice_args(["ddj-flx4", "--target", "d2-cue"]) == ("ddj-flx4", "d2-cue")
    for bad in ([], ["a", "b"], ["ddj-flx4", "--target"]):
        with pytest.raises(PracticeUsage):
            parse_practice_args(bad)


def test_unknown_lock_is_no_engine_not_a_crash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from apps.opendj_cli import EXIT_NO_ENGINE
    from apps.opendj_cli.practice_cli import run

    assert run(["ddj-flx4", "--json"], as_json=False, lock=tmp_path / "missing.lock") == EXIT_NO_ENGINE


def test_payload_stages_flx4_panel(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # requirement: SKIN-8871D -- the installed app serves the same layouts as a checkout.
    from scripts.build_engine_payload import PayloadBuildError, _stage_practice_panels

    root = Path(__file__).resolve().parents[2]
    staged = _stage_practice_panels(root, tmp_path / "app")
    assert "tools/deck-diagrams/devices/ddj-flx4/panel.json" in staged
    assert (tmp_path / "app" / "tools/deck-diagrams/devices/ddj-flx4/panel.json").is_file()
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    with pytest.raises(PayloadBuildError, match="no practice panel layouts"):
        _stage_practice_panels(empty, tmp_path / "app2")


def test_practice_verb_only_at_command_position() -> None:
    # requirement: SKIN-8871E -- a later "practice" token (a title, a comment) is not the verb.
    from apps.opendj_cli.__main__ import _command_position

    assert _command_position(["practice", "ddj-flx4"]) == 0
    assert _command_position(["--lock", "/tmp/x.lock", "practice", "ddj-flx4"]) == 2
    assert _command_position(["--json", "practice", "ddj-flx4"]) == 1
    tokens = ["track", "comment", "abc", "practice"]
    assert tokens[_command_position(tokens)] == "track"
