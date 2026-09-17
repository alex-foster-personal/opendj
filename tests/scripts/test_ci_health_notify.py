"""Tests for ``scripts/ci_health_notify.sh``, the watchdog's alert dispatcher.

These pin the primary-email/secondary-notification delivery contract. The exit
code follows primary delivery; secondary failures never imply email success.

The natural-language acceptance tests, one assertion each:

    if a primary-channel failure does not exit nonzero, the watchdog is broken
    if a secondary-channel failure alone exits nonzero, the watchdog is broken
    if the primary channel is not attempted first, the watchdog is broken
    if a failed primary aborts the secondaries, the watchdog is broken

Two layers, because stubbing everything would let the real send path rot unseen:

* the dispatcher tests stub all three channel functions, so only the
  priority logic decides the verdict;
* the ``_alert_email`` tests run the REAL function against a fake ``gws``
  executable, covering its guard branches, the arguments it builds, and
  whether ``USER`` actually reaches the child process, which is the
  difference between a working mailer and one that deletes its own
  credentials.

Authenticated end-to-end delivery requires separately retained operator evidence.
These tests perform no real network delivery.

No network, no mail, no notifications: the script is sourced with its ``_main``
call stripped so nothing self-executes.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

NOTIFY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_health_notify.sh"

# Stub bodies for the three channels. 0 means delivered, 1 means the channel failed.
_HARNESS = """
set -uo pipefail
source "{loadable}"

_alert_email() {{ ORDER="${{ORDER}}email "; return {email}; }}
_alert_macos_notification() {{ ORDER="${{ORDER}}macos "; return {macos}; }}
_alert_pushcut() {{ ORDER="${{ORDER}}pushcut "; return {pushcut}; }}

