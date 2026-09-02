"""The setup HTTP surface: every step an agent has to be able to drive.

The router claims agent-native parity -- five endpoints, one per wizard
step, with refusals that carry codes. None of that is true until something
asserts it, so this module walks the whole flow over HTTP with no browser
anywhere.

Single-line intent:
  - if an endpoint disappears from the committed contract then an agent
    driving the wizard breaks with no test failing
  - if two imports can run at once then two writers race into state.db
  - if a refusal loses its code then a caller has to parse prose to branch
  - if the stems step ever claims availability it does not have then the
    house rule against mocked UI is broken at the API layer
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.build_info import (
    CODE_BUILD_IDENTITY_UNAVAILABLE,
    BuildIdentity,
)
from apps.engine_core.config import EngineConfig
from apps.engine_core.jobs.runner import (
    known_kinds,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.setup import detect, record
from apps.engine_core.setup.api import STEMS_UNAVAILABLE_MESSAGE, router
from apps.engine_core.setup.jobs import SETUP_IMPORT_KIND
from apps.stems.job import JOB_KIND as STEMS_JOB_KIND
from tests.engine_core.conftest import build_identity

API = "/api/v1/setup"

COMMITTED_OPENAPI: Path = (
    Path(__file__).resolve().parents[2] / "apps" / "webui" / "openapi.json"
)

SETUP_PATHS: tuple[str, ...] = (
    "/api/v1/setup/status",
    "/api/v1/setup/detect/rekordbox",
    "/api/v1/setup/import",
    "/api/v1/setup/dismiss",
    "/api/v1/setup/stems",
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


def _build_client(
    data_dir: Path, jobs_db: Path, identity: BuildIdentity
) -> Iterator[TestClient]:
    """The real router over a real JobStore. The runner is NOT started, so
    nothing claims a queued row out from under an assertion."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    store = JobStore(jobs_db, boot_id="boot-setup", owner_pid=os.getpid())
    store.recover()
    app.state.engine_cfg = EngineConfig(data_dir=data_dir)
    app.state.jobs_store = store
    app.state.build_identity = identity
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        store.close()


