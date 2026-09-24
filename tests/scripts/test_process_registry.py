"""Fleet process registry: naming convention + drift check (DEVOPS-14, issue #2542).

[if] a live owned unit drifts from the registry [then] the check fails loudly, [else stop].

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
  - if a recorded unit name, command or schedule carrying a real home
    directory, mailbox or tailnet name reaches the tracked artifact then
    broken (OSSPUB-01: the artifact is generated and committed)
  - if scrubbing a value twice differs from scrubbing it once then broken (a
    regeneration would drift, and a guard regeneration undoes is not a guard)
  - if the drift check compares an unscrubbed live name against a scrubbed
    registry row then broken (the row would read STALE forever)
"""

from __future__ import annotations

import json

import pytest

from scripts.oss_tip_audit import findings_in_text
from scripts.process_registry_check import EXIT_DRIFT, EXIT_OK, EXIT_UNKNOWN, run_check
from scripts.process_registry_gen import (
    build_doc,
    carry_forward_unqueried_host,
    merge_with_previous,
    render_markdown,
)
from scripts.process_registry_sources import (
    Host,
    HostResult,
    HostUnreachableError,
    ProcessUnit,
    SchedulerKind,
    naming_status,
    scrub_identities,
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


# --------------------------------------------------------- identity scrub

# Assembled from fragments for the same reason the sibling audit suite does it: the
# going-public gate scans this file too, so a literal home directory or address here
# would make the clean-tree assertion unsatisfiable -- which is itself the proof that
# the gate reads tests.
_HOME = "/" + "Users" + "/" + "jdoe"
_MAILBOX = "someone" + "@" + "not-a-real-provider" + ".com"
_TAILNET = "box" + "." + "some-other-real-looking-tailnet" + ".ts.net"
_STAMP = "2026-09-17T00:00:00Z"


def _unit_carrying_identities() -> ProcessUnit:
    """A recorded unit whose name and command both carry live-host identities."""
    return ProcessUnit(
        host="h1",
        scheduler=SchedulerKind.LAUNCHD,
        unit="com.af.backup-" + _MAILBOX,
        schedule="see plist",
        state="running",
        last_exit="0",
        command=f"launchd -> {_HOME}/Library/LaunchAgents on {_TAILNET}",
        owned=True,
        area="test",
        purpose="test fixture",
        naming="violation",
    )


def test_a_recorded_identity_never_reaches_the_artifact() -> None:
    """The registry is GENERATED and COMMITTED, so a real identity in a unit name or
    command off any queried host lands in the repository and reddens the going-public
    audit. Scrub at the serialization boundary, which is where a record becomes the
    artifact, rather than editing the artifact."""
    text = json.dumps(_unit_carrying_identities().to_json(), indent=2)
    assert list(findings_in_text("process-registry.json", text)) == []
    for identity in (_HOME, _MAILBOX, _TAILNET):
        assert identity not in text


def test_scrubbing_twice_is_the_same_as_scrubbing_once() -> None:
    """A guard that regeneration silently undoes is not a guard. Every placeholder is
    itself exempt from the rule it replaces, so the second pass has nothing left to do
    and cannot drift the artifact."""
    raw = f'{{"unit": "com.af.backup-{_MAILBOX}", "command": "{_HOME}/Library on {_TAILNET}"}}'
    once = scrub_identities(raw)
    assert once != raw
    assert list(findings_in_text("registry.json", once)) == []
    assert scrub_identities(once) == once


def test_a_regeneration_cannot_reintroduce_a_scrubbed_identity() -> None:
    """The path where this could actually fail is not the live-collection one: when a
    host goes unreachable `merge_with_previous` carries its WHOLE previous block
    forward verbatim, so a scrub that lived only in the collector would leave those
    rows untouched on every later pass. Assert the carried block is unchanged and
    still carries no finding."""
    live = [HostResult(host="h1", reachable=True, units=[_unit_carrying_identities()])]
    first = build_doc(merge_with_previous(live, {}), _STAMP)
    previous = {h["host"]: h for h in first["hosts"]}
    again = build_doc(
        merge_with_previous(
            [HostResult(host="h1", reachable=False, error="ssh timed out")], previous
        ),
        _STAMP,
    )
    assert again["hosts"][0]["units"] == first["hosts"][0]["units"]
    assert list(findings_in_text("process-registry.json", json.dumps(again))) == []


def test_hosts_filter_carries_a_reachable_hosts_real_units_forward_as_stale(
) -> None:
    """A --hosts filter that skips a previously-reachable host (PR #3827
    review, docs/ops/process-registry.json) must not report it as newly
    unreachable with no known-good timestamp. That combination -- reachable
    flipped to False, stale_as_of left null -- reads as "unreachable and we
    don't even know when it last worked", worse than either true state.
    """
    prev_block = {
        "host": "agentbox",
        "reachable": True,
        "error": None,
        "stale_as_of": None,
        "generated_at_utc_of_block": "2026-09-20T00:27:53Z",
        "units": [_unit_carrying_identities().to_json()],
    }
    carried = carry_forward_unqueried_host(prev_block)
    assert carried["reachable"] is False
    assert carried["stale_as_of"] == "2026-09-20T00:27:53Z"
    assert carried["units"] == prev_block["units"]
    assert "not queried this pass" in carried["error"]
    assert carried["last_known_error"] is None


def test_hosts_filter_error_does_not_nest_for_a_reachable_host_carried_twice(
) -> None:
    """A previously-reachable host skipped by --hosts on two regens in a
    row must keep last_known_error as None, not pick up the not-queried
    note itself as if it were a real error (claude-review, PR #3827, round
    6, P3): the round-4 test only covered a host that was unreachable with
    a REAL error before the first carry, so it missed this case, where the
    first carry's last_known_error is legitimately None and the second
    carry's `None or prev_block["error"]` fallback grabs the constant note
    instead of staying None."""
    prev_block = {
        "host": "agentbox",
        "reachable": True,
        "error": None,
        "stale_as_of": None,
        "generated_at_utc_of_block": "2026-09-20T00:27:53Z",
        "units": [],
    }
    once = carry_forward_unqueried_host(prev_block)
    twice = carry_forward_unqueried_host(once)
    assert once["last_known_error"] is None
    assert twice["last_known_error"] is None
    assert twice["error"] == once["error"] == "not queried this pass (--hosts filter)"
    assert twice["stale_as_of"] == once["stale_as_of"] == "2026-09-20T00:27:53Z"


def test_hosts_filter_preserves_an_unreachable_hosts_real_error(
) -> None:
    """A host that was already unreachable before the --hosts filter skipped
    it must keep saying WHY (a real ssh/DNS error), not just "not queried
    this pass" with the original diagnosis discarded."""
    prev_block = {
        "host": "bifrost2",
        "reachable": False,
        "error": "ssh: Could not resolve hostname bifrost2: Name or service not known",
        "stale_as_of": "2026-09-16T06:11:17Z",
        "generated_at_utc_of_block": None,
        "units": [],
    }
    carried = carry_forward_unqueried_host(prev_block)
    assert carried["reachable"] is False
    assert carried["stale_as_of"] == "2026-09-16T06:11:17Z"
    assert "not queried this pass" in carried["error"]
    assert "Could not resolve hostname bifrost2" in carried["last_known_error"]


def test_hosts_filter_error_does_not_nest_across_repeated_regens() -> None:
    """A second, later --hosts regen that skips the same host again must not
    re-wrap an already-carried block's error: `error` stays the constant
    note and `last_known_error` stays the ORIGINAL diagnosis, not a
    "not queried -- last known: not queried -- last known: ..." pileup."""
    prev_block = {
        "host": "bifrost2",
        "reachable": False,
        "error": "ssh: Could not resolve hostname bifrost2: Name or service not known",
        "stale_as_of": "2026-09-16T06:11:17Z",
        "generated_at_utc_of_block": None,
        "units": [],
    }
    once = carry_forward_unqueried_host(prev_block)
    twice = carry_forward_unqueried_host(once)
    assert twice["error"] == once["error"] == "not queried this pass (--hosts filter)"
    assert twice["last_known_error"] == once["last_known_error"]
    assert "Could not resolve hostname bifrost2" in twice["last_known_error"]


def test_rendered_markdown_never_claims_a_carried_host_was_checked_this_pass(
) -> None:
    """A carried-forward host's real last-known error must render alongside
    the not-queried note, not in place of it (claude-review, PR #3827,
    round 5, P3): showing only last_known_error reports a measurement
    ("UNREACHABLE this pass") that never happened this pass at all."""
    prev_block = {
        "host": "bifrost2",
        "reachable": False,
        "error": "ssh: Could not resolve hostname bifrost2: Name or service not known",
        "stale_as_of": "2026-09-16T06:11:17Z",
        "generated_at_utc_of_block": None,
        "units": [],
    }
    carried = carry_forward_unqueried_host(prev_block)
    md = render_markdown([carried], _STAMP)
    assert "not queried this pass" in md
    assert "Could not resolve hostname bifrost2" in md
    # Both facts on the same line, not one silently replacing the other.
    for line in md.splitlines():
        if "not queried this pass" in line:
            assert "Could not resolve hostname bifrost2" in line


def test_a_scrubbed_row_still_matches_its_live_original() -> None:
    """The drift check compares a live name against a committed row, and only one of
    those two has been scrubbed. Compare like with like, or every scrubbed row reads
    STALE forever and the check reports drift on a registry that is correct."""
    live = [_unit("h1", "com.af.backup-" + _MAILBOX)]
    row = _unit("h1", "com.af.backup-" + _MAILBOX).to_json()
    code, checks = run_check([_HOST], {"h1": [row]}, _fake_collector(live))
    assert code == EXIT_OK
    assert checks[0].status == "clean"


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
