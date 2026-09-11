"""Acceptance tests for red-team track (g) local API probes (issue #1861).

The probes talk to a real listening socket or HTTP server on loopback. Nothing
is mocked: a bind assertion that never reads the kernel table, a CORS refusal
without a positive control, or a traversal refusal without an in-root hit
would all be silent false passes.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from scripts import redteam_local_api as mod
from scripts.redteam_findings import Verdict

SHA = "c" * 40
CONTROL_ORIGIN = "http://127.0.0.1:5173"


class _HealthCorsHandler(BaseHTTPRequestHandler):
    """Minimal daemon: health + ratings filename route + optional CORS policy."""

    def do_GET(self) -> None:
        origin = self.headers.get("Origin", "")
        path = unquote(urlsplit(self.path).path)
        if path == "/api/v1/health":
            self._send(200, b'{"status":"ok"}', origin)
            return
        prefix = "/api/v1/bench/ratings/"
        if path.startswith(prefix):
            name = path[len(prefix) :]
            root = Path(self.server.root)  # type: ignore[attr-defined]
            naive = getattr(self.server, "naive_join", False)
            target = (root / name) if naive else (root / Path(name).name)
            if not naive and (Path(name).name != name or not name.endswith(".json")):
                self._send(400, b'{"detail":"ratings_filename_invalid"}', origin)
                return
            resolved = target.resolve()
            if not naive and not resolved.is_relative_to(root.resolve()):
                self._send(400, b'{"detail":"escaped"}', origin)
                return
            if resolved.is_file():
                self._send(200, resolved.read_bytes(), origin)
                return
            self._send(404, b'{"detail":"ratings_file_missing"}', origin)
            return
        self._send(404, b'{"detail":"missing"}', origin)

    def log_message(self, *args: object) -> None:
        return

    def _send(self, status: int, body: bytes, origin: str) -> None:
        self.send_response(status)
        allowed = getattr(self.server, "allowed_origin", None)
        reflect = getattr(self.server, "reflect_any_origin", False) and origin
        listed = allowed is not None and origin == allowed
        if reflect or listed:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    allowed_origin: str | None = CONTROL_ORIGIN,
    reflect_any_origin: bool = False,
    naive_join: bool = False,
) -> tuple[ThreadingHTTPServer, int]:
    root = tmp_path / "ratings"
    root.mkdir()
    (root / "ok.json").write_text('{"ratings":[]}', encoding="utf-8")
    (tmp_path / "secret.json").write_text('{"secret":true}', encoding="utf-8")
    server = ThreadingHTTPServer((host, 0), _HealthCorsHandler)
    server.root = root  # type: ignore[attr-defined]
    server.allowed_origin = allowed_origin  # type: ignore[attr-defined]
    server.reflect_any_origin = reflect_any_origin  # type: ignore[attr-defined]
    server.naive_join = naive_join  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _target(port: int, origin: str = CONTROL_ORIGIN) -> mod.ProbeTarget:
    return mod.ProbeTarget(host="127.0.0.1", port=port, frontend_origin=origin)


def _listen(host: str) -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(8)
    return sock, int(sock.getsockname()[1])


def test_bind_address_pass_reads_loopback_listen_table() -> None:
    """If the kernel says 127.0.0.1 then bind-address is PASS, or broken."""
    sock, port = _listen("127.0.0.1")
    try:
        result = mod.probe_bind_address(port)
    finally:
        sock.close()
    assert result.verdict == mod.ProbeVerdict.PASS, result
    assert "127.0.0.1" in result.output


def test_bind_address_fail_when_wildcard_listen() -> None:
    """If the kernel says 0.0.0.0 then bind-address is FAIL, or broken."""
    sock, port = _listen("0.0.0.0")
    try:
        result = mod.probe_bind_address(port)
    finally:
        sock.close()
    assert result.verdict == mod.ProbeVerdict.FAIL, result
    assert result.verdict is not mod.ProbeVerdict.PASS


def test_bind_address_unknown_when_nothing_listens() -> None:
    """If no socket is listening then bind-address is UNKNOWN, never PASS."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    result = mod.probe_bind_address(port)
    assert result.verdict == mod.ProbeVerdict.UNKNOWN
    assert result.verdict is not mod.ProbeVerdict.PASS


def test_lan_refusal_pass_when_loopback_only(tmp_path: Path) -> None:
    """If LAN IPs cannot fetch health and loopback can, lan-refusal is PASS."""
    lan = mod.non_loopback_ipv4()
    if not lan:
        pytest.skip("host has no non-loopback IPv4")
    server, port = _serve(tmp_path, host="127.0.0.1")
    try:
        result = mod.probe_lan_refusal(_target(port), lan_ips=lan)
    finally:
        server.shutdown()
        server.server_close()
    assert result.verdict == mod.ProbeVerdict.PASS, result
    assert "127.0.0.1" in result.control


def test_lan_refusal_fail_when_non_loopback_answers(tmp_path: Path) -> None:
    """If a non-loopback address serves health then lan-refusal is FAIL."""
    lan = mod.non_loopback_ipv4()
    if not lan:
        pytest.skip("host has no non-loopback IPv4")
    server, port = _serve(tmp_path, host="0.0.0.0")
    try:
        result = mod.probe_lan_refusal(_target(port), lan_ips=(lan[0],))
    finally:
        server.shutdown()
        server.server_close()
    assert result.verdict == mod.ProbeVerdict.FAIL, result


