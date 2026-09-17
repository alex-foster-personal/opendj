"""``opendj update check|apply`` on the installed CLI (AGENT-13).

Driven end to end: the real ``opendj`` entry point, a real engine on a real
socket and a real lock file, a real manifest served by a real HTTP channel,
and a real relaunch that is a second engine process on a second port. The one
thing a test cannot do is install a signed .app, so the SHELL is simulated the
way ``conftest.PerformancePage`` simulates the performance page: it polls the
real order route, runs the real relaunch, and posts the real result document.

Single-line acceptance, in the repo's "if X then broken" shape:

- if `check` exits 0 on an unreachable channel, a script reads an outage as
  "current" -> broken.
- if `check` exits 0 on `ahead-of-channel`, an agent reads it as an answer it
  can act on -> broken.
- if `apply` reports success without both before/after identities changing to
  the announced version, a failed install reads as an applied one -> broken.
- if `apply` posts anything when the channel is not offering an update, it
  installs on a machine that asked nothing of it -> broken.
- if `apply` waits forever for a relaunch that never comes, it hangs a caller
  instead of failing loudly -> broken.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from apps.opendj_cli.__main__ import main
from apps.opendj_cli.update_cli import (
    EXIT_APPLY_FAILED,
    EXIT_NOT_APPLIED,
    EXIT_USAGE,
)
from tests.opendj_cli.updater_rig import Updater

RUNNING_VERSION = "0.1.0"
RELEASED_VERSION = "0.1.1"
RUNNING_SHA = "0d41a28c0000000000000000000000000000beef"
RELEASED_SHA = "deadbeef0000000000000000000000000000beef"


def _check(updater: Updater, *extra: str) -> int:
    return main(["--lock", str(updater.lock_path), "update", "check", *extra])


def _apply(updater: Updater, *extra: str) -> int:
    return main(["--lock", str(updater.lock_path), "update", "apply", *extra])


@pytest.mark.requirement("AGENT-13")
def test_check_names_the_status_and_both_versions(updater: Updater, capsys: Any) -> None:
    """[if] check finds an available update [then] it prints all three fields, [else stop].
    [if] the channel offers a newer build [then] check prints the status,
    the current version and the available version and exits 0, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    assert _check(updater) == 0
    out = capsys.readouterr().out
    assert "status: update-available" in out
    assert f"current_version: {RUNNING_VERSION}" in out
    assert f"available_version: {RELEASED_VERSION}" in out


