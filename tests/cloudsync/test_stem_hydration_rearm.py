"""Acceptance tests for STEM-32 transient stem hydration re-arm (#2815)."""
from __future__ import annotations

import time

import pytest

from apps.cloud import stem_index
from apps.cloud.stem_source import STEM_HYDRATION_NOT_ARMED
from apps.sync_hub import config as sync_config
from tests.cloudsync.conftest import free_port
from tests.cloudsync.stem_rearm_hub_rig import (
    boot_stem_hub,
    build_spoke_app,
    enroll_spoke,
    read_hit_count,
    stem_index_payload,
    stop_stem_hub,
)

pytestmark = pytest.mark.requirement("STEM-32")


@pytest.fixture(autouse=True)
def _fast_rearm_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stem_index, "INDEX_REFRESH_RETRY_INTERVAL_S", 0.2)
    monkeypatch.setenv("MDT_SYNC_CREDENTIAL_MODE", "observe")


def test_transient_unreachable_rearms_and_hydrates_without_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: removing maybe_rearm_stem_hydration must fail this test."""
    stable_id = "rearm-recovery-track"
    enroll_root = tmp_path / "enroll"
    enroll_hub = boot_stem_hub(
        enroll_root,
        index=stem_index_payload(stable_id),
        name="enroll-hub",
    )
    spoke_dir = tmp_path / "spoke"
    try:
        enroll_spoke(spoke_dir, enroll_hub.url)
    finally:
        stop_stem_hub(enroll_hub)

    unreachable_port = free_port()
    unreachable_url = f"http://127.0.0.1:{unreachable_port}"
    sync_config.write_config(
        spoke_dir,
        sync_config.CloudSyncConfig(
            enabled=True,
            hub_url=unreachable_url,
            machine_name="spoke",
        ),
    )
    app, client = build_spoke_app(spoke_dir, monkeypatch)
    assert app.state.stem_hydration_unarmed_kind == "transient"
    assert app.state.stem_hydration_source is None

    response = client.get(f"/api/v1/tracks/{stable_id}/stems")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == STEM_HYDRATION_NOT_ARMED

    recovery_hub = boot_stem_hub(
        tmp_path / "recovery",
        port=unreachable_port,
        index=stem_index_payload(stable_id),
        name="recovery-hub",
    )
    time.sleep(0.25)
    try:
        response = client.get(f"/api/v1/tracks/{stable_id}/stems")
        assert response.status_code == 200
        body = response.json()
        assert body.get("hydrating") is True or body.get("stable_id") == stable_id
        assert read_hit_count(recovery_hub) == 1
        assert app.state.stem_hydration_source is not None
        assert app.state.stem_hydration_unarmed_reason is None
    finally:
        stop_stem_hub(recovery_hub)


def test_structural_401_never_retries_rearm(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: retrying structural reasons must fail this test."""
    stable_id = "structural-auth-track"
    hub_root = tmp_path / "hub"
    hub = boot_stem_hub(
        hub_root,
        index=stem_index_payload(stable_id),
    )
    spoke_dir = tmp_path / "spoke"
    try:
        enroll_spoke(spoke_dir, hub.url)
        (spoke_dir / "sync-credential").write_text(
            "sync_bearer_totally-invalid-token",
            encoding="utf-8",
        )
        app, client = build_spoke_app(spoke_dir, monkeypatch)
        assert app.state.stem_hydration_unarmed_kind == "structural"

        first = client.get(f"/api/v1/tracks/{stable_id}/stems")
        assert first.status_code == 502
        assert first.json()["detail"]["code"] == STEM_HYDRATION_NOT_ARMED
        assert read_hit_count(hub) == 0

        time.sleep(0.25)
        second = client.get(f"/api/v1/tracks/{stable_id}/stems")
        assert second.status_code == 502
        assert second.json()["detail"]["code"] == STEM_HYDRATION_NOT_ARMED
        assert read_hit_count(hub) == 0
    finally:
        stop_stem_hub(hub)


def test_throttle_skips_hub_inside_window(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hub index refresh can exceed the 0.2s recovery-test interval; keep a wide
    # floor so the immediate second miss is throttled from rearm start time.
    monkeypatch.setattr(stem_index, "INDEX_REFRESH_RETRY_INTERVAL_S", 60.0)
    stable_id = "throttle-track"
    hub_root = tmp_path / "hub"
    hub = boot_stem_hub(hub_root)
    spoke_dir = tmp_path / "spoke"
    try:
        enroll_spoke(spoke_dir, hub.url)
        app, client = build_spoke_app(spoke_dir, monkeypatch)
        assert app.state.stem_hydration_unarmed_kind == "transient"
        boot_hits = read_hit_count(hub)

        time.sleep(0.25)
        first = client.get(f"/api/v1/tracks/{stable_id}/stems")
        assert first.status_code == 502
        after_first = read_hit_count(hub)
        assert after_first == boot_hits + 1

        second = client.get(f"/api/v1/tracks/{stable_id}/stems")
        assert second.status_code == 502
        assert read_hit_count(hub) == after_first
    finally:
        stop_stem_hub(hub)
