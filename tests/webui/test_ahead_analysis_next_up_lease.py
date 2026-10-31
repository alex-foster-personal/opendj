"""Next-up writes are fenced on the ui-mirror lease (NATIVE-5886, AGENT-18).

[if] a page that no longer holds the mirror lease PUTs next-up [then] it is refused, [else stop].
"""

from __future__ import annotations

from time import monotonic
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes import ahead_analysis
from apps.webui.server.routes.state import LEASE_HEADER, MirrorLease

pytestmark = pytest.mark.requirement("NATIVE-5886")


class _Drain:
    def __init__(self) -> None:
        self.next_up: list[str] = ["leader-track"]

    def set_next_up(self, stable_ids: list[str]) -> list[str]:
        self.next_up = list(stable_ids)
        return list(self.next_up)


def _client(holder: str | None, *, expired: bool = False) -> tuple[TestClient, _Drain]:
    app = FastAPI()
    app.include_router(ahead_analysis.router, prefix="/api/v1")
    drain = _Drain()
    app.state.ahead_analysis = drain
    if holder is not None:
        app.state.ui_mirror_lease = MirrorLease(
            holder=holder, expires_monotonic=monotonic() + (-1.0 if expired else 10.0)
        )
    return TestClient(app), drain


def _put(client: TestClient, ids: list[str], lease: str | None) -> Any:
    headers = {} if lease is None else {LEASE_HEADER: lease}
    return client.put("/api/v1/ahead-analysis/next-up", json={"stable_ids": ids}, headers=headers)


def test_a_former_leader_cannot_overwrite_the_current_leaders_chain() -> None:
    """[if] another page holds the live lease [then] this page's next-up PUT is a 409, [else stop]."""
    client, drain = _client("tab-b")
    response = _put(client, [], "tab-a")
    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "lease_held", "holder": "tab-b"}
    assert drain.next_up == ["leader-track"], "if a stale PUT replaced the leader's list then broken"


def test_control_the_lease_holder_writes() -> None:
    """[if] the writing page holds the live lease [then] its next-up PUT is applied, [else stop]."""
    client, drain = _client("tab-a")
    response = _put(client, ["t1"], "tab-a")
    assert response.status_code == 200 and response.json() == {"ok": True, "next_up": ["t1"]}
    assert drain.next_up == ["t1"]


@pytest.mark.parametrize(("holder", "expired"), [(None, False), ("tab-a", True), ("tab-b", True)])
def test_a_page_without_a_live_lease_cannot_write(holder: str | None, expired: bool) -> None:
    """[if] no live lease exists (never taken, or lapsed) [then] a header-bearing PUT is a 409, [else stop]."""
    client, drain = _client(holder, expired=expired)
    response = _put(client, [], "tab-a")
    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "lease_lapsed", "holder": None}
    assert drain.next_up == ["leader-track"]


def test_control_unleased_writers_are_unchanged() -> None:
    """[if] a PUT carries no lease header (the CLI) [then] it applies with or without a lease, [else stop]."""
    for holder, expired in ((None, False), ("tab-b", True), ("tab-b", False)):
        client, drain = _client(holder, expired=expired)
        response = _put(client, ["t1"], None)
        assert response.status_code == 200, (holder, expired)
        assert drain.next_up == ["t1"]


def test_control_a_leader_whose_lease_lapsed_writes_again_once_its_mirror_publish_renews_it() -> None:
    """[if] the lapsed leader's next mirror PUT retakes the lease [then] its next-up PUT applies, [else stop]."""
    from datetime import UTC, datetime

    from apps.webui.server.routes import state

    client, drain = _client("tab-a", expired=True)
    client.app.include_router(state.router, prefix="/api/v1")
    assert _put(client, ["t1"], "tab-a").status_code == 409
    mirror = {"client_id": "tab-a", "published_at": datetime.now(UTC).isoformat()}
    renewed = client.put("/api/v1/state/ui-mirror", json=mirror, headers={LEASE_HEADER: "tab-a"})
    assert renewed.status_code == 202, renewed.text
    response = _put(client, ["t1"], "tab-a")
    assert response.status_code == 200 and drain.next_up == ["t1"]
