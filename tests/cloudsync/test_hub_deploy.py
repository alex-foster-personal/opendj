"""The hub deploy artifacts launch a real hub, and the probe can tell.

Boots the EXACT argv every launcher runs (:func:`hub_deploy.hub_serve_argv`
plus :data:`hub_deploy.HUB_ENV`, after the same ``init`` the start script
runs) as a real subprocess on a free loopback port. Two hubs boot side by
side: one with ``MDT_IS_HUB=1`` and one with ``MDT_IS_HUB=0`` over an
identically initialized data dir, so the probe's ``is_hub`` is shown to come
from the SERVING process and able to say no ((private path)).

The rendered units are checked against the same argv, so a unit that starts
something other than what this file booted fails here.

Sibling modules hold the tests; this file is the public import surface.

Split across ``test_hub_deploy_{boot,render,refusals,allowed_hosts,port_race}.py``
and ``hub_deploy_test_support.py`` (issue #10963, parent #1390).

[if] hub deploy launches a hub [then] the probes is_hub matches hub_serve_argv, [else stop].
"""

from __future__ import annotations

pytest_plugins = ["tests.cloudsync.hub_deploy_test_support"]

from tests.cloudsync.hub_deploy_test_support import (  # noqa: F401
    ALLOWED_HOST,
    AMBIENT_ENV_TO_DROP,
    BOOT_GUARD_S,
    BootedHub,
    GOLDEN_FIXTURES,
    GOLDEN_REPO,
    GOLDEN_UV,
    PROBE_HOST,
    REPO_ROOT,
    _answers_health,
    _boot,
    _free_loopback_port,
    _golden_inputs,
    _hello,
    _hub_env,
    _init_data_dir,
    _init_spoke_data_dir,
    _inputs,
    _logged_boot_id,
    _lost_port_race,
    _machine_name_for_id,
    _machine_names,
    _start_hub,
    _stop_hub,
    _systemd_argv,
    _systemd_key,
    _systemd_user_manager_unavailable,
    _uv,
    _wait_for_hub,
    booted_hubs,
)

__all__ = [
    "ALLOWED_HOST",
    "AMBIENT_ENV_TO_DROP",
    "BOOT_GUARD_S",
    "BootedHub",
    "GOLDEN_FIXTURES",
    "GOLDEN_REPO",
    "GOLDEN_UV",
    "PROBE_HOST",
    "REPO_ROOT",
    "_answers_health",
    "_boot",
    "_free_loopback_port",
    "_golden_inputs",
    "_hello",
    "_hub_env",
    "_init_data_dir",
    "_init_spoke_data_dir",
    "_inputs",
    "_logged_boot_id",
    "_lost_port_race",
    "_machine_name_for_id",
    "_machine_names",
    "_start_hub",
    "_stop_hub",
    "_systemd_argv",
    "_systemd_key",
    "_systemd_user_manager_unavailable",
    "_uv",
    "_wait_for_hub",
    "booted_hubs",
]
