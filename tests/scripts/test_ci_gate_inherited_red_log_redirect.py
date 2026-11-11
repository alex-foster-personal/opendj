"""The job-logs fetch follows GitHub's 302 to the log blob without leaking the token there.

GitHub answers `actions/jobs/{id}/logs` with a 302 to a pre-signed Azure Blob URL, and Azure
rejects any request that also carries an Authorization header with 401 "Server failed to
authenticate the request". Two real local HTTP servers stand in for the API host and the blob
host; nothing in urllib is mocked.

Regression lines:
  - if the job-logs fetch forwards Authorization to the redirect host then broken
  - if the job-logs fetch stops sending Authorization to the API host then broken
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.ci_gate_inherited_red import fetch_text

LOG_BODY = "2026-11-11T10:00:00Z FAILED tests/audio/test_a.py::test_one\n"
TOKEN = "ghs_test_token_not_real"


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _blob_handler(seen: list[str | None]) -> type[BaseHTTPRequestHandler]:
    class Blob(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            auth = self.headers.get("Authorization")
            seen.append(auth)
            body = b"Server failed to authenticate the request." if auth else LOG_BODY.encode()
            self.send_response(401 if auth else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    return Blob


def _api_handler(blob_url: str, seen: list[str | None]) -> type[BaseHTTPRequestHandler]:
    class Api(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", blob_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    return Api


def test_job_logs_redirect_reaches_the_blob_host_without_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    api_seen: list[str | None] = []
    blob_seen: list[str | None] = []
    with _serve(_blob_handler(blob_seen)) as blob:
        # `localhost` vs `127.0.0.1`: a different host name, as GitHub's blob host is.
        blob_url = f"http://localhost:{blob.server_address[1]}/actions-results/job-logs.txt?sig=abc"
        with _serve(_api_handler(blob_url, api_seen)) as api:
            body = fetch_text(f"http://127.0.0.1:{api.server_address[1]}/repos/o/r/actions/jobs/1/logs", TOKEN)

    assert body == LOG_BODY
    assert api_seen == [f"Bearer {TOKEN}"]
    assert blob_seen == [None]