@pytest.fixture
def client(data_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    """An INSTALLED build: the wizard is the first thing a new user meets.

    ``payload`` is the identity the shipped dmg reports, so every existing
    assertion in this file keeps describing the case it was written for.
    The developer-checkout case gets its own fixture below.
    """
    yield from _build_client(
        data_dir, tmp_path / "jobs.db", build_identity("payload")
    )


@pytest.fixture
def dev_client(data_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    """The same engine running out of a checkout (``source='repo'``)."""
    yield from _build_client(
        data_dir, tmp_path / "jobs.db", build_identity("repo")
    )


@pytest.fixture
def unidentified_client(data_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    """A build that cannot say what it is. There is no third mode to pick."""
    yield from _build_client(
        data_dir,
        tmp_path / "jobs.db",
        BuildIdentity(
            info=None,
            failure=(
                "OPENDJ_PAYLOAD_MANIFEST=/nowhere/manifest.json but no file "
                "is there."
            ),
        ),
    )


def _with_plain_copy(data_dir: Path, source: Path) -> Path:
    """Copy the resolved rekordbox fixture DB into ``data_dir``.

    ``source`` is always the ``rb_plain_db_path`` fixture (root
    ``conftest.py``), which resolves through ``tests.fixtures._resolver``,
    fails closed on a missing fixture host, and checksum-verifies -- rather
    than a hard-coded in-repo path (PR #718 review).
    """
    destination = data_dir / detect.PLAIN_COPY_NAME
    shutil.copy2(source, destination)
    return destination


def _no_rekordbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "no-rekordbox"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "apps.shared.platform_paths.rekordbox_app_dir", lambda: empty
    )


# ----- status -------------------------------------------------------------
def test_a_fresh_data_dir_reports_a_first_run(client: TestClient) -> None:
    body = client.get(f"{API}/status").json()
    assert body["library_empty"] is True
    assert body["tracks"] == 0
    assert body["should_show_wizard"] is True
    assert body["last_import"] is None


def test_status_names_the_stages_the_import_will_report(
    client: TestClient,
) -> None:
    """The wizard renders its own progress list from this, not a hardcoded one."""
    assert client.get(f"{API}/status").json()["stages"] == [
        "detect",
        "snapshot",
        "decrypt",
        "ingest",
        "analysis",
    ]


def test_a_populated_library_stops_the_wizard_showing(
    client: TestClient, data_dir: Path
) -> None:
    from apps.shared.state import db as state_db
    from apps.shared.state.writer import StateWriter

    state_path = data_dir / "state" / detect.STATE_DB_NAME
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test")
    try:
        writer.upsert_track(
            stable_id="a" * 40,
            stable_id_tier="inferred",
            title="a track",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=None,
            file_path=None,
            content_hash=None,
        )
    finally:
        writer.close()
        conn.close()

    body = client.get(f"{API}/status").json()
    assert body["library_empty"] is False
    assert body["tracks"] == 1
    assert body["should_show_wizard"] is False


# ----- dev mode -----------------------------------------------------------
# The wizard is for someone who just installed the app, not for the developer
# who is running the engine out of their own checkout with an empty scratch
# data dir. The engine already knows which it is -- /api/v1/build-info calls
# it `source` -- so the gate is derived from that rather than from a second,
# guessable signal.
#
# - if a repo checkout still auto-triggers the wizard then every dev boot
#   with a fresh --data-dir lands on setup instead of the library -> broken
# - if the suppression is invisible then nobody can tell a dev-mode skip from
#   a dismissal, and the bug report says "the wizard never appears" -> broken
# - if an unreadable build identity picks a mode anyway then an installed
#   build with a broken manifest silently behaves like a checkout -> broken
def test_a_developer_checkout_never_auto_triggers_the_wizard(
    dev_client: TestClient,
) -> None:
    """Empty library, nothing dismissed, and still no wizard: source=repo."""
    body = dev_client.get(f"{API}/status").json()
    assert body["library_empty"] is True
    assert body["dismissed"] is False
    assert body["dev_mode"] is True
    assert body["should_show_wizard"] is False


def test_an_installed_build_still_shows_the_wizard_on_a_first_run(
    client: TestClient,
) -> None:
    """source=payload is unchanged: empty and not dismissed means show it."""
    body = client.get(f"{API}/status").json()
    assert body["library_empty"] is True
    assert body["dismissed"] is False
    assert body["dev_mode"] is False
    assert body["should_show_wizard"] is True


def test_status_refuses_when_the_build_cannot_say_what_it_is(
    unidentified_client: TestClient,
) -> None:
    """No guess, no default: the same 503 and reason /build-info gives."""
    response = unidentified_client.get(f"{API}/status")
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == CODE_BUILD_IDENTITY_UNAVAILABLE
    assert "OPENDJ_PAYLOAD_MANIFEST" in detail["message"]


def test_dismiss_reports_dev_mode_too(dev_client: TestClient) -> None:
    """/dismiss returns the same status model, so it carries the same flag."""
    body = dev_client.post(f"{API}/dismiss", json={"dismissed": False}).json()
    assert body["dev_mode"] is True
    assert body["should_show_wizard"] is False


# ----- detect -------------------------------------------------------------
def test_detect_reports_the_real_path_it_would_import(
    client: TestClient, data_dir: Path, rb_plain_db_path: Path
) -> None:
    plain = _with_plain_copy(data_dir, rb_plain_db_path)
    body = client.get(f"{API}/detect/rekordbox").json()
    assert body["import_source"] == str(plain)
    assert body["import_source_encrypted"] is False
    assert body["plain_copy"]["exists"] is True
    assert body["plain_copy"]["size_bytes"] == plain.stat().st_size


def test_detect_says_so_when_there_is_no_rekordbox(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_rekordbox(monkeypatch, tmp_path)
    body = client.get(f"{API}/detect/rekordbox").json()
    assert body["installed"] is False
    assert body["import_source"] is None
    assert detect.CODE_REKORDBOX_NOT_FOUND in body["blockers"]


def test_detect_always_explains_the_key_verdict(client: TestClient) -> None:
    body = client.get(f"{API}/detect/rekordbox").json()
    assert isinstance(body["key_available"], bool)
    assert body["key_detail"].strip() != ""


# ----- import -------------------------------------------------------------
def test_import_enqueues_the_registered_kind(
    client: TestClient, data_dir: Path, rb_plain_db_path: Path
) -> None:
    _with_plain_copy(data_dir, rb_plain_db_path)
    response = client.post(f"{API}/import", json={})
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["kind"] == SETUP_IMPORT_KIND
    assert job["status"] == "queued"
    assert job["payload"]["data_dir"] == str(data_dir)


def test_import_passes_its_options_into_the_job_payload(
    client: TestClient, data_dir: Path, rb_plain_db_path: Path
) -> None:
    source = _with_plain_copy(data_dir, rb_plain_db_path)
    job = client.post(
        f"{API}/import",
        json={"source": str(source), "limit": 3, "refresh_decrypt": True},
    ).json()
    assert job["payload"]["source"] == str(source)
    assert job["payload"]["limit"] == 3
    assert job["payload"]["refresh_decrypt"] is True


def test_import_refuses_a_second_run_while_one_is_live(
    client: TestClient, data_dir: Path, rb_plain_db_path: Path
) -> None:
    _with_plain_copy(data_dir, rb_plain_db_path)
    first = client.post(f"{API}/import", json={})
    assert first.status_code == 202

    second = client.post(f"{API}/import", json={})
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == detect.CODE_IMPORT_ALREADY_RUNNING
    assert first.json()["id"] in detail["message"]


def test_import_refuses_before_enqueueing_when_nothing_is_installed(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A doomed job is worse than a refusal: it hides the reason in a row."""
    _no_rekordbox(monkeypatch, tmp_path)
    response = client.post(f"{API}/import", json={})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == detect.CODE_REKORDBOX_NOT_FOUND
    assert "no rekordbox database was found" in detail["message"]


def test_a_missing_share_dir_does_not_block_the_import(
    client: TestClient, data_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, rb_plain_db_path: Path,
) -> None:
    """Tracks land either way; only the waveforms are missing. Reported, not
    refused -- refusing would deny an operator a usable library over a
    problem they can fix afterwards."""
    _with_plain_copy(data_dir, rb_plain_db_path)
    monkeypatch.setattr(
        "apps.shared.platform_paths.compute_share_root",
        lambda: tmp_path / "no-share-dir",
    )
    assert detect.CODE_SHARE_MISSING in (
        client.get(f"{API}/detect/rekordbox").json()["blockers"]
    )
    assert client.post(f"{API}/import", json={}).status_code == 202


def test_import_refuses_a_source_it_cannot_build_a_worker_from(
    client: TestClient, data_dir: Path, rb_plain_db_path: Path
) -> None:
    _with_plain_copy(data_dir, rb_plain_db_path)
    response = client.post(f"{API}/import", json={"source": "   "})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "setup_payload_invalid"


# ----- dismiss ------------------------------------------------------------
def test_dismiss_persists_and_is_reversible(
    client: TestClient, data_dir: Path
) -> None:
    body = client.post(f"{API}/dismiss", json={"dismissed": True}).json()
    assert body["dismissed"] is True
    assert body["should_show_wizard"] is False
    assert record.read(data_dir).dismissed is True

    # Re-enterable from settings: the flag goes back the other way.
    body = client.post(f"{API}/dismiss", json={"dismissed": False}).json()
    assert body["dismissed"] is False
    assert body["should_show_wizard"] is True


def test_dismissal_survives_a_new_client(
    client: TestClient, data_dir: Path
) -> None:
    """Engine-side, not localStorage -- so an agent sees the same state."""
    client.post(f"{API}/dismiss", json={"dismissed": True})
    assert record.record_path(data_dir).is_file()
    assert client.get(f"{API}/status").json()["dismissed"] is True


# ----- stems --------------------------------------------------------------
@pytest.fixture
def unwired_stems() -> Iterator[None]:
    """A chassis that never wired the stems kind.

    The registry is process-global and any earlier test that built a full
    engine leaves ``stems.separate`` in it, so the unavailable branch has to
    be arranged rather than assumed -- otherwise this file passes or fails
    on test ORDER, which is the opposite of evidence.

    Restored through the composition root rather than by hand, because
    ``unregister_worker`` drops the kind's observer and reconciler too and
    putting back only the worker would leave the session subtly wrong.
    """
    from apps.engine_core.app import _register_job_kinds

    was_wired = STEMS_JOB_KIND in known_kinds()
    unregister_worker(STEMS_JOB_KIND)
    yield
    if was_wired:
        _register_job_kinds()


def test_the_stems_step_says_no_when_the_kind_is_not_registered(
    client: TestClient, unwired_stems: None
) -> None:
    """The tester's own words, not a shrug: no worker, no library pass."""
    body = client.get(f"{API}/stems").json()
    assert body["available"] is False
    assert body["reason"] == STEMS_UNAVAILABLE_MESSAGE


def test_the_stems_step_says_yes_once_the_kind_is_registered(
    client: TestClient, unwired_stems: None
) -> None:
    """af--stems-modal landed, so 'not yet available' became the lie.

    The verdict is read off the registry, which means registering the kind
    here -- exactly what the composition root does -- flips it.
    """
    register_worker(STEMS_JOB_KIND, lambda _payload: ["true"])
    body = client.get(f"{API}/stems").json()
    assert body["available"] is True
    assert body["reason"] != STEMS_UNAVAILABLE_MESSAGE
    assert body["job_kind"] == STEMS_JOB_KIND
    assert body["plan_endpoint"] == "/api/v1/stems/plan"
    assert body["enqueue_endpoint"] == "/api/v1/jobs"


def test_the_stems_step_still_returns_real_tiers(client: TestClient) -> None:
    """The choice UI renders real rungs. Nothing here is a placeholder."""
    from apps.stems import tiers as tiercfg

    body = client.get(f"{API}/stems").json()
    assert [tier["key"] for tier in body["tiers"]] == [
        tier.key for tier in tiercfg.ladder()
    ]
    assert body["per_track_endpoint"] == "/api/v1/stems/generate"
    for tier in body["tiers"]:
        assert tier["availability"] in {"AVAILABLE", "NOT_APPLICABLE"}
        if tier["availability"] == "NOT_APPLICABLE":
            assert tier["unavailable_because"].strip() != ""


# ----- parity -------------------------------------------------------------
def test_every_setup_endpoint_is_in_the_committed_contract() -> None:
    """The published document is what the generated client is built from.

    An endpoint that exists in the app but not here is one the frontend
    cannot call in a typed way and an agent cannot discover.
    """
    document: dict[str, Any] = json.loads(
        COMMITTED_OPENAPI.read_text(encoding="utf-8")
    )
    missing = [path for path in SETUP_PATHS if path not in document["paths"]]
    assert not missing, (
        f"setup routes absent from the committed contract: {missing}. "
        "Run `just openapi-dump` and regenerate the frontend types."
    )
