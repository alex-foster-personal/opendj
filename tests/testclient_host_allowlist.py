"""Shared TestClient default for the daemon host allowlist (issue #2689).

``apps/webui/server/request_guard.py`` (commit 97aefcd82, "add daemon host
allowlist and origin guard") rejects any request whose Host header is not in
the app's allowlist, returning ``HOST_NOT_ALLOWED``. The allowlist always
includes the loopback hostnames (``127.0.0.1``, ``localhost``, ``::1``), but
``starlette.testclient.TestClient`` defaults its ``base_url`` to
``http://testserver``, so every TestClient built without an explicit
``base_url`` sent an unallowlisted Host and got refused.

Fixing every call site individually (181 of them across tests/webui and
tests/cloudsync at the time this was written) is not the class fix: the next
new test would reintroduce the same failure. Instead this module patches the
one place the default lives -- ``TestClient.__init__``'s ``base_url``
default -- so every TestClient built anywhere in the suite without an
explicit ``base_url`` gets an allowlisted loopback host instead. A caller
that passes its own ``base_url`` (for example to exercise a share-host or a
foreign-host rejection test) is untouched: this only changes the default.

Call :func:`install_loopback_testclient_default` from a conftest.py that
collects the affected tests. It is idempotent -- calling it more than once
(e.g. once each from tests/webui/conftest.py and tests/cloudsync/conftest.py
in the same session) leaves the same loopback default in place.
"""
from __future__ import annotations

from starlette.testclient import TestClient

#: Always present in request_guard's allowlist regardless of
#: MUSIC_DJ_ALLOWED_HOSTS or share config, so this default clears the guard
#: everywhere without depending on any test's env or app configuration.
LOOPBACK_TEST_BASE_URL = "http://127.0.0.1"


def install_loopback_testclient_default() -> None:
    """Make ``TestClient(app)`` default to an allowlisted loopback Host."""
    defaults = TestClient.__init__.__defaults__
    if defaults is None or defaults[0] == LOOPBACK_TEST_BASE_URL:
        return
    TestClient.__init__.__defaults__ = (LOOPBACK_TEST_BASE_URL, *defaults[1:])
