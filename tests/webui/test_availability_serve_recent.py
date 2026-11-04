"""PERF-RB-06: row hydration serves a recent known answer instead of pending.

Regression one-liners:
  - if a checked row's index answer is 2 min old then the listing serves it, not pending
  - if serving recent answers is switched off then the same rows read pending (mutation control)
  - if the index answer is older than the window then the row reads pending
  - if the track was never checked here then a copied index answer is still not trusted
  - if this process stat a path 2 min ago then that answer is served past the TTL
  - if an index row's path can be remapped then the row names no measured file
  - if a cached answer is served then path resolution ran for it (no I/O allowed)
  - if the path map moves after a stat then an L1 answer from the old map is still served
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg import availability, path_index
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.app_fixtures import _stub_rb_vendor

from .conftest import TEST_HOST_BASE_URL
from .test_rb_availability_budget import (
    _configure_data_dir,
    _mark_availability,
    _seed_library,
    _stat_spy,
)

TRACKS = 40
BUDGET = availability.PROBE_BUDGET_ROW_HYDRATION
PENDING = "AVAILABILITY_PENDING"


@pytest.fixture
def client_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path, Path]]:
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    _stub_rb_vendor(monkeypatch)
    monkeypatch.setattr(
        "apps.webui.server.routes.playlists.rb_vendor.playlist_order_index", dict
    )
    app = create_app(
        backend=SqliteBackend(state_db_path), bind_host="127.0.0.1",
        hostname="test-host", state_db_path=str(state_db_path), mount_frontend=False,
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as client:
        yield client, data_dir, state_db_path


_INDEX_MTIME = 1_700_000_000_000_000_000


def _seed_index_aged(state_db_path: Path, data_dir: Path, paths: list[str], age_s: float) -> None:
    namespace = path_index.resolver_namespace(data_dir)
    stamp = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(timespec="microseconds")
    conn = state_db.open_rw(state_db_path)
    try:
        path_index.upsert_rows(
            conn, namespace, [(path, 256, _INDEX_MTIME) for path in paths]
        )
        conn.execute("UPDATE path_availability SET checked_at = ?", (stamp,))
        conn.commit()
    finally:
        conn.close()


def _statuses(client: TestClient) -> list[str]:
    response = client.get("/api/v1/playlists/pl-big")
    assert response.status_code == 200, response.text
    return [t["file_availability"] for t in response.json()["tracks"]]


def _checked_library(state_db_path: Path, data_dir: Path, age_s: float) -> None:
    sids, paths = _seed_library(state_db_path, track_count=TRACKS)
    _mark_availability(state_db_path, sids, paths)
    _seed_index_aged(state_db_path, data_dir, paths, age_s)


@pytest.mark.requirement("PERF-RB-06")
def test_recent_index_answer_is_served_not_pending(client_env, monkeypatch) -> None:
    """[if] a checked row's answer is 2 min old [then] it is served, [else stop]."""
    client, data_dir, state_db_path = client_env
    _checked_library(state_db_path, data_dir, age_s=120)
    calls = _stat_spy(monkeypatch)

    statuses = _statuses(client)

    assert calls["n"] == BUDGET
    assert statuses.count(PENDING) == 0
    assert set(statuses) == {"present"}


@pytest.mark.requirement("PERF-RB-06")
def test_without_serving_recent_the_same_rows_read_pending(client_env, monkeypatch) -> None:
    """[if] recent answers are not served [then] rows read pending, [else stop]."""
    client, data_dir, state_db_path = client_env
    _checked_library(state_db_path, data_dir, age_s=120)
    _stat_spy(monkeypatch)
    monkeypatch.setattr(availability.ProbeBudget, "serves_recent", property(lambda _self: False))

    assert _statuses(client).count(PENDING) == TRACKS - BUDGET


@pytest.mark.requirement("PERF-RB-06")
def test_answer_older_than_the_window_reads_pending(client_env, monkeypatch) -> None:
    """[if] the index answer is past the window [then] rows read pending, [else stop]."""
    client, data_dir, state_db_path = client_env
    _checked_library(state_db_path, data_dir, age_s=availability.SERVE_RECENT_MAX_S + 60)
    _stat_spy(monkeypatch)

    assert _statuses(client).count(PENDING) == TRACKS - BUDGET


@pytest.mark.requirement("PERF-RB-06")
def test_unchecked_track_never_trusts_a_copied_index(client_env, monkeypatch) -> None:
    """[if] tracks were never checked here [then] a recent index stays unused, [else stop]."""
    client, data_dir, state_db_path = client_env
    _sids, paths = _seed_library(state_db_path, track_count=TRACKS)
    _seed_index_aged(state_db_path, data_dir, paths, age_s=120)
    _stat_spy(monkeypatch)

    assert _statuses(client).count(PENDING) == TRACKS - BUDGET