ORDER=""
_fire_alert_channels title subject short body >/dev/null 2>&1
printf 'exit=%s order=%s' "$?" "${{ORDER}}"
"""


def _dispatch(tmp_path: Path, *, email: int, macos: int, pushcut: int) -> tuple[int, str]:
    """Run _fire_alert_channels with stubbed channels; return (exit code, call order)."""
    source = NOTIFY_SCRIPT.read_text()
    assert '_main "$@"' in source, "notifier no longer ends in a _main call, harness is stale"
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))

    script = tmp_path / "harness.sh"
    script.write_text(_HARNESS.format(loadable=loadable, email=email, macos=macos, pushcut=pushcut))
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert result.stdout.startswith("exit="), f"harness produced no verdict: {result.stderr}"
    verdict, order = result.stdout.split(" order=", 1)
    return int(verdict.removeprefix("exit=")), order


def test_all_channels_delivering_exits_zero(tmp_path: Path) -> None:
    code, _ = _dispatch(tmp_path, email=0, macos=0, pushcut=0)
    assert code == 0


def test_primary_email_is_attempted_before_the_secondaries(tmp_path: Path) -> None:
    _, order = _dispatch(tmp_path, email=0, macos=0, pushcut=0)
    assert order == "email macos pushcut "


def test_primary_failure_exits_nonzero_even_when_secondaries_deliver(tmp_path: Path) -> None:
    """The whole point of the change: a delivered Pushcut must not mask a dead mailer."""
    code, _ = _dispatch(tmp_path, email=1, macos=0, pushcut=0)
    assert code != 0


@pytest.mark.parametrize(
    ("macos", "pushcut"),
    [(1, 0), (0, 1), (1, 1)],
    ids=["macos-down", "pushcut-down", "both-secondaries-down"],
)
def test_secondary_failure_alone_never_fails_the_run(
    tmp_path: Path, macos: int, pushcut: int
) -> None:
    code, _ = _dispatch(tmp_path, email=0, macos=macos, pushcut=pushcut)
    assert code == 0


def test_every_channel_failing_exits_nonzero(tmp_path: Path) -> None:
    code, _ = _dispatch(tmp_path, email=1, macos=1, pushcut=1)
    assert code != 0


def test_failed_primary_does_not_abort_the_secondaries(tmp_path: Path) -> None:
    """Secondaries are free, so a dead mailer must not also cost us the cheap channels."""
    _, order = _dispatch(tmp_path, email=1, macos=0, pushcut=0)
    assert order == "email macos pushcut "


_EMAIL_HARNESS = """
set -uo pipefail
source "{loadable}"
GWS_BIN="{fake_gws}"
_alert_email "a subject" "a body"
printf 'exit=%s' "$?"
"""

_FAKE_GWS = """#!/bin/bash
{{ printf 'USER=%s\\n' "${{USER-UNSET}}"; printf 'ARGS=%s\\n' "$*"; }} > "{record}"
exit {code}
"""


#: The recipient the harness configures. A synthetic mailbox on purpose: the
#: tracked tree must not name anybody's real address (#1540), and a test that
#: pinned the old hard-coded one would re-introduce it.
ALERT_EMAIL = "mdt-watchdog@example.invalid"


def _run_real_alert_email(
    tmp_path: Path, *, gws_exit: int, alert_email: str | None = ALERT_EMAIL
) -> tuple[int, str, str]:
    """Drive the REAL _alert_email against a fake gws binary.

    Only the gws executable is faked, so the function's own guard logic, argument
    construction and environment propagation are the code under test. ``alert_email
    = None`` leaves MDT_ALERT_EMAIL out of the child environment entirely, which is
    the unconfigured shape.
    """
    source = NOTIFY_SCRIPT.read_text()
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))

    record = tmp_path / "gws_invocation.txt"
    fake_gws = tmp_path / "gws"
    fake_gws.write_text(_FAKE_GWS.format(record=record, code=gws_exit))
    fake_gws.chmod(0o755)

    script = tmp_path / "email_harness.sh"
    script.write_text(_EMAIL_HARNESS.format(loadable=loadable, fake_gws=fake_gws))
    child_env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    if alert_email is not None:
        child_env["MDT_ALERT_EMAIL"] = alert_email
    result = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
    )
    code = int(result.stdout.rsplit("exit=", 1)[1])
    return code, result.stdout, record.read_text() if record.exists() else ""


def test_alert_email_exports_user_into_the_gws_child_process(tmp_path: Path) -> None:
    """The USER pin is worthless unless it actually reaches the gws process.

    Run with a deliberately bare env (no USER), exactly the shape launchd supplies.
    """
    _, _, invocation = _run_real_alert_email(tmp_path, gws_exit=0)
    assert "USER=UNSET" not in invocation, "gws was invoked without USER, credentials will be wiped"
    assert invocation.startswith("USER=")


def test_alert_email_sends_to_the_configured_alert_address_via_gmail_send(
    tmp_path: Path,
) -> None:
    """The recipient is CONFIGURED, and it is the configured one that is mailed."""
    _, _, invocation = _run_real_alert_email(tmp_path, gws_exit=0)
    assert "gmail +send" in invocation
    assert f"--to {ALERT_EMAIL}" in invocation


def test_alert_email_fails_loudly_when_no_alert_address_is_configured(
    tmp_path: Path,
) -> None:
    """An unset MDT_ALERT_EMAIL is a primary-channel failure, never a silent skip.

    If a default address ever creeps back in, this test is the one that goes red:
    it asserts both that the run fails AND that gws was never invoked, so a
    fallback recipient cannot satisfy it.
    """
    code, output, invocation = _run_real_alert_email(
        tmp_path, gws_exit=0, alert_email=None
    )
    assert code != 0
    assert "[ERROR] channel email (PRIMARY)" in output
    assert "MDT_ALERT_EMAIL" in output
    assert invocation == "", "gws ran with no configured recipient"


def test_required_config_precondition_reports_an_unset_alert_address(tmp_path: Path) -> None:
    """The precondition is checked BEFORE the check runs, on a bare environment."""
    source = NOTIFY_SCRIPT.read_text()
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))
    script = tmp_path / "precondition_harness.sh"
    script.write_text(
        f'set -uo pipefail\nsource "{loadable}"\n'
        "_require_alert_email\n"
        'printf "exit=%s" "$?"\n'
    )
    result = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.stdout.endswith("exit=1")
    assert "[ERROR] precondition: MDT_ALERT_EMAIL is unset" in result.stdout
    assert "[OK]" not in result.stdout


def test_alert_email_reports_ok_when_gws_succeeds(tmp_path: Path) -> None:
    code, output, _ = _run_real_alert_email(tmp_path, gws_exit=0)
    assert code == 0
    assert "[OK] channel email (PRIMARY)" in output


def test_alert_email_reports_error_when_gws_exits_nonzero(tmp_path: Path) -> None:
    """A failing mailer must be an [ERROR], never a [WARN], or it stops paging."""
    code, output, _ = _run_real_alert_email(tmp_path, gws_exit=1)
    assert code != 0
    assert "[ERROR] channel email (PRIMARY)" in output
    assert "[WARN]" not in output


def test_alert_email_reports_error_when_gws_is_absent(tmp_path: Path) -> None:
    """The honest [ERROR] for a machine with no gws installed at all."""
    source = NOTIFY_SCRIPT.read_text()
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))
    script = tmp_path / "absent_harness.sh"
    script.write_text(
        # MDT_ALERT_EMAIL is set BEFORE the source: the script resolves its config
        # block once, at source time, exactly as it does in production.
        f'set -uo pipefail\nMDT_ALERT_EMAIL="{ALERT_EMAIL}"\nsource "{loadable}"\nGWS_BIN=""\n'
        '_alert_email "s" "b"\nprintf \'exit=%s\' "$?"\n'
    )
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert result.stdout.rsplit("exit=", 1)[1] != "0"
    assert "[ERROR] channel email (PRIMARY): gws not installed" in result.stdout
    assert "brew install googleworkspace-cli" in result.stdout


def test_notifier_resolves_gws_by_absolute_path_first() -> None:
    """A bare PATH lookup made the primary channel hostage to launchd's environment."""
    source = NOTIFY_SCRIPT.read_text()
    assert 'GWS_BIN_DEFAULT="/opt/homebrew/bin/gws"' in source
    assert 'GWS_BIN="$(command -v gws || true)"' not in source


