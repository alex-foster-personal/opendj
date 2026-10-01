"""scripts/perf/capture_build_identity.py -- build-identity verification checks.

Split out of test_library_mode_capture.py (quality-ratchet
`file_size.over_limit_python`, PR #4034), mirroring the split of
scripts/perf/capture_build_identity.py out of capture_library_mode.py: these
tests exercise the engine/frontend/checkout identity gates in isolation.
Integration-level tests that drive `main()` end to end stay in
test_library_mode_capture.py and import the server helpers below.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest


def _serve_html(html: str) -> tuple[http.server.ThreadingHTTPServer, str]:
    """A real loopback HTTP server that answers every GET with `html`."""
    body = html.encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<script type="module" src="/@vite/client"></script>', "vite-dev"),
        ('<link rel="modulepreload" href="/_app/immutable/entry/start.js">', "static-build"),
    ],
)
def test_frontend_mode_names_the_bundle_it_measured(html: str, expected: str) -> None:
    """[if] the frontend serves the Vite client [then] the row says vite-dev, else static-build."""
    from scripts.perf.capture_build_identity import _frontend_mode

    server, url = _serve_html(html)
    try:
        assert _frontend_mode(url) == expected
    finally:
        server.shutdown()
        server.server_close()


def test_frontend_mode_refuses_an_unreachable_frontend() -> None:
    """[if] nothing listens at the frontend [then] ConnectionError, never a guessed mode."""
    from scripts.perf.capture_build_identity import _frontend_mode

    server, url = _serve_html("")
    server.shutdown()
    server.server_close()
    with pytest.raises(ConnectionError, match="frontend unreachable"):
        _frontend_mode(url)


def _serve_build_info(
    git_sha_full: str, *, git_dirty: bool | None = False
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """A real loopback HTTP server standing in for /api/v1/build-info.

    `git_dirty=None` omits the field entirely, standing in for a daemon whose
    build-info response predates it."""
    payload: dict[str, object] = {"git_sha_full": git_sha_full}
    if git_dirty is not None:
        payload["git_dirty"] = git_dirty
    body = json.dumps(payload).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_verify_served_build_sha_passes_when_the_engine_serves_this_checkout() -> None:
    """[if] build-info's git_sha_full matches the capturing checkout and git_dirty is
    False [then] no reason is returned [else fail the capture]."""
    from scripts.perf.capture_build_identity import _verify_served_build_sha

    server, url = _serve_build_info("abc123full", git_dirty=False)
    try:
        assert _verify_served_build_sha(url, "abc123full") is None
    finally:
        server.shutdown()
        server.server_close()


def test_verify_served_build_sha_refuses_a_stale_or_foreign_engine() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4131404896: [if] --engine points at a
    build other than this checkout [then] a reason names BOTH shas, [else stop] --
    otherwise a stale or foreign build's numbers get marked measured under a SHA it
    never actually served."""
    from scripts.perf.capture_build_identity import _verify_served_build_sha

    server, url = _serve_build_info("stale000sha")
    try:
        reason = _verify_served_build_sha(url, "abc123full")
        assert reason is not None
        assert "stale000sha" in reason
        assert "abc123full" in reason
    finally:
        server.shutdown()
        server.server_close()


def test_verify_served_build_sha_refuses_an_unreachable_engine() -> None:
    """[if] nothing listens at --engine [then] a reason string, never a silent pass."""
    from scripts.perf.capture_build_identity import _verify_served_build_sha

    server, url = _serve_build_info("abc123full")
    server.shutdown()
    server.server_close()
    reason = _verify_served_build_sha(url, "abc123full")
    assert reason is not None
    assert "unreachable" in reason


