"""Loopback asset server for hub presign hydration tests.

The hub calls the real :func:`apps.cloud.asset_store.presign_url`; tests point
``MDT_ASSET_PRESIGN_HOST`` at this server the same way enrollment tests point
``OPENDJ_GOOGLE_JWKS_URL`` at :mod:`tests.cloudsync.google_jwks_rig`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

from apps.cloud.asset_store import PRESIGN_HOST_ENV, PRESIGN_SCHEME_ENV, asset_object_key
from apps.cloud.config import CloudConfig


@contextmanager
def serve_presigned_assets(
    cfg: CloudConfig, bodies: dict[str, bytes]
) -> Iterator[str]:
    """Serve ``bodies`` (digest -> bytes) on loopback until the block exits.

    Sets ``MDT_ASSET_PRESIGN_HOST`` so real presigned URLs minted by the hub
    target this server instead of Cloudflare R2.
    """
    import os

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = unquote(self.path.split("?", 1)[0])
            prefix = f"/{cfg.audio_bucket}/"
            if not path.startswith(prefix):
                self.send_error(404)
                return
            key = path[len(prefix) :]
            digest = next(
                (
                    content_hash
                    for content_hash, body in bodies.items()
                    if asset_object_key(content_hash) == key
                ),
                None,
            )
            if digest is None:
                self.send_error(404)
                return
            body = bodies[digest]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_address[1]}"
    previous_host = os.environ.get(PRESIGN_HOST_ENV)
    previous_scheme = os.environ.get(PRESIGN_SCHEME_ENV)
    os.environ[PRESIGN_HOST_ENV] = host
    os.environ[PRESIGN_SCHEME_ENV] = "http"
    try:
        yield f"http://{host}"
    finally:
        if previous_host is None:
            os.environ.pop(PRESIGN_HOST_ENV, None)
        else:
            os.environ[PRESIGN_HOST_ENV] = previous_host
        if previous_scheme is None:
            os.environ.pop(PRESIGN_SCHEME_ENV, None)
        else:
            os.environ[PRESIGN_SCHEME_ENV] = previous_scheme
        server.shutdown()
        server.server_close()
        thread.join(timeout=10.0)


@contextmanager
def serve_presigned_assets_by_key(
    cfg: CloudConfig, bodies: dict[str, bytes]
) -> Iterator[str]:
    """Serve ``bodies`` (object_key -> bytes) on loopback until the block exits."""
    import os

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = unquote(self.path.split("?", 1)[0])
            prefix = f"/{cfg.audio_bucket}/"
            if not path.startswith(prefix):
                self.send_error(404)
                return
            key = path[len(prefix) :]
            body = bodies.get(key)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_address[1]}"
    previous_host = os.environ.get(PRESIGN_HOST_ENV)
    previous_scheme = os.environ.get(PRESIGN_SCHEME_ENV)
    os.environ[PRESIGN_HOST_ENV] = host
    os.environ[PRESIGN_SCHEME_ENV] = "http"
    try:
        yield f"http://{host}"
    finally:
        if previous_host is None:
            os.environ.pop(PRESIGN_HOST_ENV, None)
        else:
            os.environ[PRESIGN_HOST_ENV] = previous_host
        if previous_scheme is None:
            os.environ.pop(PRESIGN_SCHEME_ENV, None)
        else:
            os.environ[PRESIGN_SCHEME_ENV] = previous_scheme
        server.shutdown()
        server.server_close()
        thread.join(timeout=10.0)


__all__ = ["serve_presigned_assets", "serve_presigned_assets_by_key"]
