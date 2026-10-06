"""CLOUDSYNC-35: the dev server refuses to boot on a data dir that would sync, unless opted in.

Regression lines:
  - if a dev server boots on an enabled cloudsync-config.json without MDT_DEV_CLOUDSYNC=on then broken
  - if MDT_CLOUDSYNC_SCHEDULER=1 alone gets a dev server past the guard then broken
  - if a data dir with no config or a disabled one is refused then broken
  - if the opt-in does not let a syncing dev server boot then broken

[if] a dev server would sync a cloned data dir without an opt-in [then] boot fails, [else stop].
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.sync_hub import config as cloudsync_config
from apps.webui.server import dev_cloudsync_guard as guard

pytestmark = pytest.mark.requirement("CLOUDSYNC-35")

HUB = "https://hub.example.ts.net:8870"
REPO = Path(__file__).resolve().parents[2]


def _data_dir(tmp_path: Path, *, enabled: bool | None) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "machine-id").write_text("514404d7662649218bb5b10ec33fc1ac\n", encoding="utf-8")
    if enabled is not None:
        (data_dir / cloudsync_config.CONFIG_FILENAME).write_text(
            json.dumps({"enabled": enabled, "hub_url": HUB, "machine_name": "air-preview"}), encoding="utf-8"
        )
    return data_dir


def test_an_enabled_config_refuses_and_names_the_machine(tmp_path: Path) -> None:
    """[if] the config says enabled and nothing opts in [then] boot raises, naming it, [else stop]."""
    data_dir = _data_dir(tmp_path, enabled=True)
    with pytest.raises(guard.DevCloudSyncRefused) as refused:
        guard.assert_dev_cloudsync_allowed(data_dir, {})
    message = str(refused.value)
    assert "514404d7662649218bb5b10ec33fc1ac" in message and guard.OPT_IN_ENV in message
    assert str(cloudsync_config.config_path(data_dir)) in message


def test_the_scheduler_env_alone_is_refused(tmp_path: Path) -> None:
    """[if] only MDT_CLOUDSYNC_SCHEDULER=1 enables sync [then] boot still raises, [else stop]."""
    with pytest.raises(guard.DevCloudSyncRefused, match="source: env"):
        guard.assert_dev_cloudsync_allowed(_data_dir(tmp_path, enabled=None), {cloudsync_config.SCHEDULER_ENV: "1"})


@pytest.mark.parametrize("enabled", [None, False], ids=["no-config", "disabled"])
def test_a_data_dir_that_will_not_sync_boots(tmp_path: Path, enabled: bool | None) -> None:
    """[if] there is no config or it is disabled [then] the guard passes, [else stop]."""
    guard.assert_dev_cloudsync_allowed(_data_dir(tmp_path, enabled=enabled), {})


def test_the_opt_in_lets_a_syncing_dev_server_boot(tmp_path: Path) -> None:
    """[if] MDT_DEV_CLOUDSYNC=on [then] an enabled config passes the guard, [else stop]."""
    guard.assert_dev_cloudsync_allowed(_data_dir(tmp_path, enabled=True), {guard.OPT_IN_ENV: "on"})


def test_an_unknown_opt_in_value_is_refused(tmp_path: Path) -> None:
    """[if] MDT_DEV_CLOUDSYNC is neither on nor off [then] boot raises, [else stop]."""
    with pytest.raises(guard.DevCloudSyncRefused, match="not a member"):
        guard.assert_dev_cloudsync_allowed(_data_dir(tmp_path, enabled=None), {guard.OPT_IN_ENV: "yes"})


_BOOT = (
    "from apps.webui.server import app as m\n"
    "from apps.webui.server.dev_cloudsync_guard import DevCloudSyncRefused\n"
    "try:\n"
    "    m._build_default_app()\n"
    "except DevCloudSyncRefused:\n"
    "    print('REFUSED')\n"
    "else:\n"
    "    print('BOOTED')\n"
)


def _boot(data_dir: Path, **extra: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MDT_CLOUDSYNC", "MDT_DEV_CLOUDSYNC"))}
    env.update(MDT_DATA_DIR=str(data_dir), MDT_LIBRARY_MODE="local", MUSIC_DJ_BIND_HOST="127.0.0.1", **extra)
    proc = subprocess.run(
        [sys.executable, "-c", _BOOT], cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.strip().splitlines()[-1]


def test_the_dev_server_entry_refuses_a_cloned_syncing_data_dir(tmp_path: Path) -> None:
    """[if] the real dev entry boots on an enabled config [then] it refuses before arming, [else stop]."""
    data_dir = _data_dir(tmp_path, enabled=True)
    assert _boot(data_dir) == "REFUSED", "if the dev server boots a syncing clone silently then broken"
    # Mutation control: the same data dir with the opt-in boots, so REFUSED above is
    # the guard and not some other failure of booting in a subprocess.
    assert _boot(data_dir, MDT_DEV_CLOUDSYNC="on", MDT_CLOUDSYNC_SCHEDULER="0") == "BOOTED"
