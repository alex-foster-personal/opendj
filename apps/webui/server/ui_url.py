"""Where a browser should point to reach the UI this process serves (UIURL-01).

The packaged engine serves the whole SPA on its own loopback port, so a Chrome
tab and the desktop window can be the same engine and the same library. The
address users are given is ``http://opendj.localhost:<port>/``: every browser
resolves ``*.localhost`` to loopback with no hosts-file edit (RFC 6761), and
the request guard admits exactly this one name by default.

A portless name (``http://opendj.localhost/``) would need port 80, which needs
root or a privileged helper on macOS. That is out of scope on purpose.
"""
from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from typing import Any, Final, Literal

#: The one named host the request guard admits by default, beside loopback.
NAMED_LOCAL_HOST: Final = "opendj.localhost"

UiServedBy = Literal["engine", "dev-frontend"]

# A loopback accept or refuse returns immediately. The timeout only stops a
# black-holed port from stalling /health and /ui-url. Past it, the frontend
# is not served: advertising it would hand out a dead address.
_DEV_FRONTEND_CONNECT_TIMEOUT_S: Final = 0.25

# Past the handshake, the port must answer GET / with the Open DJ document.
# Vite's first page render can take longer than a bare accept, so the read
# gets its own, still bounded, budget.
_DEV_FRONTEND_READ_TIMEOUT_S: Final = 1.5
_DEV_FRONTEND_MAX_BODY_BYTES: Final = 256 * 1024

#: Written into every page by ``apps/webui/frontend/src/app.html`` (dev and
#: built). An unrelated local service on the configured port does not carry it.
UI_DOCUMENT_MARKER: Final = "data-sveltekit-preload-data"


@dataclass(frozen=True)
class UiAddress:
    ui_url: str
    loopback_url: str
    port: int
    served_by: UiServedBy


def named_origin(port: int) -> str:
    return f"http://{NAMED_LOCAL_HOST}:{port}"


def _address(port: int, served_by: UiServedBy) -> UiAddress:
    return UiAddress(
        ui_url=f"{named_origin(port)}/",
        loopback_url=f"http://127.0.0.1:{port}/",
        port=port,
        served_by=served_by,
    )


def _dev_frontend_accepts(port: int) -> bool:
    """True when 127.0.0.1:port completes a TCP handshake.

    The probe socket is bound first. On loopback, connect() to a closed port
    succeeds when the kernel picks that same port as the source: the socket
    connects to itself. A source port equal to ``port`` means nothing else
    held it, which is not a frontend.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        if int(sock.getsockname()[1]) == port:
            return False
        sock.settimeout(_DEV_FRONTEND_CONNECT_TIMEOUT_S)
        sock.connect(("127.0.0.1", port))
    except OSError:
        return False
    else:
        return True
    finally:
        sock.close()


def _dev_frontend_serves_ui(port: int) -> bool:
    """True only when 127.0.0.1:port answers GET / with the Open DJ UI.

    ``just webui-backend`` records ``frontend_port`` from ``.env`` even when
    ``just webui-frontend`` is not running, and any unrelated local service
    can hold that port. A handshake alone would advertise that service as
    the UI (discussion_r4237290775). So the port must answer an HTTP 200
    ``text/html`` document carrying :data:`UI_DOCUMENT_MARKER`. A silent
    listener, a non-HTML answer or another app's page is not served.
    """
    if not _dev_frontend_accepts(port):
        return False
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_DEV_FRONTEND_READ_TIMEOUT_S)
    try:
        conn.request("GET", "/", headers={"Accept": "text/html"})
        resp = conn.getresponse()
        if resp.status != 200:
            return False
        if "text/html" not in (resp.getheader("Content-Type") or "").lower():
            return False
        body = resp.read(_DEV_FRONTEND_MAX_BODY_BYTES).decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()
    return UI_DOCUMENT_MARKER in body


def resolve_ui_address(app_state: Any) -> UiAddress | None:
    """The UI this process serves, or None when it serves none.

    The engine's own SPA mount wins: if this process serves the bundle, its
    own port is guaranteed to answer. Otherwise a dev backend points at the
    vite frontend only when that port answers GET / with the Open DJ
    document. A configured port with nothing, or something else, listening
    is not served. When neither
    holds, None: no guess.
    """
    port = getattr(app_state, "port", None)
    frontend_port = getattr(app_state, "frontend_port", None)
    if getattr(app_state, "spa_mounted", False) and port is not None:
        return _address(int(port), "engine")
    if frontend_port is not None and _dev_frontend_serves_ui(int(frontend_port)):
        return _address(int(frontend_port), "dev-frontend")
    return None


__all__ = [
    "NAMED_LOCAL_HOST",
    "UI_DOCUMENT_MARKER",
    "UiAddress",
    "UiServedBy",
    "named_origin",
    "resolve_ui_address",
]
