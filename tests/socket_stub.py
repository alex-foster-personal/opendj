"""Stop a test's raw-socket accept loop so its thread really exits.

A stub that runs ``while True: server.accept()`` on a daemon thread and stops
with only ``server.close()`` leaks that thread on Linux: close does not wake a
thread already blocked in accept(2), so it outlives its test and can take a
connection meant for a LATER test's listener. Seen on PR #4540 (run
36790672335, Wed 30 Sep 2026): test_a_vite_dev_frontend_refuses got "Remote end
closed connection without response" while the leaked act_gestures ``serve``
thread raised JSONDecodeError on that test's HTTP GET, and another raised
"[Errno 88] Socket operation on non-socket".

``shutdown(SHUT_RDWR)`` does wake a blocked accept on Linux; the loop must then
return on the OSError accept raises. Joining with a timeout and asserting the
thread exited turns any future leak into a loud teardown failure instead of a
flaky stranger.
"""

from __future__ import annotations

import contextlib
import socket
import threading

JOIN_TIMEOUT_S = 5.0


def stop_accept_loop(
    server: socket.socket,
    thread: threading.Thread,
    *,
    timeout_s: float = JOIN_TIMEOUT_S,
) -> None:
    """Wake ``thread`` out of ``server.accept()``, close ``server``, join, and
    assert the thread exited. The accept loop must return on OSError."""
    # macOS raises ENOTCONN for shutdown on a listening socket; close then
    # wakes accept there. The join below is the check either way.
    with contextlib.suppress(OSError):
        server.shutdown(socket.SHUT_RDWR)
    server.close()
    thread.join(timeout=timeout_s)
    assert not thread.is_alive(), (
        f"accept-loop thread {thread.name!r} still alive {timeout_s}s after shutdown+close; "
        "its loop must return on OSError from accept()"
    )
