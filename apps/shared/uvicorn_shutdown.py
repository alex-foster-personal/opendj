"""Shared uvicorn graceful-shutdown timeout for the engine and webui daemons.

Both ``apps.engine_core`` and ``apps.webui.server`` run a uvicorn app that can
be SIGTERM'd while requests are open: a dev client's keep-alive connections,
or a long-lived stream. Bounding the graceful window keeps a SIGTERM reliable
instead of letting uvicorn wait indefinitely for every open request to close
before it runs lifespan shutdown. Unbounded, a hot-reload or a supervised
restart can hang forever against a browser holding keep-alive connections --
see ``apps.webui.server.__main__`` requirement ``WEBUI-SERVER-GRACEFUL-SHUTDOWN``.
"""

from __future__ import annotations

#: Seconds uvicorn waits for open requests on SIGTERM before it closes them
#: and runs the lifespan shutdown.
GRACEFUL_SHUTDOWN_S: int = 3

__all__ = ["GRACEFUL_SHUTDOWN_S"]