def test_verify_served_build_sha_refuses_a_dirty_engine() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4132371711: [if] build-info reports the
    expected sha with git_dirty=True [then] a reason names the sha and says DIRTY
    [else] uncommitted engine changes get measured and recorded as though they came
    from the clean, named commit."""
    from scripts.perf.capture_build_identity import _verify_served_build_sha

    server, url = _serve_build_info("abc123full", git_dirty=True)
    try:
        reason = _verify_served_build_sha(url, "abc123full")
        assert reason is not None
        assert "abc123full" in reason
        assert "DIRTY" in reason
    finally:
        server.shutdown()
        server.server_close()


def test_verify_served_build_sha_refuses_a_response_missing_git_dirty() -> None:
    """[if] build-info answers without git_dirty at all [then] a reason naming the
    missing field, never a silent pass -- the field is a required part of
    BuildInfoOut (apps/engine_core/build_info.py), so its absence is itself
    something wrong with the served identity."""
    from scripts.perf.capture_build_identity import _verify_served_build_sha

    server, url = _serve_build_info("abc123full", git_dirty=None)
    try:
        reason = _verify_served_build_sha(url, "abc123full")
        assert reason is not None
        assert "git_dirty" in reason
    finally:
        server.shutdown()
        server.server_close()


def _serve_engine(
    git_sha_full: str, *, git_dirty: bool = False, pid: int | None = None
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """A real loopback server answering both /api/v1/health and
    /api/v1/build-info, standing in for a full --engine.

    `pid` defaults to this TEST PROCESS's own real, running pid rather than a
    fixed placeholder -- `_read_engine_pid` (discussion_r4138422256) requires
    a genuine integer, and a hardcoded fake would still satisfy `isinstance`
    while telling a reader nothing about why that number was chosen. Pass an
    explicit `pid` to simulate a same-sha engine restart (a different pid
    serving the identical git_sha_full/git_dirty).
    """
    health_body = json.dumps({"status": "ok"}).encode("utf-8")
    served_pid = pid if pid is not None else os.getpid()
    build_info_body = json.dumps(
        {"git_sha_full": git_sha_full, "git_dirty": git_dirty, "pid": served_pid}
    ).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = build_info_body if self.path.endswith("/build-info") else health_body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_read_engine_pid_returns_the_real_served_pid() -> None:
    """[if] build-info carries a real pid [then] _read_engine_pid returns it, [else stop]."""
    from scripts.perf.capture_build_identity import _read_engine_pid

    server, url = _serve_engine("abc123full", pid=54321)
    try:
        assert _read_engine_pid(url) == 54321
    finally:
        server.shutdown()
        server.server_close()


def test_read_engine_pid_refuses_a_response_with_no_usable_pid() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4138422256: [if] build-info answers
    without a real integer pid [then] this raises loud rather than returning a
    fabricated identity to pin against, [else] a same-sha engine restart could
    never be told apart from the original process."""
    from scripts.perf.capture_build_identity import _read_engine_pid

    server, url = _serve_build_info("abc123full")  # no "pid" key at all
    try:
        with pytest.raises(RuntimeError, match="carries no usable pid"):
            _read_engine_pid(url)
    finally:
        server.shutdown()
        server.server_close()


def _serve_frontend(
    *, version: str | None, vite_dev: bool
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """A real loopback server standing in for a frontend origin, with NO /api
    route at all -- unlike vite-dev, a static frontend server never proxies
    anything, so this stands in for the topology
    `_verify_frontend_build_version` is meant to check.

    `version=None` means "this server does not serve /_app/version.json",
    reproducing the real, measured behavior of `vite dev` (confirmed live,
    Mon 29 Sep 2026: a running dev server answers that path with 404 because
    SvelteKit only writes it for a static build)."""
    index_html = (
        '<script type="module" src="/@vite/client"></script><title>opendj</title>'
        if vite_dev
        else "<title>opendj</title>"
    ).encode("utf-8")
    version_body = None if version is None else json.dumps({"version": version}).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/_app/version.json" or self.path == "/_app/version.json":
                if version_body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(version_body)))
                self.end_headers()
                self.wfile.write(version_body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(index_html)))
            self.end_headers()
            self.wfile.write(index_html)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_verify_frontend_build_version_passes_when_the_static_build_matches() -> None:
    """[if] /_app/version.json's version matches the capturing checkout [then] no
    reason is returned."""
    from scripts.perf.capture_build_identity import _verify_frontend_build_version

    server, url = _serve_frontend(version="abc123full", vite_dev=False)
    try:
        assert _verify_frontend_build_version(url, "abc123full") is None
    finally:
        server.shutdown()
        server.server_close()


