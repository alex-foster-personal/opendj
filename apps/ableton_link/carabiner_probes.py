"""Loopback TCP probes for a Carabiner instance on a port (LINK-122..124)."""

from __future__ import annotations

import socket
import time

from apps.ableton_link.carabiner import parse_status_line

PROBE_TIMEOUT_S: float = 0.5


def port_is_open(port: int, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def answers_status(port: int, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """True when 127.0.0.1:``port`` answers ``status`` with a parseable Carabiner status line."""
    deadline = time.monotonic() + timeout_s
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s) as sock:
            sock.sendall(b"status\n")
            buffer = b""
            while time.monotonic() < deadline:
                sock.settimeout(max(deadline - time.monotonic(), 0.01))
                chunk = sock.recv(4096)
                if not chunk:
                    return False
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if parse_status_line(raw.decode("utf-8", "replace")) is not None:
                        return True
    except (OSError, ValueError):
        return False
    return False


__all__ = [
    "PROBE_TIMEOUT_S",
    "answers_status",
    "port_is_open",
]