@pytest.mark.requirement("AGENT-13")
def test_check_exits_zero_when_the_channel_offers_nothing_new(
    updater: Updater, capsys: Any
) -> None:
    """[if] the channel offers no update [then] check exits 0 as up-to-date, [else stop].
    [if] this build is what the channel offers [then] check exits 0 naming
    it current, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RUNNING_VERSION
    )
    assert _check(updater) == 0
    assert "status: up-to-date" in capsys.readouterr().out


@pytest.mark.requirement("AGENT-13")
def test_check_json_carries_the_engine_document(
    updater: Updater, capsys: Any
) -> None:
    """[if] check runs with --json [then] stdout parses as the engine's document, [else stop].
    [if] check runs with --json [then] stdout is the engine's own document,
    [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    # --json is a global flag, so it goes before the subcommand, exactly as it
    # does for `opendj --json api GET /api/v1/health`.
    assert main(["--lock", str(updater.lock_path), "--json", "update", "check"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "update-available"
    assert document["current_version"] == RUNNING_VERSION
    assert document["available_version"] == RELEASED_VERSION


@pytest.mark.requirement("AGENT-13")
def test_check_exits_nonzero_when_the_channel_is_behind_this_build(
    updater: Updater, capsys: Any
) -> None:
    """[if] this build is ahead of the channel [then] check errors, not current, [else stop].
    [if] this build is newer than the channel [then] check exits nonzero
    saying so, and never claims it is current, [else stop]."""
    updater.start(
        app_version="0.2.0", git_sha_full=RUNNING_SHA, publish=RUNNING_VERSION
    )
    assert _check(updater) == EXIT_NOT_APPLIED
    captured = capsys.readouterr()
    assert "ahead-of-channel" in captured.out + captured.err


@pytest.mark.requirement("AGENT-13")
def test_check_exits_nonzero_when_nothing_answers_the_lock(
    updater: Updater, capsys: Any
) -> None:
    """[if] the lock names an unreachable port [then] check exits 2 naming the URL, [else stop].
    [if] the lock names a port nothing answers on [then] check exits nonzero
    naming the URL it tried, [else stop]."""
    updater.lock_path.write_text(
        json.dumps({"pid": 1, "role": "opendj-engine", "host": "127.0.0.1", "port": 9}),
        encoding="utf-8",
    )
    assert _check(updater) == 2
    captured = capsys.readouterr()
    assert "127.0.0.1:9" in captured.out + captured.err


@pytest.mark.requirement("AGENT-13")
def test_apply_installs_and_prints_both_identities(updater: Updater, capsys: Any) -> None:
    """[if] apply installs an offered build [then] it exits 0 with both identities, [else stop].
    [if] the channel offers a newer build [then] apply posts the order, waits
    for the relaunch and exits 0 only once both app_version and git_sha_full
    are the announced build, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(
            app_version=RELEASED_VERSION, git_sha_full=RELEASED_SHA
        )
    )
    assert _apply(updater, "--timeout-s", "60") == 0
    updater.wait_for_shell()
    assert updater.shell_error is None
    # The positive control for the counters the refusal tests assert are zero.
    assert updater.apply_posts == 1, "exactly one order reaches the engine"
    assert updater.shell_claims == 1, "the shell must install exactly one release"
    out = capsys.readouterr().out
    assert f"app_version={RUNNING_VERSION}" in out and RUNNING_SHA in out
    assert f"app_version={RELEASED_VERSION}" in out and RELEASED_SHA in out


@pytest.mark.requirement("AGENT-13")
def test_apply_sends_nothing_when_the_channel_offers_nothing(
    updater: Updater, capsys: Any
) -> None:
    """[if] the channel offers no update [then] apply errors and posts nothing, [else stop].
    [if] the channel is not offering an update [then] apply exits nonzero
    with the status and detail and posts no order, [else stop].

    The count is taken on the ENGINE, not on the CLI's behaviour: the engine
    refuses such an order too, so a CLI that posted anyway and was refused
    would otherwise look exactly like one that never posted. The shell is
    running throughout as the positive control for the same rig.
    """
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RUNNING_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(
            app_version=RELEASED_VERSION, git_sha_full=RELEASED_SHA
        )
    )
    assert _apply(updater, "--timeout-s", "5") != 0
    captured = capsys.readouterr()
    assert "up-to-date" in captured.out + captured.err
    assert updater.apply_posts == 0, "apply must post nothing when there is no update"
    assert updater.shell_claims == 0


@pytest.mark.requirement("AGENT-13")
def test_apply_surfaces_the_409_when_one_is_already_in_flight(
    updater: Updater, capsys: Any
) -> None:
    """[if] another apply is already pending [then] apply exits nonzero naming it, [else stop].
    [if] another apply is already pending [then] apply exits nonzero with the
    409 status and detail, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    command_id = updater.enqueue_apply()
    assert _apply(updater, "--timeout-s", "5") != 0
    captured = capsys.readouterr()
    assert command_id in captured.out + captured.err
    assert "already pending or claimed" in captured.out + captured.err


@pytest.mark.requirement("AGENT-13")
def test_apply_refuses_a_relaunch_that_is_not_the_announced_version(
    updater: Updater, capsys: Any
) -> None:
    """[if] the relaunch reports an unannounced version [then] apply exits nonzero, [else stop].
    [if] the relaunched app reports a build the channel did not announce
    [then] apply exits nonzero rather than reporting success, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(app_version="0.1.2", git_sha_full=RELEASED_SHA)
    )
    assert _apply(updater, "--timeout-s", "60") != 0
    updater.wait_for_shell()
    captured = capsys.readouterr()
    assert "0.1.2" in captured.out + captured.err
    assert RELEASED_VERSION in captured.out + captured.err


@pytest.mark.requirement("AGENT-13")
def test_apply_bounds_the_wait_and_fails_loudly(updater: Updater, capsys: Any) -> None:
    """[if] no shell claims the order [then] apply exits nonzero by its deadline, [else stop].
    [if] no shell ever claims the order [then] apply exits nonzero within its
    deadline rather than hanging, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    started = time.monotonic()
    assert _apply(updater, "--timeout-s", "2") != 0
    waited = time.monotonic() - started
    assert waited < 60.0, f"apply must give up at its deadline, waited {waited:.1f}s"
    captured = capsys.readouterr()
    assert "timeout" in (captured.out + captured.err).lower()


@pytest.mark.requirement("AGENT-13")
def test_apply_exits_three_when_the_shell_reports_a_failed_install(
    updater: Updater, capsys: Any
) -> None:
    """[if] the shell reports a failed install [then] apply exits 3 with its error, [else stop].
    [if] the shell reports the install failed [then] apply exits 3 with the
    shell's own error, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(
            app_version=RELEASED_VERSION, git_sha_full=RELEASED_SHA
        ),
        result={
            "status": "failed",
            "outcome": "refused",
            "error": "minisign verify failed",
        },
    )
    assert _apply(updater, "--timeout-s", "60") == EXIT_APPLY_FAILED
    captured = capsys.readouterr()
    assert "minisign verify failed" in captured.out + captured.err


@pytest.mark.requirement("AGENT-13")
def test_an_unknown_update_verb_is_a_usage_error(updater: Updater, capsys: Any) -> None:
    """[if] update gets an unknown verb [then] it exits usage naming check/apply, [else stop].
    [if] update is given a verb it does not have [then] it exits 1 and names
    the two it does, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    assert main(["update", "install"]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert "check" in captured.out + captured.err
    assert "apply" in captured.out + captured.err


@pytest.mark.requirement("AGENT-13")
def test_apply_refuses_a_relaunch_that_never_changes_the_identity(
    updater: Updater, capsys: Any
) -> None:
    """[if] the relaunch keeps the same identity [then] apply exits nonzero, [else stop].
    [if] the relaunched app reports the SAME build [then] apply exits nonzero
    instead of calling the install done, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(
            app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA
        )
    )
    assert _apply(updater, "--timeout-s", "3") != 0
    updater.wait_for_shell()
    captured = capsys.readouterr()
    assert RUNNING_SHA in captured.out + captured.err
