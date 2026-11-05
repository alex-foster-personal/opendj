"""[if] CloudSync is on with no cells [then] portable defaults are written, [else stop]."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.cloud.policy import CFG as FILE_POLICY
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import config as sync_config
from apps.sync_hub import policy_bootstrap
from apps.sync_hub.client_transport_ops import state_db_path
from apps.sync_hub.policy_store import POLICIES_TABLE

from .policy_fixtures import count, http_client, local_id, make_fleet_dir

pytestmark = pytest.mark.requirement("CLOUDSYNC-4918")


@pytest.fixture(autouse=True)
def _clean_cloudsync_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MDT_IS_HUB",
        sync_config.SCHEDULER_ENV,
        sync_config.ENDPOINT_ENV,
        policy_bootstrap.SEED_CLASS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


LIVE_POLICY_ROWS = "SELECT COUNT(*) FROM sync_policies WHERE deleted_at IS NULL"
CHANGELOG_ROWS = f"SELECT COUNT(*) FROM local_changelog WHERE table_name = '{POLICIES_TABLE}'"
_HUB = "https://hub.example-tailnet.ts.net:8871"


def _enable_cloudsync(data_dir: Path) -> None:
    sync_config.write_config(
        data_dir,
        sync_config.CloudSyncConfig(enabled=True, hub_url=_HUB, machine_name=None),
    )


def _expected_portable() -> dict[str, tuple[str, int | None]]:
    return {
        kind: (
            artifact.default_mode_by_machine_class["portable"],
            artifact.cache_budget_mb if artifact.default_mode_by_machine_class["portable"] == "cached" else None,
        )
        for kind, artifact in FILE_POLICY.artifacts.items()
    }


def _live_cells(data_dir: Path) -> dict[str, tuple[str, int | None]]:
    conn = state_db.open_ro(state_db_path(data_dir))
    try:
        rows = conn.execute(
            "SELECT asset_kind, mode, cache_budget_mb FROM sync_policies WHERE machine_id = ? AND deleted_at IS NULL",
            (local_id(data_dir),),
        ).fetchall()
    finally:
        conn.close()
    return {str(kind): (str(mode), budget) for kind, mode, budget in rows}


def test_enabled_spoke_without_cells_seeds_portable_defaults(tmp_path: Path) -> None:
    """[if] CloudSync is on and this machine has no cells [then] portable defaults land."""
    data_dir = make_fleet_dir(tmp_path, with_hub=True)
    assert count(data_dir, LIVE_POLICY_ROWS) == 6
    _enable_cloudsync(data_dir)

    result = policy_bootstrap.ensure_enabled_spoke_policy(data_dir, env={})

    assert result.status == "seeded"
    assert result.written is True
    assert result.machine_class == "portable"
    assert _live_cells(data_dir) == _expected_portable()
    assert count(data_dir, LIVE_POLICY_ROWS) == 12


def test_second_bootstrap_is_a_noop(tmp_path: Path) -> None:
    """[if] a live cell already exists [then] a second seed writes nothing."""
    data_dir = make_fleet_dir(tmp_path, with_hub=True)
    _enable_cloudsync(data_dir)
    first = policy_bootstrap.ensure_enabled_spoke_policy(data_dir, env={})
    logged = count(data_dir, CHANGELOG_ROWS)

    again = policy_bootstrap.ensure_enabled_spoke_policy(data_dir, env={})

    assert first.status == "seeded"
    assert again.status == "already-seeded"
    assert again.written is False
    assert count(data_dir, CHANGELOG_ROWS) == logged
    assert _live_cells(data_dir) == _expected_portable()


def test_cloudsync_off_writes_no_policy_rows(tmp_path: Path) -> None:
    """[if] CloudSync is off [then] automatic seed does not invent fleet rows."""
    data_dir = make_fleet_dir(tmp_path, with_hub=False)
    before = count(data_dir, LIVE_POLICY_ROWS)

    result = policy_bootstrap.ensure_enabled_spoke_policy(data_dir, env={})

    assert result.status == "skipped-off"
    assert result.written is False
    assert count(data_dir, LIVE_POLICY_ROWS) == before
    assert before == 0


def test_hub_process_never_seeds(tmp_path: Path) -> None:
    """[if] this process is the hub [then] bootstrap skips even with a spoke config."""
    data_dir = make_fleet_dir(tmp_path, with_hub=True)
    _enable_cloudsync(data_dir)

    result = policy_bootstrap.ensure_enabled_spoke_policy(data_dir, env={"MDT_IS_HUB": "1"})

    assert result.status == "skipped-hub"
    assert result.written is False
    assert _live_cells(data_dir) == {}


def test_list_machines_seeds_when_cloudsync_is_configured(tmp_path: Path) -> None:
    """[if] GET /cloudsync/machines runs on a configured spoke with no cells [then] cells exist."""
    data_dir = make_fleet_dir(tmp_path, with_hub=True)
    _enable_cloudsync(data_dir)
    with http_client(data_dir) as http:
        response = http.get("/api/v1/cloudsync/machines")
    assert response.status_code == 200, response.text
    assert _live_cells(data_dir) == _expected_portable()


def test_list_machines_does_not_seed_when_cloudsync_is_off(tmp_path: Path) -> None:
    """[if] GET /cloudsync/machines runs with CloudSync off [then] no self cells appear."""
    data_dir = make_fleet_dir(tmp_path, with_hub=False)
    with http_client(data_dir) as http:
        response = http.get("/api/v1/cloudsync/machines")
    assert response.status_code == 200, response.text
    assert _live_cells(data_dir) == {}


def test_unknown_seed_class_env_is_refused() -> None:
    """[if] MDT_CLOUDSYNC_SEED_CLASS is not a known class [then] refuse, [else stop]."""
    with pytest.raises(sync_config.CloudSyncConfigError):
        policy_bootstrap.resolve_seed_class({policy_bootstrap.SEED_CLASS_ENV: "laptop"})


def test_author_machine_is_the_seed_target(tmp_path: Path) -> None:
    """[if] ensure_self_class_seeded runs [then] cells belong to this machine only."""
    data_dir = make_fleet_dir(tmp_path, with_hub=True)
    conn = state_db.open_rw(state_db_path(data_dir))
    try:
        me = sync_stamp.local_machine_id(conn)
        result = policy_bootstrap.ensure_self_class_seeded(conn)
        assert result.status == "seeded"
        kinds = {
            row[0]
            for row in conn.execute(
                "SELECT asset_kind FROM sync_policies WHERE machine_id = ? AND deleted_at IS NULL",
                (me,),
            )
        }
    finally:
        conn.close()
    assert kinds == set(FILE_POLICY.artifacts)


def test_adopted_machine_identity_is_the_seed_target(tmp_path: Path) -> None:
    """[if] the machine name already belongs to another id (restored data dir) [then] seed
    that adopted id instead of raising MACHINE_NOT_FOUND on engine boot, [else stop]."""
    data_dir = make_fleet_dir(tmp_path, with_hub=True)
    file_id = local_id(data_dir)
    conn = state_db.open_rw(state_db_path(data_dir))
    try:
        name = conn.execute("SELECT name FROM machines WHERE machine_id = ?", (file_id,)).fetchone()[0]
        conn.execute(
            "UPDATE machines SET machine_id = ? WHERE machine_id = ?",
            ("restored-identity", file_id),
        )
    finally:
        conn.close()
    sync_config.write_config(
        data_dir,
        sync_config.CloudSyncConfig(enabled=True, hub_url=_HUB, machine_name=str(name)),
    )

    result = policy_bootstrap.ensure_enabled_spoke_policy(data_dir, env={})

    assert result.status == "seeded", result
    rows = count(
        data_dir,
        "SELECT COUNT(*) FROM sync_policies WHERE machine_id = 'restored-identity' AND deleted_at IS NULL",
    )
    assert rows == len(FILE_POLICY.artifacts)