def test_notifier_pins_user_so_gws_cannot_delete_its_own_credentials() -> None:
    """Without USER, gws misses its keychain entry and deletes credentials.enc.

    Missing USER may prevent the configured keychain lookup. Export it before
    invoking the mailer in a bare scheduled-job environment.
    """
    source = NOTIFY_SCRIPT.read_text()
    assert ': "${USER:=$(id -un)}"' in source
    assert "export USER" in source


def test_user_is_exported_before_the_first_gws_invocation() -> None:
    """Pinning USER after the send would not help; ordering is the whole point."""
    source = NOTIFY_SCRIPT.read_text()
    assert source.index("export USER") < source.index('"${GWS_BIN}" gmail +send')


def test_bounded_command_kills_the_stuck_process_group_with_a_json_receipt(tmp_path: Path) -> None:
    """A stuck checker must return a terminal receipt instead of holding launchd forever."""
    source = NOTIFY_SCRIPT.read_text()
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))
    sleeper = tmp_path / "sleeper"
    sleeper.write_text("#!/bin/bash\nsleep 5 &\nwait\n")
    sleeper.chmod(0o755)
    script = tmp_path / "timeout_harness.sh"
    script.write_text(
        f'set -uo pipefail\nsource "{loadable}"\n'
        f'BOUNDED_COMMAND_TIMEOUT_JSON=1 _run_command_bounded 0.1 "{sleeper}"\n'
        'printf "\\nexit=%s" "$?"\n'
    )

    started = time.monotonic()
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)

    assert time.monotonic() - started < 2
    receipt, _, code = result.stdout.rpartition("\nexit=")
    assert int(code) == 124
    payload = json.loads(receipt)
    assert payload["timeout_seconds"] == 0.1
    assert "process group terminated" in payload["error"]


# ----- self-update -------------------------------------------------------------------
#
# These pin refresh behavior for a static machine-local launchd checkout.
# A clean fast-forward advances source; a refusal is loud and does not stop checks.
#
#     if the pull is a clean fast-forward, it logs [OK] and the checkout advances
#     if the pull is NOT a clean fast-forward, it logs [ERROR], never silently
#     a failed pull never blocks the health check itself from still running
#     the pull runs before anything else in _main
#
# Only the git executable is faked, so _self_update's own guard logic, argument
# construction, and working directory are the code under test.

_SELF_UPDATE_HARNESS = """
set -uo pipefail
source "{loadable}"
GIT_BIN="{fake_git}"
REPO_DIR="{repo_dir}"
_self_update
printf 'exit=%s' "$?"
"""

_FAKE_GIT = """#!/bin/bash
printf 'ARGS=%s\\n' "$*" > "{record}"
touch ./ran-in-this-dir
printf '%s\\n' "{stdout}"
exit {code}
"""


def _run_self_update(
    tmp_path: Path, *, git_exit: int, git_stdout: str = "Already up to date."
) -> tuple[int, str, str, Path]:
    """Run the REAL _self_update against a fake git binary.

    Returns (exit, log output, git argv, repo_dir).
    """
    source = NOTIFY_SCRIPT.read_text()
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))

    record = tmp_path / "git_invocation.txt"
    fake_git = tmp_path / "git"
    fake_git.write_text(_FAKE_GIT.format(record=record, code=git_exit, stdout=git_stdout))
    fake_git.chmod(0o755)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    script = tmp_path / "self_update_harness.sh"
    script.write_text(
        _SELF_UPDATE_HARNESS.format(loadable=loadable, fake_git=fake_git, repo_dir=repo_dir)
    )
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert "exit=" in result.stdout, f"harness produced no verdict: {result.stderr}"
    log_output, _, tail = result.stdout.rpartition("exit=")
    code = int(tail)
    invocation = record.read_text() if record.exists() else ""
    return code, log_output, invocation, repo_dir


def test_self_update_pulls_ff_only_from_origin_main(tmp_path: Path) -> None:
    _, _, invocation, _ = _run_self_update(tmp_path, git_exit=0)
    assert invocation.strip() == "ARGS=pull --ff-only origin main"