def test_lan_refusal_unknown_without_loopback_control() -> None:
    """If the loopback control cannot run then lan-refusal is UNKNOWN, never PASS."""
    sock, port = _listen("127.0.0.1")
    sock.close()
    result = mod.probe_lan_refusal(_target(port), lan_ips=("192.0.2.8",))
    assert result.verdict == mod.ProbeVerdict.UNKNOWN
    assert result.verdict is not mod.ProbeVerdict.PASS


def test_cors_pass_refuses_hostile_and_null_and_allows_control(tmp_path: Path) -> None:
    """Hostile and null Origin refused, app origin allowed, or broken."""
    server, port = _serve(tmp_path, allowed_origin=CONTROL_ORIGIN)
    try:
        result = mod.probe_cors(_target(port))
    finally:
        server.shutdown()
        server.server_close()
    assert result.verdict == mod.ProbeVerdict.PASS, result
    assert CONTROL_ORIGIN in result.control


def test_cors_fail_when_hostile_origin_is_echoed(tmp_path: Path) -> None:
    """If evil.example is echoed then cors is FAIL, or broken."""
    server, port = _serve(tmp_path, reflect_any_origin=True)
    try:
        result = mod.probe_cors(_target(port))
    finally:
        server.shutdown()
        server.server_close()
    assert result.verdict == mod.ProbeVerdict.FAIL, result


def test_cors_unknown_without_positive_control(tmp_path: Path) -> None:
    """If the app origin is not allowed then a refusal proves nothing: UNKNOWN."""
    server, port = _serve(tmp_path, allowed_origin="http://127.0.0.1:9")
    try:
        result = mod.probe_cors(_target(port))
    finally:
        server.shutdown()
        server.server_close()
    assert result.verdict == mod.ProbeVerdict.UNKNOWN
    assert result.verdict is not mod.ProbeVerdict.PASS


def test_path_traversal_pass_refuses_dotdot_when_in_root_control_works(
    tmp_path: Path,
) -> None:
    """../ is refused and an in-root filename lookup succeeds, or broken."""
    server, port = _serve(tmp_path, naive_join=False)
    try:
        result = mod.probe_path_traversal(
            _target(port),
            routes=(("GET", "/api/v1/bench/ratings/{filename}", "filename"),),
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.verdict == mod.ProbeVerdict.PASS, result
    assert "probe-control.json" in result.control


def test_path_traversal_fail_when_dotdot_reads_outside_root(tmp_path: Path) -> None:
    """If ../ serves a file outside the ratings root then path-traversal is FAIL."""
    server, port = _serve(tmp_path, naive_join=True)
    try:
        result = mod.probe_path_traversal(
            _target(port),
            routes=(("GET", "/api/v1/bench/ratings/{filename}", "filename"),),
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.verdict == mod.ProbeVerdict.FAIL, result


def test_path_traversal_unknown_when_control_route_is_down() -> None:
    """If the in-root control cannot run then path-traversal is UNKNOWN, never PASS."""
    sock, port = _listen("127.0.0.1")
    sock.close()
    result = mod.probe_path_traversal(
        _target(port),
        routes=(("GET", "/api/v1/bench/ratings/{filename}", "filename"),),
    )
    assert result.verdict == mod.ProbeVerdict.UNKNOWN
    assert result.verdict is not mod.ProbeVerdict.PASS


def test_unknown_findings_are_unavailable_not_pass() -> None:
    """A probe that cannot run records UNAVAILABLE, never a pass finding."""
    result = mod.ProbeResult(
        name="bind-address",
        verdict=mod.ProbeVerdict.UNKNOWN,
        summary="down",
        command="inspect",
        output="none",
        control="need listen",
    )
    findings = mod.findings_from_results((result,), sha=SHA, host="nucbox")
    assert len(findings) == 1
    assert findings[0].verdict == Verdict.UNAVAILABLE


def test_fail_findings_are_filed_shape() -> None:
    """A FAIL probe becomes a Finding.fail the filing CLI can consume."""
    result = mod.ProbeResult(
        name="cors",
        verdict=mod.ProbeVerdict.FAIL,
        summary="echoed evil",
        command="GET health Origin evil",
        output="ACAO=https://evil.example",
        control="Origin app allowed",
    )
    findings = mod.findings_from_results((result,), sha=SHA, host="nucbox")
    assert findings[0].verdict == Verdict.FAIL
    assert findings[0].fingerprint == "local-api-cors"


def test_just_recipe_is_the_one_command() -> None:
    """The probes are runnable with one command: just redteam-local-api."""
    text = Path("justfile").read_text(encoding="utf-8")
    assert "redteam-local-api:" in text
    assert "scripts.redteam_local_api" in text


def test_cli_dead_port_reports_unknown_never_pass(tmp_path: Path) -> None:
    """If --port points at nothing then the CLI prints UNKNOWN, never PASS."""
    sock, port = _listen("127.0.0.1")
    sock.close()
    code = mod.main(
        [
            "--port",
            str(port),
            "--frontend-origin",
            CONTROL_ORIGIN,
            "--index",
            str(tmp_path / "i.jsonl"),
        ]
    )
    assert code == 2