def test_verify_frontend_build_version_refuses_a_foreign_static_build() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4132371694: [if] /_app/version.json
    names a different build than the capturing checkout [then] a reason names BOTH
    versions -- this is the check that a proxied /api/v1/build-info comparison
    cannot make, because that path is served BY THE ENGINE."""
    from scripts.perf.capture_build_identity import _verify_frontend_build_version

    server, url = _serve_frontend(version="some-other-checkouts-sha", vite_dev=False)
    try:
        reason = _verify_frontend_build_version(url, "abc123full")
        assert reason is not None
        assert "some-other-checkouts-sha" in reason
        assert "abc123full" in reason
        assert url in reason
    finally:
        server.shutdown()
        server.server_close()


def test_verify_frontend_build_version_refuses_a_dirty_frontend_build() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4132707456: [if] /_app/version.json
    names the expected sha with the `-dirty` suffix svelte.config.js writes for an
    uncommitted tree [then] a reason names the sha and says DIRTY, [else] a frontend
    built from uncommitted changes gets measured and recorded as though it came from
    the named, clean commit -- the same gap the engine's own git_dirty check closes."""
    from scripts.perf.capture_build_identity import _verify_frontend_build_version

    server, url = _serve_frontend(version="abc123full-dirty", vite_dev=False)
    try:
        reason = _verify_frontend_build_version(url, "abc123full")
        assert reason is not None
        assert "abc123full" in reason
        assert "DIRTY" in reason
    finally:
        server.shutdown()
        server.server_close()


def test_verify_frontend_build_version_refuses_a_vite_dev_server() -> None:
    """[if] the frontend is a vite-dev server (no /_app/version.json, matching the
    live-measured 404) [then] a reason string, never a silent pass -- `main()`
    refuses before ever reaching this check, but the function itself must not
    treat a 404 as an absence of evidence rather than a failure."""
    from scripts.perf.capture_build_identity import _verify_frontend_build_version

    server, url = _serve_frontend(version=None, vite_dev=True)
    try:
        reason = _verify_frontend_build_version(url, "abc123full")
        assert reason is not None
        assert "404" in reason
    finally:
        server.shutdown()
        server.server_close()


def _init_git_repo(root: Path) -> None:
    """A real, minimal git repo -- no fakes -- for exercising a git-status
    check without touching this repo's own working tree."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "tracked.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)


def test_verify_capturing_checkout_clean_passes_on_a_clean_tree(tmp_path: Path) -> None:
    """[if] the capturing checkout has no uncommitted changes [then] no reason is
    returned."""
    from scripts.perf.capture_build_identity import _verify_capturing_checkout_clean

    _init_git_repo(tmp_path)
    assert _verify_capturing_checkout_clean(tmp_path) is None


def test_verify_capturing_checkout_clean_refuses_a_dirty_harness() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4132814651: [if] the checkout that
    runs the Playwright spec and process sampler has uncommitted changes [then] a
    reason names the checkout path and says DIRTY, [else] an operator who edits
    the harness after starting clean, matching services gets rows attributed to a
    commit whose harness code never actually ran -- the engine-sha and
    frontend-version checks cannot catch this, because they verify the served
    daemons, not the process driving and sampling them."""
    import tempfile

    from scripts.perf.capture_build_identity import _verify_capturing_checkout_clean

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_git_repo(root)
        (root / "tracked.txt").write_text("v2 -- edited after the commit\n", encoding="utf-8")
        reason = _verify_capturing_checkout_clean(root)
        assert reason is not None
        assert str(root) in reason
        assert "DIRTY" in reason
