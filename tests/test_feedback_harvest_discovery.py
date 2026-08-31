"""Discovery tests for scripts/feedback_harvest.py.

Regression cover for the Mon 31 Aug 2026 miss: the maintainer left a pinned comment and
a general note on an ad hoc audition server (port 8693), but discovery only
knew about the .env dev port, the 8585 default and the installed app, so the
feedback sat unharvested for an hour and was found only by a manual sweep.

- if _local_listening_ports does not return a port that is currently bound on
  127.0.0.1 then discovery is broken
- if _discover does not offer a scanned port that answers the feedback API
  then audition-server feedback will be missed again
- if _discover offers a scanned port that does NOT answer the feedback API
  then unrelated local services (databases, proxies) leak into the harvest
- if _serves_feedback(quiet=True) prints a warning then a routine scan will
  bury real warnings in noise
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_harvester():
    path = REPO_ROOT / "scripts" / "feedback_harvest.py"
    spec = importlib.util.spec_from_file_location("feedback_harvest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registering before exec: @dataclass resolves the module from sys.modules.
    sys.modules["feedback_harvest"] = module
    spec.loader.exec_module(module)
    return module


fh = _load_harvester()


class _FeedbackHandler(BaseHTTPRequestHandler):
    """Answers the feedback API and nothing else, like a real engine."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/api/v1/feedback/todos":
            body = b'{"todos": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args: object) -> None:
        return


class _SilentHandler(BaseHTTPRequestHandler):
    """A local service that is not an Open DJ engine."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_error(404)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def serving(request: pytest.FixtureRequest):
    handler = request.param
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("serving", [_FeedbackHandler], indirect=True)
def test_local_listening_ports_sees_a_bound_port(serving: int) -> None:
    assert serving in fh._local_listening_ports()


@pytest.mark.parametrize("serving", [_FeedbackHandler], indirect=True)
def test_discover_offers_a_scanned_engine(serving: int) -> None:
    bases = {t.base for t in fh._discover(extra_urls=[], skip_silver=True)}
    assert f"http://127.0.0.1:{serving}" in bases


@pytest.mark.parametrize("serving", [_SilentHandler], indirect=True)
def test_discover_skips_a_scanned_non_engine(serving: int) -> None:
    bases = {t.base for t in fh._discover(extra_urls=[], skip_silver=True)}
    assert f"http://127.0.0.1:{serving}" not in bases


def test_quiet_probe_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    target = fh.Target(
        machine="test", base=f"http://127.0.0.1:{dead_port}", ssh_host=None
    )
    assert fh._serves_feedback(target, quiet=True) is False
    assert capsys.readouterr().out == ""


def test_loud_probe_warns(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    target = fh.Target(
        machine="test", base=f"http://127.0.0.1:{dead_port}", ssh_host=None
    )
    assert fh._serves_feedback(target) is False
    assert "[WARN]" in capsys.readouterr().out
