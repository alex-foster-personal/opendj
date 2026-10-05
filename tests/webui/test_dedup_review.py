"""Tests for the dedup review routes (duplicate-review-merge).

Regression one-liners:
  - if GET /dedup/clusters doesn't skip cleanly (200 + note) without a
    fingerprint db then broken
  - if GET /dedup/clusters doesn't hydrate members from the backend then broken
  - if the canonical member isn't flagged is_canonical=True then broken
  - if POST decision doesn't round-trip on the next GET then broken
  - if POST decision accepts a survivor outside the cluster then broken
  - if POST decision on an unknown cluster_id doesn't 404 then broken
  - if GET /dedup/clusters doesn't 200 (not 500) when the db exists but is empty then broken
  - if a fingerprint-only db makes the review endpoint 500 then broken
  - if a decision can attach to a rebuilt cluster that reused a numeric id then broken
  - if a stale decision revision overwrites a newer decision then broken
  - if a corrupt decision store is silently replaced then broken
  - if decision writers on separate processes can overlap then broken
  - if OpenAPI omits the stable cluster key or If-Match contract then broken
"""
from __future__ import annotations

import multiprocessing
import queue
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from apps.dedup import schema as dedup_schema
from apps.webui.server import dedup_decisions
from apps.webui.server.backend import InMemoryBackend, Track
from apps.webui.server.routes import dedup_review

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

# Cold spawn start re-imports this module (fastapi and friends) in each child,
# which can exceed 5s on a loaded machine. Generous deadlines keep the test
# honest about crash-vs-slow instead of flaking on load (observed 3/9 local
# failures, Thu 28 Aug 2026).
LOCK_TEST_READY_DEADLINE_S = 120.0
LOCK_TEST_JOIN_DEADLINE_S = 60.0


def _wait_for_start_or_raise(start: Any) -> None:
    """Fail loudly if the parent never fires the start event; no silent skew."""
    if not start.wait(timeout=LOCK_TEST_READY_DEADLINE_S):
        raise TimeoutError(
            f"start event not set within {LOCK_TEST_READY_DEADLINE_S}s"
        )


def _hold_decision_lock(
    decisions_path: str,
    ready: Any,
    start: Any,
    result: Any,
) -> None:
    """Process target proving the decision lock serializes real writers."""
    path = Path(decisions_path)
    dedup_decisions.DECISIONS_FILE = path
    dedup_review.DECISIONS_FILE = path
    ready.put(True)
    try:
        _wait_for_start_or_raise(start)
        with dedup_decisions.decision_file_lock():
            entered = time.monotonic()
            time.sleep(0.15)
            leaving = time.monotonic()
        result.put(("ok", entered, leaving))
    except BaseException as exc:
        result.put(("error", type(exc).__name__, str(exc)))


def _await_children_ready(ready: Any, processes: list[Any]) -> None:
    """Collect one readiness signal per child, distinguishing crashed from slow.

    A child that dies before signaling fails the test immediately with its
    exitcode (traceback lands in pytest's captured stderr); a slow child gets
    the full deadline before a timeout failure names the survivors.
    """
    deadline = time.monotonic() + LOCK_TEST_READY_DEADLINE_S
    signaled = 0
    while signaled < len(processes):
        try:
            assert ready.get(timeout=1.0) is True
            signaled += 1
            continue
        except queue.Empty:
            pass
        crashed = [
            (process.pid, process.exitcode)
            for process in processes
            if process.exitcode not in (None, 0)
        ]
        if crashed:
            raise AssertionError(
                f"lock-test child crashed before signaling ready, "
                f"(pid, exitcode): {crashed}; traceback is in captured stderr"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"only {signaled}/{len(processes)} lock-test children signaled "
                f"ready within {LOCK_TEST_READY_DEADLINE_S}s, exitcodes "
                f"{[process.exitcode for process in processes]}; a child this "
                f"slow is a hang, not load"
            )


