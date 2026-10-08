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

import socket
from collections.abc import Callable
from types import FrameType

import uvicorn

#: Seconds uvicorn waits for open requests on SIGTERM before it closes them
#: and runs the lifespan shutdown.
GRACEFUL_SHUTDOWN_S: int = 3


class EarlyShutdownServer(uvicorn.Server):
    """A uvicorn server that tells the app the moment shutdown starts (W43).

    uvicorn runs the app's lifespan shutdown only AFTER its graceful wait, so
    the app cannot release a long poll in time for that wait to end early.
    ``on_shutdown_begin`` runs first, on the event loop, before the listener
    closes and the wait starts.

    ``on_exit_signal`` runs IN the signal handler, once, on the first exit
    signal. That is the one hook that does not depend on the event loop: a
    route blocking the loop with synchronous work keeps ``shutdown()`` from
    ever being reached, but Python still runs a signal handler on the main
    thread between bytecodes, and a blocking call resumes after it.

    ``on_shutdown_end`` runs after the lifespan shutdown, the last moment the
    process is still sure to be alive: once ``serve`` returns, uvicorn
    re-raises the captured SIGTERM under the default handler, which ends the
    process on the spot, so code placed after ``run()`` never runs on a quit.
    """

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        on_shutdown_begin: Callable[[], None],
        on_exit_signal: Callable[[], None] | None = None,
        on_shutdown_end: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(config)
        self._on_shutdown_begin = on_shutdown_begin
        self._on_exit_signal = on_exit_signal
        self._on_shutdown_end = on_shutdown_end
        self._exit_signalled = False

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if not self._exit_signalled:
            self._exit_signalled = True
            if self._on_exit_signal is not None:
                self._on_exit_signal()
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._on_shutdown_begin()
        await super().shutdown(sockets)
        if self._on_shutdown_end is not None:
            self._on_shutdown_end()


__all__ = ["GRACEFUL_SHUTDOWN_S", "EarlyShutdownServer"]