def test_self_update_runs_in_the_repo_working_directory(tmp_path: Path) -> None:
    _, _, _, repo_dir = _run_self_update(tmp_path, git_exit=0)
    assert (repo_dir / "ran-in-this-dir").exists()


def test_self_update_success_logs_ok_and_returns_zero(tmp_path: Path) -> None:
    code, log_output, _, _ = _run_self_update(
        tmp_path, git_exit=0, git_stdout="Updating 8c9f8cc..b336512"
    )
    assert code == 0
    assert "[OK] self-update:" in log_output
    assert "Updating 8c9f8cc..b336512" in log_output
    assert "[ERROR]" not in log_output


def test_self_update_failure_is_loud_and_returns_nonzero(tmp_path: Path) -> None:
    """A pull that is not a clean fast-forward must never fail silently."""
    code, log_output, _, _ = _run_self_update(
        tmp_path, git_exit=1, git_stdout="fatal: Not possible to fast-forward, aborting."
    )
    assert code != 0
    assert "[ERROR] self-update: git pull --ff-only origin main failed" in log_output
    assert "fatal: Not possible to fast-forward, aborting." in log_output


def test_self_update_reports_error_when_git_is_absent(tmp_path: Path) -> None:
    source = NOTIFY_SCRIPT.read_text()
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))
    script = tmp_path / "no_git_harness.sh"
    script.write_text(
        f'set -uo pipefail\nsource "{loadable}"\nGIT_BIN=""\n'
        '_self_update\nprintf \'exit=%s\' "$?"\n'
    )
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert result.stdout.rsplit("exit=", 1)[1] != "0"
    assert "[ERROR] self-update: git not found" in result.stdout


def test_git_bin_resolved_by_absolute_path_first() -> None:
    """Same reasoning as GWS_BIN: a channel the maintainer relies on should not depend on launchd's PATH."""
    source = NOTIFY_SCRIPT.read_text()
    assert 'GIT_BIN_DEFAULT="/usr/bin/git"' in source


def test_self_update_runs_before_the_check_starts() -> None:
    """Ordering is the whole point: self-update must happen first, every run."""
    source = NOTIFY_SCRIPT.read_text()
    main_body = source[source.index("_main() {") :]
    assert main_body.index("_self_update") < main_body.index(
        "starting check for the music-dj-tools repo"
    )


_INTEGRATION_HARNESS = """
set -uo pipefail
MDT_ALERT_EMAIL="{alert_email}"
source "{loadable}"
GIT_BIN="{fake_git}"
REPO_DIR="{repo_dir}"
UV_BIN="{fake_uv}"
CHECK_SCRIPT="{repo_dir}/scripts/ci_health_check.py"
LOG_FILE="{log_file}"
_main
printf 'exit=%s' "$?"
"""

_FAKE_UV_ALWAYS_HEALTHY = """#!/bin/bash
printf '{{"exit_code": 0, "ok": true, "checks": []}}\\n'
exit 0
"""


def test_a_failed_self_update_still_lets_the_health_check_run(tmp_path: Path) -> None:
    """The whole point: stale code that keeps running beats a watchdog that goes dark.

    Stubs GIT_BIN to fail the pull and UV_BIN to a fake checker that always reports
    healthy, then runs the REAL _main end to end and confirms both the self-update
    failure AND the subsequent successful check appear in the log.
    """
    source = NOTIFY_SCRIPT.read_text()
    loadable = tmp_path / "notify_loadable.sh"
    loadable.write_text(source.replace('_main "$@"', ":"))

    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/bash\nexit 1\n")
    fake_git.chmod(0o755)

    fake_uv = tmp_path / "uv"
    fake_uv.write_text(_FAKE_UV_ALWAYS_HEALTHY)
    fake_uv.chmod(0o755)

    repo_dir = tmp_path / "repo"
    (repo_dir / "scripts").mkdir(parents=True)
    (repo_dir / "scripts" / "ci_health_check.py").write_text("# stub\n")

    log_file = tmp_path / "mdt-ci-health.log"

    script = tmp_path / "integration_harness.sh"
    script.write_text(
        _INTEGRATION_HARNESS.format(
            alert_email=ALERT_EMAIL,
            loadable=loadable,
            fake_git=fake_git,
            repo_dir=repo_dir,
            fake_uv=fake_uv,
            log_file=log_file,
        )
    )
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert "exit=" in result.stdout, f"harness produced no verdict: {result.stderr}"
    log_output, _, tail = result.stdout.rpartition("exit=")
    assert int(tail) == 0, "a dead self-update must not fail the run"
    assert "[ERROR] self-update: git pull --ff-only origin main failed" in log_output
    assert "[OK] ci-health: all checks passed, no alert needed" in log_output