def _seed_cluster_db(
    db_path: Path, *, cluster_id: int, canonical_sid: str, canonical_path: str,
    alias_sid: str, alias_path: str, similarity: float = 0.97,
    rationale: str = "bitrate=320",
) -> None:
    conn = dedup_schema.ensure_schema(db_path)
    try:
        conn.execute(
            "INSERT INTO duplicate_clusters "
            "(cluster_id, canonical_stable_id, canonical_path, rationale) "
            "VALUES (?, ?, ?, ?)",
            (cluster_id, canonical_sid, canonical_path, rationale),
        )
        conn.execute(
            "INSERT INTO track_aliases "
            "(alias_stable_id, alias_path, cluster_id, canonical_stable_id, similarity) "
            "VALUES (?, ?, ?, ?, ?)",
            (alias_sid, alias_path, cluster_id, canonical_sid, similarity),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seeded_backend() -> InMemoryBackend:
    backend = InMemoryBackend()
    backend.seed_track(Track(
        stable_id="track-canon", title="Midnight Drive", artist="Tamsin Quell",
        bpm=124.0, key="8A", duration_ms=210_000, rating=4,
    ))
    backend.seed_track(Track(
        stable_id="track-alias", title="Midnight Drive (128k)", artist="Tamsin Quell",
        bpm=124.0, key="8A", duration_ms=210_000, rating=None,
    ))
    return backend


@pytest.fixture
def dedup_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "phase7.sqlite"
    decisions_path = tmp_path / "review-decisions.json"
    monkeypatch.setattr(dedup_review.dedup_paths, "DEDUP_FALLBACK_DB", db_path)
    monkeypatch.setattr(dedup_review, "DECISIONS_FILE", decisions_path)
    monkeypatch.setattr(dedup_decisions, "DECISIONS_FILE", decisions_path)
    return db_path


@pytest.fixture
def app_client(seeded_backend: InMemoryBackend, dedup_db: Path):
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    app = create_app(
        backend=seeded_backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        yield c


def test_clusters_skip_cleanly_without_db(app_client) -> None:
    r = app_client.get("/api/v1/dedup/clusters")
    assert r.status_code == 200
    body = r.json()
    assert body["clusters"] == []
    assert body["note"] is not None
    assert "Find duplicates" in body["note"]


def test_clusters_empty_db_is_200(app_client, dedup_db: Path) -> None:
    dedup_schema.ensure_schema(dedup_db).close()
    r = app_client.get("/api/v1/dedup/clusters")
    assert r.status_code == 200
    assert r.json()["clusters"] == []
    assert r.json()["note"] is None


def test_clusters_fingerprint_only_db_is_empty_review_state(
    app_client, dedup_db: Path,
) -> None:
    conn = sqlite3.connect(dedup_db)
    try:
        conn.execute(
            "CREATE TABLE fingerprints ("
            "path TEXT PRIMARY KEY, stable_id TEXT, fingerprint TEXT NOT NULL, "
            "duration REAL NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    response = app_client.get("/api/v1/dedup/clusters")

    assert response.status_code == 200
    assert response.json()["clusters"] == []
    assert "find_clusters" in response.json()["note"]


def test_clusters_hydrate_members(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    r = app_client.get("/api/v1/dedup/clusters")
    assert r.status_code == 200
    clusters = r.json()["clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["cluster_id"] == 1
    assert cluster["cluster_key"].startswith("sha256:")
    assert cluster["survivor_stable_id"] == "track-canon"
    assert cluster["rationale"] == "bitrate=320"
    assert cluster["flagged_manual_review"] is False
    assert cluster["decision"] is None

    by_id = {m["stable_id"]: m for m in cluster["members"]}
    assert by_id["track-canon"]["is_canonical"] is True
    assert by_id["track-canon"]["title"] == "Midnight Drive"
    assert by_id["track-canon"]["similarity"] is None
    assert by_id["track-alias"]["is_canonical"] is False
    assert by_id["track-alias"]["similarity"] == pytest.approx(0.97)
    assert by_id["track-alias"]["artist"] == "Tamsin Quell"
    # Neither fixture path exists on disk.
    assert by_id["track-canon"]["file_exists"] is False
    assert by_id["track-alias"]["file_exists"] is False
    assert r.headers["etag"] == r.json()["revision"]


def test_clusters_surface_persisted_manual_review_warning(
    app_client, dedup_db: Path,
) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    with sqlite3.connect(dedup_db) as connection:
        connection.execute(
            "UPDATE duplicate_clusters SET flagged_manual_review = 1"
        )

    response = app_client.get("/api/v1/dedup/clusters")

    assert response.status_code == 200
    assert response.json()["clusters"][0]["flagged_manual_review"] is True


def test_decision_round_trip(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=7,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    cluster_response = app_client.get("/api/v1/dedup/clusters")
    cluster = cluster_response.json()["clusters"][0]
    r = app_client.post(
        "/api/v1/dedup/clusters/7/decision",
        headers={"If-Match": cluster_response.headers["etag"]},
        json={
            "cluster_key": cluster["cluster_key"],
            "survivor": "track-alias",
            "action": "merge",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cluster_id"] == 7
    assert body["cluster_key"] == cluster["cluster_key"]
    assert body["survivor"] == "track-alias"
    assert body["action"] == "merge"
    assert body["pending_apply"] is True
    assert body["decided_at"]
    assert body["revision"] == r.headers["etag"]

    r2 = app_client.get("/api/v1/dedup/clusters")
    decision = r2.json()["clusters"][0]["decision"]
    assert decision["survivor"] == "track-alias"
    assert decision["action"] == "merge"
    assert decision["cluster_key"] == cluster["cluster_key"]
    assert decision["pending_apply"] is True


def test_decision_rejects_non_member_survivor(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    current = app_client.get("/api/v1/dedup/clusters")
    r = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        headers={"If-Match": current.headers["etag"]},
        json={
            "cluster_key": current.json()["clusters"][0]["cluster_key"],
            "survivor": "not-in-cluster",
            "action": "merge",
        },
    )
    assert r.status_code == 422


def test_decision_unknown_cluster_404(app_client, dedup_db: Path) -> None:
    dedup_schema.ensure_schema(dedup_db).close()
    current = app_client.get("/api/v1/dedup/clusters")
    r = app_client.post(
        "/api/v1/dedup/clusters/999/decision",
        headers={"If-Match": current.headers["etag"]},
        json={
            "cluster_key": "sha256:missing",
            "survivor": "track-canon",
            "action": "skip",
        },
    )
    assert r.status_code == 404


def test_decision_no_db_404(app_client) -> None:
    r = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        headers={"If-Match": '"empty"'},
        json={
            "cluster_key": "sha256:missing",
            "survivor": "track-canon",
            "action": "skip",
        },
    )
    assert r.status_code == 404


def test_decision_rejects_bad_action(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    current = app_client.get("/api/v1/dedup/clusters")
    r = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        headers={"If-Match": current.headers["etag"]},
        json={
            "cluster_key": current.json()["clusters"][0]["cluster_key"],
            "survivor": "track-canon",
            "action": "delete-everything",
        },
    )
    assert r.status_code == 422


def test_decision_requires_if_match(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    current = app_client.get("/api/v1/dedup/clusters").json()

    response = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        json={
            "cluster_key": current["clusters"][0]["cluster_key"],
            "survivor": "track-canon",
            "action": "skip",
        },
    )

    assert response.status_code == 428


def test_stale_revision_cannot_overwrite_decision(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    current = app_client.get("/api/v1/dedup/clusters")
    cluster_key = current.json()["clusters"][0]["cluster_key"]
    stale_revision = current.headers["etag"]
    first = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        headers={"If-Match": stale_revision},
        json={
            "cluster_key": cluster_key,
            "survivor": "track-canon",
            "action": "keep-all",
        },
    )
    assert first.status_code == 200

    stale = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        headers={"If-Match": stale_revision},
        json={
            "cluster_key": cluster_key,
            "survivor": "track-alias",
            "action": "merge",
        },
    )

    assert stale.status_code == 409
    assert stale.headers["etag"] == first.headers["etag"]
    latest = app_client.get("/api/v1/dedup/clusters").json()["clusters"][0]
    assert latest["decision"]["action"] == "keep-all"
    assert latest["decision"]["survivor"] == "track-canon"


def test_rebuilt_cluster_id_does_not_inherit_stale_decision(
    app_client, dedup_db: Path,
) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=4,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    original = app_client.get("/api/v1/dedup/clusters")
    original_cluster = original.json()["clusters"][0]
    decided = app_client.post(
        "/api/v1/dedup/clusters/4/decision",
        headers={"If-Match": original.headers["etag"]},
        json={
            "cluster_key": original_cluster["cluster_key"],
            "survivor": "track-canon",
            "action": "merge",
        },
    )
    assert decided.status_code == 200

    conn = sqlite3.connect(dedup_db)
    try:
        conn.execute("DELETE FROM track_aliases")
        conn.execute("DELETE FROM duplicate_clusters")
        conn.execute(
            "INSERT INTO duplicate_clusters "
            "(cluster_id, canonical_stable_id, canonical_path, rationale) "
            "VALUES (4, 'replacement-canon', '/music/new.flac', 'new')"
        )
        conn.execute(
            "INSERT INTO track_aliases "
            "(alias_stable_id, alias_path, cluster_id, canonical_stable_id, similarity) "
            "VALUES ('replacement-alias', '/music/new.mp3', 4, 'replacement-canon', 0.99)"
        )
        conn.commit()
    finally:
        conn.close()

    rebuilt = app_client.get("/api/v1/dedup/clusters")
    rebuilt_cluster = rebuilt.json()["clusters"][0]
    assert rebuilt_cluster["cluster_key"] != original_cluster["cluster_key"]
    assert rebuilt_cluster["decision"] is None

    stale = app_client.post(
        "/api/v1/dedup/clusters/4/decision",
        headers={"If-Match": rebuilt.headers["etag"]},
        json={
            "cluster_key": original_cluster["cluster_key"],
            "survivor": "track-canon",
            "action": "merge",
        },
    )
    assert stale.status_code == 409


@pytest.mark.parametrize(
    "corrupt",
    [
        b'{"schema_version": 1, "decisions": ',
        b'{"decisions": {}}',
    ],
    ids=["truncated-json", "missing-schema-version"],
)
def test_corrupt_decision_store_fails_without_overwrite(
    app_client, dedup_db: Path, corrupt: bytes,
) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    dedup_review.DECISIONS_FILE.write_bytes(corrupt)

    response = app_client.get("/api/v1/dedup/clusters")

    assert response.status_code == 500
    assert response.json()["detail"]["error"] == "invalid_decision_store"
    assert dedup_review.DECISIONS_FILE.read_bytes() == corrupt


def test_decision_file_lock_serializes_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    result = context.Queue()
    decisions_path = str(tmp_path / "review-decisions.json")
    processes = [
        context.Process(
            target=_hold_decision_lock,
            args=(decisions_path, ready, start, result),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        _await_children_ready(ready, processes)
        start.set()
        join_deadline = time.monotonic() + LOCK_TEST_JOIN_DEADLINE_S
        for process in processes:
            process.join(timeout=max(0.1, join_deadline - time.monotonic()))
            if process.exitcode is None:
                raise AssertionError(
                    f"lock-test child pid {process.pid} still running "
                    f"{LOCK_TEST_JOIN_DEADLINE_S}s after start; hang, not load"
                )
            assert process.exitcode == 0, (
                f"lock-test child pid {process.pid} exited "
                f"{process.exitcode}; traceback is in captured stderr"
            )
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

    intervals = [result.get(timeout=10) for _ in processes]
    assert all(interval[0] == "ok" for interval in intervals), intervals
    first, second = sorted(intervals, key=lambda interval: interval[1])
    assert first[2] <= second[1]


def test_openapi_documents_dedup_cas_contract(app_client) -> None:
    operation = app_client.app.openapi()["paths"][
        "/api/v1/dedup/clusters/{cluster_id}/decision"
    ]["post"]
    if_match = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "If-Match"
    )
    assert if_match["required"] is True
    assert {"200", "409", "428"} <= set(operation["responses"])


def test_duplicate_cluster_rows_fail_get(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    _seed_cluster_db(
        dedup_db, cluster_id=2,
        canonical_sid="track-canon", canonical_path="/music/canon-copy.flac",
        alias_sid="track-alias", alias_path="/music/alias-copy.mp3",
    )
    response = app_client.get("/api/v1/dedup/clusters")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_cluster_identity"
    assert "sha256:" in detail["message"]
