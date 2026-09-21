"""Adversarial hardening round 1 -- library management surface (LIBMX-01..10 wave).

See specs/library-hardening-adversarial-testing.md for the round log. Each
test below reproduces one concrete finding from direct code reading, not a
hypothesis -- run against ``origin/main`` before any fix lands here to get
the round's "before" score, then again after the fix for "after".

Regression one-liners:
  - if a whitespace-only smartlist name is accepted by create/duplicate then broken
  - if a deeply nested smartlist rule raises RecursionError instead of a clean
    domain error then broken
  - if a >100-row bulk-edit or my-tag batch is accepted unbounded then broken
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.smartlists.schema import SmartlistRuleError, validate_rule
from apps.shared.state import db as state_db
from apps.smartlists.repo import SmartlistsRepo
from apps.webui.server.app import create_app
from apps.webui.server.routes import smartlists as smartlists_routes
from apps.webui.server.sqlite_backend import SqliteBackend

# request_guard's host allowlist (issue #2689) rejects TestClient's default
# Host ("testserver"); "test-host" is the hostname= every app below is given.
_BASE_URL = "http://test-host"

_BASE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _seed_state_db(path: Path) -> None:
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        ensure_phase08_tables(conn)
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, "
            "artists_json, album, isrc, duration_ms, file_path, "
            "content_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "hard-track-001", "inferred", "Probe", json.dumps(["Tester"]),
                None, None, 300000, None, None,
                _BASE.isoformat(), _BASE.isoformat(),
            ),
        )
    finally:
        conn.close()


@pytest.fixture
def smartlists_client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "state.db"
    _seed_state_db(db_path)
    app = create_app(
        backend=SqliteBackend(db_path), bind_host="127.0.0.1", hostname="test-host",
        state_db_path=str(db_path), mount_frontend=False,
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    with TestClient(app, base_url=_BASE_URL) as c:
        yield c


def _nested_not_rule(depth: int) -> dict:
    """``depth`` levels of {"op": "not", "children": [...]}} around a leaf."""
    rule: dict = {"field": "rating", "op": ">=", "value": 0}
    for _ in range(depth):
        rule = {"op": "not", "children": [rule]}
    return rule


# ----- Finding 1: whitespace-only smartlist name accepted by create/duplicate


def test_create_smartlist_rejects_whitespace_only_name(smartlists_client):
    """update_rule already rejects this (SmartlistsRepo.update_rule's
    ``name.strip()`` guard); create() has no equivalent guard."""
    r = smartlists_client.post(
        "/api/v1/smartlists",
        json={"name": "   ", "rule": {"field": "rating", "op": ">=", "value": 0}},
    )
    assert r.status_code == 422, (
        f"whitespace-only name was accepted (status {r.status_code}): {r.text}"
    )


def test_duplicate_smartlist_rejects_whitespace_only_name(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_state_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        sid = SmartlistsRepo(conn).create(
            "Source", {"field": "rating", "op": ">=", "value": 0},
        ).id
        conn.commit()
    finally:
        conn.close()
    app = create_app(
        backend=SqliteBackend(db_path), bind_host="127.0.0.1", hostname="test-host",
        state_db_path=str(db_path), mount_frontend=False,
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    with TestClient(app, base_url=_BASE_URL) as c:
        r = c.post(f"/api/v1/smartlists/{sid}/duplicate", json={"name": "  "})
    assert r.status_code == 422, (
        f"whitespace-only duplicate name was accepted (status {r.status_code}): {r.text}"
    )


# ----- Finding 2: unbounded smartlist rule recursion -> RecursionError -> 500
#
# A depth deep enough to matter (thousands of levels) blows Python's own
# recursion limit while httpx's json encoder serializes the OUTGOING request
# body -- before the request ever reaches the server -- so the HTTP round
# trip cannot exercise the real crash site. Call the validator directly
# instead: this is also the more precise test, since it targets the exact
# function this finding is about rather than a proxy for it.


def test_validate_rule_rejects_deep_nesting_instead_of_crashing():
    deep_rule = _nested_not_rule(5000)
    try:
        validate_rule(deep_rule)
    except RecursionError:
        pytest.fail(
            "validate_rule() let a RecursionError escape instead of raising "
            "a clean SmartlistRuleError -- no route handler catches "
            "RecursionError, so this would surface as a bare 500"
        )
    except SmartlistRuleError:
        pass
    else:
        pytest.fail("deeply nested rule was accepted with no depth limit")


def test_create_smartlist_rejects_over_limit_nesting_over_http(smartlists_client):
    """Depth 40 is small enough to encode/decode trivially, so this
    isolates the missing-depth-*policy* bug (any depth is currently
    accepted) from the separate RecursionError-crash bug covered above."""
    over_limit_rule = _nested_not_rule(40)
    r = smartlists_client.post(
        "/api/v1/smartlists", json={"name": "Deep", "rule": over_limit_rule},
    )
    assert r.status_code == 422, (
        f"a rule nested 40 levels deep was accepted (status {r.status_code}): "
        f"{r.text[:500]}"
    )
    assert r.json().get("detail", {}).get("code") == "SMARTLIST_RULE_INVALID"


# ----- Finding 3: bulk_edit / my-tag batches have no size cap (unlike find-replace)


def test_bulk_edit_rejects_batch_over_find_replace_precedent_cap(client, seed_backend):
    """find_replace.py caps stable_ids at 100 (_MAX_STABLE_IDS); bulk_edit's
    sibling field has no max_length at all, so a batch this large sails past
    input validation and is only stopped (404, not 422) once the backend
    looks up the first id that does not exist -- proving there is no early
    size-based rejection."""
    stable_ids = [f"track-nonexistent-{i:06d}" for i in range(500)]
    expected_etags = {sid: "irrelevant" for sid in stable_ids}
    r = client.patch(
        "/api/v1/bulk-edit",
        json={"stable_ids": stable_ids, "expected_etags": expected_etags, "rating": 3},
    )
    assert r.status_code == 422, (
        f"500-row bulk-edit batch was not rejected by input validation "
        f"(status {r.status_code}): {r.text[:500]}"
    )


def test_mytag_assign_rejects_batch_over_find_replace_precedent_cap(client, seed_backend):
    stable_ids = [f"track-nonexistent-{i:06d}" for i in range(500)]
    expected_etags = {sid: "irrelevant" for sid in stable_ids}
    r = client.post(
        "/api/v1/mytags/assign",
        json={"stable_ids": stable_ids, "expected_etags": expected_etags, "add": ["probe"]},
    )
    assert r.status_code == 422, (
        f"500-row my-tag batch was not rejected by input validation "
        f"(status {r.status_code}): {r.text[:500]}"
    )
