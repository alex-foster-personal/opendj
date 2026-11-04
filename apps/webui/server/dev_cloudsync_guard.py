"""Refuse to boot the dev server on a data dir that would sync (CLOUDSYNC-35).

``python -m apps.webui.server`` is the dev and proof entry point: previews,
e2e suites and agents' screenshot runs boot it against a data dir that is very
often a CLONE of a real app's data (an APFS ``cp -c`` of the Air's preview
data, Tue 6 Oct 2026). A clone carries the source's ``cloudsync-config.json``
AND its ``machine-id``, so its CloudSync scheduler pushes to the fleet hub as
the real machine: the hub cannot tell the clone's rows from the original's.
Moving the config aside AFTER the first start (what happened that night) is
too late: the scheduler's first round runs 30 s after boot.

So the dev server fails fast at boot when the effective CloudSync config says
enabled (file or ``MDT_CLOUDSYNC_SCHEDULER=1``), unless the operator opts in
with ``MDT_DEV_CLOUDSYNC=on``. The packaged app (``apps.engine_core``) is not
this entry point and is unaffected.

Requirements (mini-PRD):
  ✔︎ a syncing data dir never boots the dev server silently
    [if] cloudsync-config.json says enabled and no opt-in is set [then] boot raises, naming the file
    [if] MDT_CLOUDSYNC_SCHEDULER=1 and no opt-in is set [then] boot raises
    [if] MDT_DEV_CLOUDSYNC=on [then] boot proceeds and syncs as configured
    [if] the config is absent or disabled [then] boot proceeds
    [if] MDT_DEV_CLOUDSYNC is neither on nor off [then] boot raises
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from apps.sync_hub import config as cloudsync_config

OPT_IN_ENV: str = "MDT_DEV_CLOUDSYNC"
OPT_IN_VALUES: tuple[str, ...] = ("on", "off")
MACHINE_ID_FILENAME: str = "machine-id"


class DevCloudSyncRefused(RuntimeError):
    """The dev server would sync this data dir to the hub without an opt-in."""


def assert_dev_cloudsync_allowed(data_dir: Path, env: Mapping[str, str]) -> None:
    raw = env.get(OPT_IN_ENV, "off").strip().lower()
    if raw not in OPT_IN_VALUES:
        raise DevCloudSyncRefused(f"{OPT_IN_ENV}={raw!r} is not a member of {OPT_IN_VALUES}")
    if raw == "on":
        return
    effective = cloudsync_config.resolve_config(data_dir, env=env)
    if not effective.enabled:
        return
    machine_id_path = Path(data_dir) / MACHINE_ID_FILENAME
    machine_id = machine_id_path.read_text(encoding="utf-8").strip() if machine_id_path.is_file() else "unknown"
    raise DevCloudSyncRefused(
        f"CloudSync is enabled for {data_dir} (source: {effective.enabled_source}, "
        f"{cloudsync_config.config_path(data_dir)}; hub {effective.hub_url}), and this dev server "
        f"would push to the hub as machine {machine_id}. A cloned data dir shares that id with the "
        f"machine it came from, so its rows would be indistinguishable from the real ones. Move "
        f"cloudsync-config.json aside (and unset MDT_CLOUDSYNC_SCHEDULER) BEFORE booting, or set "
        f"{OPT_IN_ENV}=on if this dev server really should sync."
    )


__all__ = ["OPT_IN_ENV", "DevCloudSyncRefused", "assert_dev_cloudsync_allowed"]
