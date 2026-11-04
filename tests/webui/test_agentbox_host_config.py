"""MDT_AGENTBOX_HOST is required; imports succeed without it (#5639 stage 2).

Regression one-liners:
  - if MDT_AGENTBOX_HOST unset or blank does not raise naming the variable then broken
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

import pytest

from apps.webui.port_config import PortConfigError
from apps.webui.run_agentbox import (
    AGENTBOX_HOST_ENV,
    AGENTBOX_SSH_HOSTS_ENV,
    agentbox_host,
    allowed_ssh_hosts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HOUSE_NAME = "agentbox"
OVERRIDE = "hostbox-test"

_PROBE = """
import json, sys
from apps.webui.port_config import PortConfigError
from apps.webui import crate_sync, run_agentbox as r
try:
    host = r.agentbox_host()
    parser = crate_sync.build_parser()
    to_default = parser.get_default("to")
    crate_sync_help = " ".join(parser.format_help().split())
    print(json.dumps({
        "hostname": host,
        "ssh_host": host,
        "allowed": sorted(r.allowed_ssh_hosts()),
        "remote_run": r.remote_run_command(),
        "remote_check": r.remote_check_command(),
        "check_argv": r.agentbox_check_command("box.example.ts.net", "192.0.2.1"),
        "is_agentbox": r.is_agentbox(hostname=host),
        "keeps_logs": str(r.log_home(hostname=host)) == str(r.LOG_DIR),
        "crate_sync_to": to_default,
        "crate_sync_help": crate_sync_help,
    }))
except PortConfigError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
"""


def _uses_in_a_fresh_process(value: str | None) -> tuple[int, str]:
    drop = {AGENTBOX_HOST_ENV, AGENTBOX_SSH_HOSTS_ENV}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if value is not None:
        env[AGENTBOX_HOST_ENV] = value
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def test_agentbox_host_raises_when_unset_or_blank() -> None:
    """[if] MDT_AGENTBOX_HOST unset or blank does not raise [then] fail, [else stop]."""
    for environ in ({}, {AGENTBOX_HOST_ENV: ""}, {AGENTBOX_HOST_ENV: "  "}):
        with pytest.raises(PortConfigError, match=AGENTBOX_HOST_ENV):
            agentbox_host(environ=environ)
        with pytest.raises(PortConfigError, match=AGENTBOX_HOST_ENV):
            allowed_ssh_hosts(environ=environ)


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


def test_every_agentbox_use_follows_the_one_variable_when_set() -> None:
    """[if] any agentbox use reads its name from anywhere else [then] fail, [else stop]."""
    code, raw = _uses_in_a_fresh_process(OVERRIDE)
    assert code == 0, raw
    uses = json.loads(raw.splitlines()[-1])
    expected = OVERRIDE
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


@pytest.mark.parametrize("value", [None, "", "  "], ids=["unset", "blank", "spaces"])
def test_import_succeeds_then_use_raises_without_host(value: str | None) -> None:
    """[if] import fails without host env [then] fail, [else stop]."""
    import_code = (
        "import apps.webui.run_agentbox\nfrom apps.webui import crate_sync\n"
    )
    drop = {AGENTBOX_HOST_ENV, AGENTBOX_SSH_HOSTS_ENV}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if value is not None:
        env[AGENTBOX_HOST_ENV] = value
    proc = subprocess.run(
        [sys.executable, "-c", import_code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    code, out = _uses_in_a_fresh_process(value)
    assert code != 0
    assert AGENTBOX_HOST_ENV in out


def test_a_bad_value_does_not_stop_import() -> None:
    """[if] a bad MDT_AGENTBOX_HOST stops import [then] fail, [else stop]."""
    drop = {AGENTBOX_HOST_ENV, AGENTBOX_SSH_HOSTS_ENV}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env[AGENTBOX_HOST_ENV] = "box;reboot"
    proc = subprocess.run(
        [sys.executable, "-c", "import apps.webui.run_agentbox"], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    with pytest.raises(PortConfigError, match=AGENTBOX_HOST_ENV):
        agentbox_host(environ={AGENTBOX_HOST_ENV: "box;reboot"})
