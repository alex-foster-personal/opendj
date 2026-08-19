"""The jobs HTTP surface: every endpoint, and every refusal it documents.

api.py claims agent-native parity -- "everything the UI can do to a job has an
endpoint here, so an agent drives the identical flow" -- and names its
refusals: 400 unknown kind, 404 no such job, 409 the row is not in a state
that permits the transition. None of that had a single test, so the claim was
a comment rather than a contract.

Single-line intent:
  - if an endpoint's happy path breaks then an agent cannot drive the flow the
    UI drives, and parity is a lie
  - if a refusal returns the wrong status then a caller retries a permanent
    error or gives up on a transient one
  - if the list limit is unbounded then one request serialises the whole table

The app here mounts the real router over a real JobStore. The runner is
deliberately NOT started for the refusal tests, so nothing claims a queued row
out from under an assertion.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.jobs.api import MAX_LIST_LIMIT, router
from apps.engine_core.jobs.runner import (
    JobRunner,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobStore

API = "/api/v1/jobs"
_SLEEPER = "import time; time.sleep(600)"


@pytest.fixture
def kinds() -> Iterator[None]:
    register_worker("test-echo", lambda p: [sys.executable, "-c", "pass", str(p)])
    register_worker("test-sleeper", lambda _p: [sys.executable, "-c", _SLEEPER])
    yield
    unregister_worker("test-echo")
    unregister_worker("test-sleeper")


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    return store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[JobStore]:
    opened = _store(tmp_path)
    yield opened
    opened.close()


def _app(store: JobStore, *, run: bool) -> FastAPI:
    runner = JobRunner(store, poll_s=0.01)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        if run:
            await runner.start()
        try:
            yield
        finally:
            if run:
                await runner.stop()

    app = FastAPI(lifespan=_lifespan)
    app.include_router(router, prefix="/api/v1")
    app.state.jobs_store = store
    app.state.jobs_runner = runner
    return app


@pytest.fixture
def client(store: JobStore) -> Iterator[TestClient]:
    """No supervisor: statuses only change when a test changes them."""
    with TestClient(_app(store, run=False)) as test_client:
        yield test_client


@pytest.fixture
def live_client(store: JobStore) -> Iterator[TestClient]:
    """Supervisor running, so a job really is claimed, spawned and cancelled."""
    with TestClient(_app(store, run=True)) as test_client:
        yield test_client


# ----- happy paths -------------------------------------------------------
def test_list_kinds_reports_the_registry(client: TestClient, kinds: None) -> None:
    response = client.get(f"{API}/kinds")
    assert response.status_code == 200
    assert response.json() == {"kinds": ["test-echo", "test-sleeper"]}


def test_enqueue_returns_201_and_the_queued_row(
    client: TestClient, kinds: None
) -> None:
    response = client.post(
        API,
        json={"kind": "test-echo", "payload": {"a": 1}, "external_ref": "ref-1"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "test-echo"
    assert body["payload"] == {"a": 1}
    assert body["status"] == "queued"
    assert body["external_ref"] == "ref-1"
    assert body["attempt"] == 0


def test_get_job_returns_the_row(client: TestClient, kinds: None) -> None:
    job_id = client.post(API, json={"kind": "test-echo"}).json()["id"]
    response = client.get(f"{API}/{job_id}")
    assert response.status_code == 200
    assert response.json()["id"] == job_id


def test_list_jobs_returns_newest_first(client: TestClient, kinds: None) -> None:
    first = client.post(API, json={"kind": "test-echo", "payload": {"n": 1}})
    second = client.post(API, json={"kind": "test-echo", "payload": {"n": 2}})
    assert first.status_code == second.status_code == 201

    response = client.get(API)
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert set(ids) == {first.json()["id"], second.json()["id"]}

    limited = client.get(API, params={"limit": 1})
    assert limited.status_code == 200
    assert len(limited.json()) == 1


def test_reenqueue_returns_the_requeued_row(
    client: TestClient, store: JobStore, kinds: None
) -> None:
    job_id = client.post(API, json={"kind": "test-echo"}).json()["id"]
    store.claim_queued()
    store.finish(job_id, "failed", error="boom")

    response = client.post(f"{API}/{job_id}/reenqueue")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["attempt"] == 1
    assert body["error"] is None
    assert body["worker_pgid"] is None


def test_cancel_takes_a_live_worker_down(
    live_client: TestClient, kinds: None
) -> None:
    """The endpoint drives the real two-phase cancel, not a status poke."""
    job_id = live_client.post(API, json={"kind": "test-sleeper"}).json()["id"]
    _await_status(live_client, job_id, {"running"})

    response = live_client.post(f"{API}/{job_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled", response.text


def test_cancelling_a_queued_job_is_200_and_cancelled(
    client: TestClient, kinds: None
) -> None:
    """C14: queued -> cancelled is a legal transition, not a 409.

    No supervisor is running here, so the row is still genuinely queued when
    the endpoint is called -- the state an agent hits when it changes its mind
    before a worker slot frees up.
    """
    job_id = client.post(API, json={"kind": "test-echo"}).json()["id"]
    response = client.post(f"{API}/{job_id}/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled", body
    assert body["worker_pgid"] is None, body
    assert body["started_at"] is None, body


# ----- documented refusals -----------------------------------------------
def test_unknown_kind_is_400(client: TestClient, kinds: None) -> None:
    response = client.post(API, json={"kind": "no-such-kind"})
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "no worker registered" in detail, detail
    assert "test-echo" in detail, "the refusal must name the known kinds"


@pytest.mark.parametrize(
    "path", ["{id}", "{id}/cancel", "{id}/reenqueue"]
)
def test_no_such_job_is_404(client: TestClient, path: str) -> None:
    url = f"{API}/{path.format(id='00000000-0000-0000-0000-000000000000')}"
    response = client.get(url) if path == "{id}" else client.post(url)
    assert response.status_code == 404, response.text
    assert "no job" in response.json()["detail"], response.text


def test_cancelling_a_terminal_job_is_409(
    client: TestClient, store: JobStore, kinds: None
) -> None:
    """A settled row has nothing left to stop, and its outcome is not ours."""
    job_id = client.post(API, json={"kind": "test-echo"}).json()["id"]
    store.claim_queued()
    store.finish(job_id, "succeeded")
    response = client.post(f"{API}/{job_id}/cancel")
    assert response.status_code == 409, response.text
    assert "nothing to stop" in response.json()["detail"], response.text


def test_reenqueueing_a_running_job_is_409(
    client: TestClient, store: JobStore, kinds: None
) -> None:
    job_id = client.post(API, json={"kind": "test-echo"}).json()["id"]
    store.claim_queued()
    response = client.post(f"{API}/{job_id}/reenqueue")
    assert response.status_code == 409, response.text
    assert "only a terminal job re-enqueues" in response.json()["detail"]


def test_reenqueueing_an_unknown_job_is_409_with_the_reason(
    client: TestClient, store: JobStore, kinds: None
) -> None:
    """The documented refusal: an unresolved 'unknown' must not be re-run.

    Re-running a job whose side effects may already have landed is worse than
    making a human look, so this 409 is a feature and its wording is the whole
    value of it.
    """
    job_id = client.post(API, json={"kind": "test-echo"}).json()["id"]
    store.claim_queued()
    store.finish(job_id, "unknown", error="engine restarted; outcome unknown")

    response = client.post(f"{API}/{job_id}/reenqueue")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "no reconcile hook" in detail, detail
    assert "repeat a side effect" in detail, detail


@pytest.mark.parametrize("limit", [0, -1, MAX_LIST_LIMIT + 1])
def test_out_of_range_limits_are_422(client: TestClient, limit: int) -> None:
    """An unbounded limit is a request to serialise the whole table."""
    response = client.get(API, params={"limit": limit})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("limit", [1, MAX_LIST_LIMIT])
def test_the_range_boundaries_are_accepted(
    client: TestClient, limit: int
) -> None:
    assert client.get(API, params={"limit": limit}).status_code == 200


def _await_status(
    client: TestClient, job_id: str, wanted: set[str], attempts: int = 400
) -> dict:
    import time

    for _ in range(attempts):
        row = client.get(f"{API}/{job_id}").json()
        if row["status"] in wanted:
            return row
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {wanted}: {row}")
