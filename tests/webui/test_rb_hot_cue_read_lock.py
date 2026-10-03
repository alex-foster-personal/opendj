"""Hot-cue reads stay open while the CloudSync writer lock is closed (CUES-01).

Regression one-liners:
  - if GET hot-cues is write-gated then a peer holding the writer lock, or a
    lock probe outage, fails every deck load that reads its cue slots
  - if PUT/DELETE hot-cues stop being write-gated then a write lands while
    another machine holds the lock
"""
from __future__ import annotations

from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from tests.webui.test_rb_hot_cue_write import STABLE_ID, client  # noqa: F401  # fixture

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]


def _probe_down() -> NoReturn:
    raise ConnectionError("cloud lock probe unreachable")


def test_hot_cue_reads_ignore_the_writer_lock_and_writes_do_not(
    client: TestClient,  # noqa: F811  # fixture
) -> None:
    url = f"/api/v1/tracks/{STABLE_ID}/hot-cues"
    revision = next(row["revision"] for row in client.get(url).json() if row["slot"] == "A")
    client.app.state.lock_status_fn = _probe_down
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert [row["slot"] for row in response.json()][:1] == ["A"]
    # Control: the same lock state still refuses a write.
    put = client.put(f"{url}/A", json={"in_ms": 1_000}, headers={"If-Match": revision})
    assert put.status_code == 503, put.text
    assert client.delete(f"{url}/A", headers={"If-Match": revision}).status_code == 503
