"""The AGENT-13 updater rig: real engines, a real channel, a real shell.

Kept out of ``tests/opendj_cli/conftest.py`` (which re-exports the ``updater``
fixture) so that file stays under the repo's 600-line ceiling.

The updater spans two processes that must AGREE: the engine that answers, and
the shell that installs. Both are real here. The channel is a real HTTP server,
the engine is real uvicorn on a real socket, and a relaunch is a second engine
process on a second port serving a second payload with the lock file rewritten
to name it. Nothing on the CLI's side is stood in for.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from apps.engine_core.build_info import MANIFEST_ENV, add_build_info_route
from apps.engine_core.update_channel import (
    UPDATE_APPLY_PATH,
    add_update_apply_route,
    add_update_check_route,
    platform_key,
)
from apps.webui.server.routes.commands import router as commands_router
from tests.waits import LIVENESS_CHECK_S, THREAD_HANG_GUARD_S, start_uvicorn_in_thread

SHELL_POLL_S = 0.05


class UpdateChannel:
    """A real HTTP server answering the Tauri manifest the engine checks.

    ``resolve_update_check`` inside the engine reads this over a socket,
    exactly as it reads GitHub Releases in production; publishing a version
    rewrites what it serves, which is what cutting a release does.
    """

    def __init__(self) -> None:
        self._manifest: dict[str, Any] = {}
        channel = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(channel._manifest).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                """Silenced: a test's stdout is its assertions, not request logs."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        # bind() above names 127.0.0.1, and reading it back off server_address
        # types as bytes, so the host is stated rather than unpacked.
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self.endpoint = f"{self.base_url}/latest.json"

    def publish(self, version: str) -> None:
        key = platform_key()
        self._manifest = {
            "version": version,
            "notes": "",
            "pub_date": "2026-09-15T08:00:00Z",
            "platforms": {
                key: {
                    "signature": "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZw==",
                    "url": f"https://example.invalid/OpenDJ-{version}.app.tar.gz",
                }
            },
        }

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _write_payload_manifest(path: Path, *, app_version: str, git_sha_full: str) -> None:
    """The identity a packaged build carries, in the payload builder's shape."""
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "opendj-engine-payload",
                "identity": {
                    "app_version": app_version,
                    "built_at_utc": "2026-09-15T08:00:00Z",
                    "git_branch": "main",
                    "git_dirty": False,
                    "git_sha": git_sha_full[:8],
                    "git_sha_full": git_sha_full,
                    "lane_label": "test",
                },
            }
        ),
        encoding="utf-8",
    )


