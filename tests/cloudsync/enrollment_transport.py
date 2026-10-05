"""The ``HubTransport`` the CLOUDSYNC suite drives the real router through.

Not a mock of the hub: it moves the same JSON through the same routing,
validation and error handling a network client hits. Only the socket is
absent, which is the one thing a unit test cannot have.

Its own module because two things need it now -- ``test_hub_sync`` and the
enrollment fixtures in ``conftest`` -- and a conftest that imported it from a
test module would drag that whole module in at collection time. ``conftest``
is the shared floor, so what it depends on has to sit below it.
``test_hub_sync`` re-exports it under its old private name, so every existing
``from tests.cloudsync.test_hub_sync import _TestClientTransport`` still
resolves to this one class rather than a second copy of it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi.testclient import TestClient

from apps.sync_hub import transport


class TestClientTransport:
    """A :class:`apps.sync_hub.client.HubTransport` backed by ``TestClient``.

    Any non-2xx becomes :class:`apps.sync_hub.client.SyncTransportError`
    carrying the status and body, matching what the real
    :class:`apps.sync_hub.transport.HttpTransport` does, so a test asserting
    on a refusal is asserting on the same shape production raises.
    """

    __test__ = False  # not a pytest test class, despite the name

    def __init__(self, http: TestClient, *, bearer: str | None = None) -> None:
        """``bearer`` mirrors :class:`apps.sync_hub.transport.HttpTransport`:
        sent as ``Authorization: Bearer`` on every call, none when None."""
        self._http = http
        self._headers: dict[str, str] = (
            {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
        )

    @property
    def app_state(self) -> Any:
        """The hub app's ``state``, for a test that must reach its resources."""
        app: Any = self._http.app
        return app.state

    def _decoded(self, response: Any, label: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise transport.refused(label, response.status_code, response.text)
        return dict(response.json())

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._http.post(path, json=dict(payload), headers=self._headers)
        return self._decoded(response, f"POST {path}")

    def get(self, path: str, params: Mapping[str, str | Sequence[str]]) -> dict[str, Any]:
        """``params``' value type matches ``HubTransport.get`` exactly (found
        by the quality ratchet's ``mypy.errors_tests`` regression on PR
        #3831's own head, round-3 fix): the old, narrower ``Mapping[str,
        str]`` meant this class did not actually satisfy the
        ``HubTransport`` Protocol it claims to implement, so mypy flagged
        every call site passing an instance of it as ``transport=`` --
        already true for 5 existing call sites tolerated as debt on main
        before this fix; a 6th, newly added one in
        ``test_sync_cli_single_flight.py`` is what turned the tolerated
        debt into a measured regression. ``dict(params)`` handed to
        ``TestClient.get`` already accepted a sequence value at runtime
        (httpx's ``QueryParamTypes`` supports repeated params); only the
        annotation was too narrow to say so.
        """
        response = self._http.get(path, params=dict(params), headers=self._headers)
        return self._decoded(response, f"GET {path}")


__all__ = ["TestClientTransport"]
