"""The full-app webui fixtures, outside conftest.py on purpose (BigRfc-1).

These fixtures build the whole daemon through ``create_app``, which imports
every router. A conftest that imports ``create_app`` puts every test beneath
it in the reach of every server module, so the PR-head selector
(``scripts/ci_test_selection_rules.py``, "conftest reach") used to select all
of tests/webui for any change under ``apps/webui/server``. Here the edge is
explicit: a test module that wants the full app imports the fixture by name,

    from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixture)

Each fixture is registered under its pytest ``name=`` and defined as
``<name>_fixture``, so the import does not shadow the test's ``client``
parameter (ruff F811). Only that module joins the reach of the app's composition root. Tests that
never build the app (pure helpers, stores, parsers) stay out of it.

``seed_backend`` stays in conftest.py: it builds an ``InMemoryBackend`` and
imports no router.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from tests.webui.testclient_host import TEST_HOST_BASE_URL

__all__ = ["client_fixture", "insecure_client_fixture", "locked_client_fixture"]


def _stub_rb_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the app fixtures hermetic: no live master.plain.db dependency.

    ``list_playlists`` consults ``rb_vendor.playlist_order_index`` whenever
    rekordbox playlists exist; it reads the live ``master.plain.db``. On a
    dev Mac that happens to exist, so the unit test silently depended on
    machine state; on any other machine it fails loudly (by design) and the
    endpoint 500s. Stub ONLY the db-backed order lookup -- everything else
    (``bulk_availability`` is a pure stat pass) stays real. rb_vendor's own
    behaviour is covered by (private path) and
    the live-db suite.
    """
    from apps.webui.server import rb_vendor

    monkeypatch.setattr(rb_vendor, "playlist_order_index", dict)


@pytest.fixture(name="client")
def client_fixture(seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _stub_rb_vendor(monkeypatch)
    app = create_app(
        backend=seed_backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        port=18697,
        frontend_port=19411,
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as c:
        yield c


@pytest.fixture(name="insecure_client")
def insecure_client_fixture(seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _stub_rb_vendor(monkeypatch)
    app = create_app(backend=seed_backend, bind_host="0.0.0.0", hostname="test-host")
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as c:
        yield c


@pytest.fixture(name="locked_client")
def locked_client_fixture(seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _stub_rb_vendor(monkeypatch)
    app = create_app(
        backend=seed_backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: {"holder": "other-host", "expires_at": "2099-01-01T00:00:00Z"},
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as c:
        yield c
