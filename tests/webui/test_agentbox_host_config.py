"""The agentbox host name comes from MDT_AGENTBOX_HOST, default unchanged (issue #5639, batch 2).

Stage 1 of moving hardcoded host aliases to documented variables: set, the
variable names the agentbox machine (hostname, bare ssh alias, short MagicDNS
name); unset or blank keeps ``agentbox``, so no machine changes behaviour before
it sets the variable (ADR-0317 disposition on #5639).

Regression one-liners:
  - if MDT_AGENTBOX_HOST unset or blank is not agentbox then broken
  - if a set MDT_AGENTBOX_HOST does not rename every agentbox use alike then broken
  - if a value that is not one DNS label reaches ssh or the remote command then broken
  - if crate_sync --help names a box other than the configured one then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.webui.port_config import PortConfigError
from apps.webui.run_agentbox import (
    AGENTBOX_HOST_ENV,
    AGENTBOX_SSH_HOSTS_ENV,
    DEFAULT_AGENTBOX_HOST,
    agentbox_host,
    allowed_ssh_hosts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HOUSE_NAME = "agentbox"
OVERRIDE = "hostbox-test"

#: Every agentbox use, read in a fresh process so module constants see the env.
_PROBE = """
import json
from apps.webui import crate_sync, run_agentbox as r
parser = crate_sync.build_parser()
to_default = parser.get_default("to")
crate_sync_help = " ".join(parser.format_help().split())
print(json.dumps({
    "hostname": r.AGENTBOX_HOSTNAME,
    "ssh_host": r.SSH_HOST,
    "allowed": sorted(r.allowed_ssh_hosts()),
    "remote_run": r.remote_run_command(),
    "remote_check": r.remote_check_command(),
    "check_argv": r.agentbox_check_command("box.example.ts.net", "192.0.2.1"),
    "is_agentbox": r.is_agentbox(hostname=r.AGENTBOX_HOSTNAME),
    "keeps_logs": str(r.log_home(hostname=r.AGENTBOX_HOSTNAME)) == str(r.LOG_DIR),
    "crate_sync_to": to_default,
    "crate_sync_help": crate_sync_help,
}))
"""


def _uses_in_a_fresh_process(value: str | None) -> dict[str, Any]:
    drop = {AGENTBOX_HOST_ENV, AGENTBOX_SSH_HOSTS_ENV}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if value is not None:
        env[AGENTBOX_HOST_ENV] = value
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_agentbox_host_defaults_to_agentbox_when_unset_or_blank() -> None:
    """[if] MDT_AGENTBOX_HOST unset or blank is not agentbox [then] fail, [else stop]."""
    assert DEFAULT_AGENTBOX_HOST == HOUSE_NAME
    assert agentbox_host(environ={}) == HOUSE_NAME
    assert agentbox_host(environ={AGENTBOX_HOST_ENV: ""}) == HOUSE_NAME
    assert agentbox_host(environ={AGENTBOX_HOST_ENV: "  "}) == HOUSE_NAME
    assert allowed_ssh_hosts(environ={}) == frozenset({HOUSE_NAME})


def test_agentbox_host_override_names_the_box_and_its_ssh_allowlist() -> None:
    """[if] a set MDT_AGENTBOX_HOST does not name the box and its alias [then] fail, [else stop]."""
    env = {AGENTBOX_HOST_ENV: f" {OVERRIDE} "}
    assert agentbox_host(environ=env) == OVERRIDE
    assert allowed_ssh_hosts(environ=env) == frozenset({OVERRIDE})
    env[AGENTBOX_SSH_HOSTS_ENV] = "box.example.ts.net"
    assert allowed_ssh_hosts(environ=env) == frozenset({OVERRIDE, "box.example.ts.net"})


@pytest.mark.parametrize(
    "bad", ["two words", "box;reboot", "box.example", "-box", "box-", "x" * 64, "box$(id)"]
)
def test_a_value_that_is_not_one_dns_label_is_refused_naming_the_variable(bad: str) -> None:
    """[if] a non-label MDT_AGENTBOX_HOST is accepted [then] fail, [else stop]."""
    with pytest.raises(PortConfigError, match=AGENTBOX_HOST_ENV):
        agentbox_host(environ={AGENTBOX_HOST_ENV: bad})


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, HOUSE_NAME), ("", HOUSE_NAME), (OVERRIDE, OVERRIDE)],
    ids=["unset", "blank", "override"],
)
def test_every_agentbox_use_follows_the_one_variable(value: str | None, expected: str) -> None:
    """[if] any agentbox use reads its name from anywhere else [then] fail, [else stop]."""
    uses = _uses_in_a_fresh_process(value)
    assert uses["hostname"] == uses["ssh_host"] == uses["crate_sync_to"] == expected
    assert uses["allowed"] == [expected]
    for command in (uses["remote_run"], uses["remote_check"]):
        assert command.startswith(f'test "$(hostname)" = {expected} && '), command
    argv = uses["check_argv"]
    assert argv[argv.index("--short-host") + 1] == expected
    assert uses["is_agentbox"] is True
    assert uses["keeps_logs"] is True
    crate_sync_help = uses["crate_sync_help"]
    for phrase in (f"pushes to {expected}", f"run on {expected};", f"(owner Mac -> {expected})"):
        assert phrase in crate_sync_help, phrase


def test_a_bad_value_stops_the_module_from_loading() -> None:
    """[if] a bad MDT_AGENTBOX_HOST lets run_agentbox load [then] fail, [else stop]."""
    env = {**os.environ, AGENTBOX_HOST_ENV: "box;reboot"}
    proc = subprocess.run(
        [sys.executable, "-c", "import apps.webui.run_agentbox"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode != 0
    assert AGENTBOX_HOST_ENV in proc.stderr
