"""Fleet process registry: naming convention + drift check (DEVOPS-14, issue #2542).

No live SSH here: every test injects a fake host collector so the suite is
deterministic and network-free, per this repo's usual fixture-injection
convention. The live wiring (`scripts.process_registry_gen`'s real ssh/
launchctl/systemctl collectors) is exercised by hand when the registry is
regenerated, not by this suite.

Regression lines:
  - if a launchd label starting with com.opendj. is marked a naming
    violation then broken
  - if a bare launchd label like opendj-hostcleanup-artifactcap (no
    com.opendj. prefix) is marked naming-OK then broken
  - if a systemd unit or Windows task starting with opendj- is marked a
    naming violation then broken
  - if a GitHub Actions workflow is marked a naming violation (the
    convention does not apply to workflow files) then broken
  - if a live owned unit with no registry row does not fail the check then
    broken
  - if a registry row whose unit no longer exists live does not fail the
    check then broken
  - if every live owned unit has a registry row and vice versa the check
    still fails then broken
  - if an UNREACHABLE host reads as a clean pass (the negative control:
    absence must never look like compliance) then broken
  - if a REACHABLE-but-EMPTY host with a registry row expecting units on it
    reads as a clean pass (the same trap, other direction) then broken
"""

from __future__ import annotations

import pytest

from scripts.process_registry_check import EXIT_DRIFT, EXIT_OK, EXIT_UNKNOWN, run_check
from scripts.process_registry_sources import (
    Host,
    HostResult,
    HostUnreachableError,
    ProcessUnit,
    SchedulerKind,
    naming_status,
)

pytestmark = pytest.mark.requirement("DEVOPS-14")


def _unit(
    host: str, unit: str, scheduler: SchedulerKind = SchedulerKind.LAUNCHD, owned: bool = True
) -> ProcessUnit:
    return ProcessUnit(
        host=host,
        scheduler=scheduler,
        unit=unit,
        schedule="n/a",
        state="n/a",
        last_exit="n/a",
        command="n/a",
        owned=owned,
        area="test",
        purpose="test fixture",
        naming=naming_status(scheduler, unit),
    )


# ------------------------------------------------------------- naming_status


def test_launchd_com_opendj_prefix_is_ok():
    assert naming_status(SchedulerKind.LAUNCHD, "com.opendj.host-disk-mac") == "ok"


def test_launchd_bare_opendj_label_is_a_violation():
    # This is a REAL finding from the live fleet (silver + Air): the label
    # predates the com.opendj. convention.
    assert naming_status(SchedulerKind.LAUNCHD, "opendj-hostcleanup-artifactcap") == "violation"


def test_launchd_com_af_label_is_a_violation():
    assert naming_status(SchedulerKind.LAUNCHD, "com.af.opendj-preview-engine") == "violation"


def test_macos_app_instance_key_is_exempt():
    assert naming_status(SchedulerKind.LAUNCHD, "application.com.opendj.desktop.111.222") == "n/a"


def test_systemd_opendj_prefix_is_ok():
    assert naming_status(SchedulerKind.SYSTEMD_USER, "opendj-sink-triage") == "ok"


def test_systemd_non_opendj_prefix_is_a_violation():
    assert naming_status(SchedulerKind.SYSTEMD_USER, "idd-lane@1") == "violation"


def test_windows_task_naming_same_rule_as_systemd():
    assert naming_status(SchedulerKind.WINDOWS_TASK, "opendj-stems-sync") == "ok"
    assert naming_status(SchedulerKind.WINDOWS_TASK, "demucs-farm") == "violation"


def test_github_actions_convention_is_not_applicable():
    assert naming_status(SchedulerKind.GITHUB_ACTIONS, "e2e") == "n/a"


# --------------------------------------------------------------- run_check

_HOST = Host("h1", SchedulerKind.LAUNCHD, "fixture host", None)


def _fake_collector(units: list[ProcessUnit]):
    def _collect(host: Host) -> HostResult:
        return HostResult(host=host.name, reachable=True, units=units)

    return _collect


def _unreachable_collector(reason: str = "ssh timed out"):
    def _collect(host: Host) -> HostResult:
        return HostResult(host=host.name, reachable=False, error=reason)

    return _collect


def test_matching_live_and_registry_is_clean():
    live = [_unit("h1", "com.opendj.thing")]
    registry = {"h1": [{"unit": "com.opendj.thing", "owned": True}]}
    code, checks = run_check([_HOST], registry, _fake_collector(live))
    assert code == EXIT_OK
    assert checks[0].status == "clean"


def test_unregistered_live_unit_fails_the_check():
    live = [_unit("h1", "com.opendj.new-thing")]
    registry: dict = {"h1": []}
    code, checks = run_check([_HOST], registry, _fake_collector(live))
    assert code == EXIT_DRIFT
    assert any("UNREGISTERED" in line for line in checks[0].detail)


def test_stale_registry_row_fails_the_check():
    live: list[ProcessUnit] = []
    registry = {"h1": [{"unit": "com.opendj.gone", "owned": True}]}
    code, checks = run_check([_HOST], registry, _fake_collector(live))
    assert code == EXIT_DRIFT
    assert any("STALE" in line for line in checks[0].detail)


def test_unreachable_host_is_unknown_never_clean():
    """Negative control: absence of data must not read as compliance."""
    registry = {"h1": [{"unit": "com.opendj.thing", "owned": True}]}
    code, checks = run_check([_HOST], registry, _unreachable_collector())
    assert code == EXIT_UNKNOWN
    assert checks[0].status == "unknown"
    assert "unreachable" in checks[0].detail[0]


def test_reachable_empty_host_with_expected_row_is_drift_not_clean():
    """Negative control, other direction: zero units back is the same shape
    an unreachable host or a suppressed error would produce. A registry that
    still expects a row there must report STALE, not silently pass."""
    registry = {"h1": [{"unit": "com.opendj.thing", "owned": True}]}
    code, checks = run_check([_HOST], registry, _fake_collector([]))
    assert code == EXIT_DRIFT
    assert checks[0].status == "drift"


def test_missing_registry_file_is_unknown():
    code, checks = run_check([_HOST], None, _fake_collector([]))
    assert code == EXIT_UNKNOWN
    assert checks[0].status == "unknown"


def test_not_owned_units_are_ignored_by_the_check():
    live = [_unit("h1", "com.apple.some-framework-helper", owned=False)]
    registry: dict = {"h1": []}
    code, _checks = run_check([_HOST], registry, _fake_collector(live))
    assert code == EXIT_OK


def test_collector_raising_unreachable_is_caught_as_unknown_not_an_exception():
    def _raises(_host: Host) -> HostResult:
        raise HostUnreachableError("boom")

    registry = {"h1": []}

    # collect_host in process_registry_gen catches this; run_check's collector
    # contract expects a HostResult back, so wire the same catch here to
    # prove the boundary the real collect_host enforces.
    def _collect(host: Host) -> HostResult:
        try:
            return _raises(host)
        except HostUnreachableError as exc:
            return HostResult(host=host.name, reachable=False, error=str(exc))

    code, checks = run_check([_HOST], registry, _collect)
    assert code == EXIT_UNKNOWN
    assert "boom" in checks[0].detail[0]
