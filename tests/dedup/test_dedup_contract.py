"""The dedup contract (ADR-NEW-module-contracts, M1): service and models."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.dedup import contract
from apps.dedup.contract.models import DedupService, ScanStatus
from apps.dedup.http import build_service

_IDLE = {
    "state": "idle",
    "total": 0,
    "done": 0,
    "computed": 0,
    "cache_hits": 0,
    "not_local": 0,
    "errors": 0,
    "clusters": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "error_samples": [],
}


def test_built_service_satisfies_the_protocol(tmp_path: Path) -> None:
    service = build_service()
    assert isinstance(service, DedupService)
    assert not hasattr(DedupService, "ensure_schema"), "an open connection must not cross the service"
    contract.ensure_schema(tmp_path / "dedup.sqlite")  # returns nothing: no connection crosses
    conn = sqlite3.connect(tmp_path / "dedup.sqlite")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"duplicate_clusters", "track_aliases", "tag_provenance"} <= tables
    assert service.scan_status().state in {"idle", "running", "done", "failed"}


def test_scan_status_matches_the_job_payload_and_rejects_extra_fields() -> None:
    # The wire is JSON: a tuple field is a list there.
    assert ScanStatus.model_validate(_IDLE).model_dump(mode="json") == _IDLE
    with pytest.raises(ValidationError):
        ScanStatus.model_validate({**_IDLE, "surprise": 1})


def test_contract_version_and_cluster_bounds() -> None:
    from apps.dedup import find_clusters

    assert contract.CONTRACT_VERSION == "dedup/2"
    assert find_clusters.DEFAULT_MAX_CLUSTER == contract.DEFAULT_MAX_CLUSTER == 8
    assert find_clusters.DEFAULT_DURATION_DELTA_S == contract.DEFAULT_DURATION_DELTA_S == 3.0