class Updater:
    """The installed app, the channel it reads, and the relaunch it performs.

    ``relaunch`` is a real relaunch: a second engine process, on a second
    port, serving a second payload, with the lock file rewritten to point at
    it. That is what the shell does after it installs, and it is the only way
    the CLI's after-read can be a DIFFERENT build from its before-read, which
    is the whole thing ``apply`` claims.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.channel = UpdateChannel()
        self.lock_path = tmp_path / ".engine.lock"
        self.base_url = ""
        self.port = 0
        self.apps: list[Any] = []
        #: Apply orders that actually reached the engine. Counted on the real
        #: server, because "the CLI sent nothing" is a claim about the wire and
        #: the engine refuses a bad order too: without this, a CLI that posted
        #: anyway and was refused looks identical to one that never posted.
        self.apply_posts = 0
        self.shell_claims = 0
        self.shell_error: BaseException | None = None
        self._boots = 0
        self._servers: list[tuple[Any, threading.Thread]] = []
        self._shell_stop = threading.Event()
        self._shell_thread: threading.Thread | None = None

    # ----- engine processes -----
    def start(self, *, app_version: str, git_sha_full: str, publish: str) -> None:
        """Boot the installed app and publish what the channel offers for it."""
        self.channel.publish(publish)
        self._boot(app_version, git_sha_full)

    def relaunch(self, *, app_version: str, git_sha_full: str) -> None:
        """Install the announced release: a new process on a new port."""
        self._boot(app_version, git_sha_full)

    def _boot(self, app_version: str, git_sha_full: str) -> None:
        self._boots += 1
        manifest = self.tmp_path / f"payload-{self._boots}.json"
        _write_payload_manifest(
            manifest, app_version=app_version, git_sha_full=git_sha_full
        )
        app = FastAPI()
        self._count_apply_posts(app)
        add_build_info_route(
            app, environ={MANIFEST_ENV: str(manifest)}, repo_root=self.tmp_path
        )
        add_update_check_route(app, endpoint=self.channel.endpoint)
        add_update_apply_route(app, endpoint=self.channel.endpoint)
        app.include_router(commands_router, prefix="/api/v1")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = int(listener.getsockname()[1])
        server, thread = start_uvicorn_in_thread(
            uvicorn.Config(app, log_level="warning"), what="the engine", sockets=[listener]
        )
        self._servers.append((server, thread))
        self.apps.append(app)
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.lock_path.write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "role": "opendj-engine",
                    "host": "127.0.0.1",
                    "port": port,
                    "boot_id": f"test-boot-{self._boots}",
                }
            ),
            encoding="utf-8",
        )

    def _count_apply_posts(self, app: FastAPI) -> None:
        """Count the apply orders that reach the engine, on the engine's side."""

        @app.middleware("http")
        async def _count(request: Any, call_next: Any) -> Any:
            if request.method == "POST" and request.url.path == UPDATE_APPLY_PATH:
                self.apply_posts += 1
            return await call_next(request)

    # ----- the desktop shell -----
    def start_shell(
        self,
        install: Callable[[], None],
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Poll for an apply-update order, run ``install``, report the result.

        The shell is the only component that may install an update, so it is
        the one that claims the order the apply route enqueued and posts the
        result the CLI is waiting on. It claims exactly one order, then exits
        the way a real shell does when it restarts into the new build. A
        refusal (an unverified signature, say) is posted the same way, so the
        failure path is the real one and not a special case.
        """
        base = self.base_url
        posted = result if result is not None else {"status": "succeeded", "outcome": "installed"}
        self._shell_thread = threading.Thread(
            target=self._shell_loop, args=(base, install, posted), daemon=True
        )
        self._shell_thread.start()

    def wait_for_shell(self) -> None:
        """Block until the shell claimed its order, or fail naming its crash."""
        if self._shell_thread is None:
            raise AssertionError("the shell was never started")
        started = time.monotonic()
        while True:
            if self.shell_error is not None:
                raise AssertionError(f"the shell crashed: {self.shell_error!r}")
            if not self._shell_thread.is_alive():
                return
            if time.monotonic() - started > THREAD_HANG_GUARD_S:
                raise AssertionError("HANG: the shell never reached its order")
            time.sleep(LIVENESS_CHECK_S)

    def _shell_loop(
        self, base: str, install: Callable[[], None], posted: dict[str, Any]
    ) -> None:
        try:
            while not self._shell_stop.is_set():
                claimed = self._claim(base)
                if claimed is None:
                    time.sleep(SHELL_POLL_S)
                    continue
                self.shell_claims += 1
                if posted.get("status") == "succeeded":
                    install()
                httpx.post(
                    f"{base}/api/v1/commands/{claimed['id']}/result",
                    json=posted,
                    timeout=5.0,
                ).raise_for_status()
                return
        except BaseException as error:
            # Kept for wait_for_shell's message, then re-raised unchanged: a
            # swallowed crash here would look exactly like a shell that simply
            # never reached its order.
            self.shell_error = error
            raise

    def _claim(self, base: str) -> dict[str, Any] | None:
        response = httpx.get(
            f"{base}/api/v1/commands/next", params={"consumer": "shell"}, timeout=5.0
        )
        response.raise_for_status()
        claimed = response.json()
        return claimed if isinstance(claimed, dict) else None

    def stop(self) -> None:
        self._shell_stop.set()
        if self._shell_thread is not None:
            self._shell_thread.join(timeout=5)
        for server, thread in self._servers:
            server.should_exit = True
            thread.join(timeout=0.5)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=10)
        self.channel.stop()

    def enqueue_apply(self) -> str:
        """A second agent's apply, already in flight, on the running app."""
        response = httpx.post(f"{self.base_url}/api/v1/update/apply", timeout=5.0)
        response.raise_for_status()
        command_id: str = response.json()["command_id"]
        return command_id


@pytest.fixture
def updater(tmp_path: Path) -> Any:
    rig = Updater(tmp_path)
    try:
        yield rig
    finally:
        rig.stop()
