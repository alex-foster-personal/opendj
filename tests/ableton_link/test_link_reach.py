"""LINK-50: LinkReachListener start() is portable when SO_REUSEPORT is missing.

- [if] start() raises when SO_REUSEPORT is absent [then] fail
- [if] a bind OSError does not leave listening=False with a reason [then] fail, [else stop]
"""

from __future__ import annotations

import socket

import pytest

from apps.ableton_link.link_reach import LinkReachListener

pytestmark = pytest.mark.requirement("LINK-50")


def test_start_still_listens_when_so_reuseport_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(socket, "SO_REUSEPORT", raising=False)
    listener = LinkReachListener(port=0)
    try:
        listener.start()
        snap = listener.snapshot({})
        assert snap.listening is True
    finally:
        if listener._sock is not None:
            listener._sock.close()


class _BindFailsSocket:
    def setsockopt(self, level: int, option: int, value: int) -> None:
        pass

    def bind(self, address: object) -> None:
        raise OSError("address already in use")

    def settimeout(self, value: float) -> None:
        pass


def test_start_records_listening_false_when_bind_raises_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: _BindFailsSocket())
    listener = LinkReachListener(port=20808)
    listener.start()
    snap = listener.snapshot({})
    assert snap.listening is False
    assert snap.reason is not None and "could not listen" in snap.reason