@pytest.mark.requirement("PERF-RB-06")
def test_own_expired_stat_is_served_past_the_ttl(tmp_path: Path, monkeypatch) -> None:
    """[if] this process stat a path 2 min ago [then] that answer is served, [else stop]."""
    _configure_data_dir(monkeypatch, tmp_path / "data")
    monkeypatch.setattr("apps.webui.server.path_availability_refresh.schedule", lambda _p: None)
    path = str(tmp_path / "a.mp3")
    rb_config._FILE_EXISTS_CACHE[path] = (
        time.monotonic() - 120,
        512,
        path,
        path_index.resolver_namespace(rb_config.DATA_DIR),
        512,
    )
    budget = availability.ProbeBudget(availability.AvailabilityProbeMode.ROW_HYDRATION)
    budget.remaining = 0

    probed = availability.bulk_probe_paths([path], budget=budget, trust_index=False)

    assert probed[path] == availability.PathProbeResult(
        "present", 512, resolved_path=path, st_mtime_ns=512
    )


def _write_path_map(path_map: Path, target: Path) -> None:
    path_map.write_text(
        json.dumps({"entries": [{"from": "/remote/library", "to": str(target)}]}),
        encoding="utf-8",
    )


@pytest.mark.requirement("QLTSCAN-02")
def test_index_answers_name_a_file_only_when_no_rule_can_move_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a path map can move an index row's path [then] the row names no measured file, [else stop]."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    path_map = tmp_path / "path-map.json"
    _write_path_map(path_map, tmp_path / "library")
    monkeypatch.setenv("MDT_PATH_MAP", str(path_map))
    plain, mapped = str(tmp_path / "plain.flac"), "/remote/library/mapped.flac"
    _seed_index_aged(state_db_path, data_dir, [plain, mapped], age_s=0)

    probed = availability.bulk_probe_paths([plain, mapped])

    assert probed[plain] == availability.PathProbeResult(
        "present", 256, resolved_path=plain, st_mtime_ns=_INDEX_MTIME
    )
    assert probed[mapped] == availability.PathProbeResult(
        "present", 256, resolved_path=None, st_mtime_ns=_INDEX_MTIME
    )


@pytest.mark.requirement("QLTSCAN-02")
def test_cached_answers_resolve_no_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] L1 and index hits are served [then] no path is resolved, so no budget is bypassed, [else stop]."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    in_l1, in_index = str(tmp_path / "l1.flac"), str(tmp_path / "index.flac")
    rb_config._FILE_EXISTS_CACHE[in_l1] = (
        time.monotonic(),
        512,
        in_l1,
        path_index.resolver_namespace(data_dir),
        _INDEX_MTIME,
    )
    _seed_index_aged(state_db_path, data_dir, [in_index], age_s=0)
    resolved: list[str] = []
    real_resolve = availability.resolve_asset_path

    def counting_resolve(path: str) -> object:
        resolved.append(path)
        return real_resolve(path)

    monkeypatch.setattr(availability, "resolve_asset_path", counting_resolve)

    probed = availability.bulk_probe_paths(
        [in_l1, in_index], probe_mode=availability.AvailabilityProbeMode.TREE_SUMMARY
    )

    assert {path: result.status for path, result in probed.items()} == {
        in_l1: "present",
        in_index: "present",
    }
    assert resolved == []


@pytest.mark.requirement("QLTSCAN-02")
def test_a_remap_invalidates_cached_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the path map moves after a stat [then] the old file's cached answer is not served, [else stop]."""
    _configure_data_dir(monkeypatch, tmp_path / "data")
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    for root in (old_root, new_root):
        root.mkdir()
        (root / "song.flac").write_bytes(b"same size")
    path_map = tmp_path / "path-map.json"
    _write_path_map(path_map, old_root)
    monkeypatch.setenv("MDT_PATH_MAP", str(path_map))
    path = "/remote/library/song.flac"

    first = availability.bulk_probe_paths([path])[path]
    repeated = availability.bulk_probe_paths([path])[path]
    _write_path_map(path_map, new_root)
    remapped = availability.bulk_probe_paths([path])[path]

    assert first == repeated, "control: the same map serves the same answer"
    assert first.resolved_path is not None and first.resolved_path.endswith("old/song.flac")
    assert remapped.resolved_path is not None
    assert remapped.resolved_path.endswith("new/song.flac")
