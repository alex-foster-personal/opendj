"""stop_accept_loop wakes a thread blocked in accept() and fails loudly on a leak."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from tests.socket_stub import stop_accept_loop


def _listener() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def test_stop_accept_loop_wakes_a_thread_blocked_in_accept() -> None:
    """[if] a loop that returns on OSError is blocked in accept() [then] stop_accept_loop
    returns with the thread exited, [else] the thread leaks."""
    server = _listener()
    entered = threading.Event()

    def serve() -> None:
        while True:
            entered.set()
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert entered.wait(timeout=2)
    time.sleep(0.05)  # let the thread reach the accept(2) syscall
    stop_accept_loop(server, thread)
    assert not thread.is_alive()


def test_stop_accept_loop_fails_when_the_loop_survives_oserror() -> None:
    """[if] the loop swallows OSError and keeps going [then] stop_accept_loop raises, [else]
    the helper's join is not checking anything."""
    server = _listener()
    done = threading.Event()

    def serve() -> None:
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                time.sleep(0.01)
                continue
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(AssertionError, match="still alive"):
            stop_accept_loop(server, thread, timeout_s=0.2)
    finally:
        done.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
