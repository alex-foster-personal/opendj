"""REC share CLI mirrors SET-08 metadata-only share routes (issue #11825).

[if] share publish runs for a finalized session [then] stdout is MetadataShareResponse with https, [else stop].
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from apps.sets.share import SetShareConfig
from apps.webui.server.app import create_app
from apps.webui.server.share_gate import AUTH_CLOUDFLARE_ACCESS, ShareConfig
from tests.sets.test_api import _seed_api_session
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

pytestmark = pytest.mark.requirement("SET-11825")

GOLDEN_SHARE = {
    "session_id": "s1",
    "share_state": "shared_cloud",
    "share_url": "https://sets.example.test/sets/shared/s1",
    "content": "metadata_only",
}


def run_rec(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.sets", "rec", *argv],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def share_cli_base_url(sets_root: Path, sets_state_db: Path) -> Iterator[tuple[str, Path]]:
    """Daemon with real share gate and SET-08 routes (not bare router-only FastAPI)."""
    app = create_app(
        mount_frontend=False,
        share_config=ShareConfig(
            host="sets.example.test",
            auth=AUTH_CLOUDFLARE_ACCESS,
        ),
        set_share_config=SetShareConfig("https://sets.example.test"),
        sets_root=sets_root,
        state_db_path=str(sets_state_db),
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"),
        what="sets rec cli share",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, sets_root
    finally:
        stop_uvicorn_in_thread(server, thread, what="sets rec cli share")


def test_rec_share_publish_then_get_metadata_only(share_cli_base_url: tuple[str, Path]) -> None:
    """[if] share publish then get [then ⛔️] stdout JSON matches API golden without audio keys."""
    base_url, sets_root = share_cli_base_url
    session_dir = _seed_api_session(sets_root)
    assert json.loads((session_dir / "manifest.json").read_text())["share_state"] == "private"

    published = run_rec(
        ["share", "publish", "--base-url", base_url, "--session-id", "s1"],
    )
    assert published.returncode == 0, published.stderr
    payload = json.loads(published.stdout)
    assert payload == GOLDEN_SHARE
    assert payload["share_url"].startswith("https://")
    assert not any("audio" in key for key in payload)
    assert json.loads((session_dir / "manifest.json").read_text())["share_state"] == "shared_cloud"

    fetched = run_rec(["share", "get", "--base-url", base_url, "--session-id", "s1"])
    assert fetched.returncode == 0, fetched.stderr
    assert json.loads(fetched.stdout) == GOLDEN_SHARE


def test_rec_share_get_before_publish_returns_409(share_cli_base_url: tuple[str, Path]) -> None:
    """[if] share get runs on a private finalized set [then ⛔️] exit 1 and 409 detail on stderr."""
    base_url, sets_root = share_cli_base_url
    _seed_api_session(sets_root)
    result = run_rec(["share", "get", "--base-url", base_url, "--session-id", "s1"])
    assert result.returncode != 0
    assert "409" in result.stderr
    assert "metadata sharing has not been enabled" in result.stderr


def test_rec_share_missing_session_returns_404(share_cli_base_url: tuple[str, Path]) -> None:
    """[if] share get or publish use a missing session id [then ⛔️] 404 and no new session dir."""
    base_url, sets_root = share_cli_base_url
    before = {p.name for p in sets_root.iterdir() if p.is_dir()}
    missing = "missing-session-id"
    for sub in ("get", "publish"):
        result = run_rec(["share", sub, "--base-url", base_url, "--session-id", missing])
        assert result.returncode != 0
        assert "404" in result.stderr
        assert "session not found" in result.stderr
    after = {p.name for p in sets_root.iterdir() if p.is_dir()}
    assert after == before
