"""The adversarial scenario verbs refuse to pass vacuously (plan W9).

[if] an adversarial verb passes without exercising its failure [then] fail, [else stop].

``test_scenarios.py`` runs every committed scenario and proves the verbs can
PASS. This module proves they can FAIL: each case is a small scenario that
must raise :class:`ScenarioError` for the reason it names, because a verb
that silently no-ops would let a scenario "converge" without exercising
anything. The last test is a coverage tripwire over the committed YAML.

Acceptance, one test each:
- if a scenario whose armed fault never fires can pass then broken
- if a sync step expecting a transport fault passes when none fired then broken
- if delete silently no-ops on a missing or already tombstoned row then broken
- if restore_hub restores from a snapshot that was never taken then broken
- if assert_converged cannot see the tombstone a delete step wrote then broken
- if a new verb or widened table has no committed scenario exercising it then broken
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.cloudsync.scenario_runner import (
    ScenarioError,
    discover_scenarios,
    load_scenario,
    run_scenario_sim,
)
from tests.cloudsync.scenario_runner_common import _ADVERSARIAL_ACTIONS, _EDITABLE_TABLES

pytestmark = pytest.mark.requirement("CAT-04")

#: The tables plan W9 added to the edit verb. The four original ones predate
#: this tripwire and are covered (or not) by the scenarios that introduced them.
WIDENED_TABLES: frozenset[str] = frozenset(
    {"track_fields", "track_locations", "playlist_memberships"}
)

_MACHINES: list[dict[str, str]] = [
    {"id": "hub", "role": "hub"},
    {"id": "spoke-a", "role": "spoke"},
    {"id": "spoke-b", "role": "spoke"},
]
_SEED: dict[str, Any] = {
    "tracks": [{"stable_id": "trk-1", "title": "original", "on": ["spoke-a", "spoke-b"]}]
}


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer sim registration."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


def _run(tmp_path: Path, steps: list[dict[str, Any]]) -> None:
    """Write a one-off scenario, load it through the real parser, run it."""
    path = tmp_path / "probe.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "probe",
                "description": "a one-off negative case",
                "machines": _MACHINES,
                "seed": _SEED,
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_scenario_sim(load_scenario(path), run_dir)


def _edit(title: str) -> dict[str, Any]:
    return {
        "on": "spoke-a",
        "action": "edit",
        "args": {"table": "tracks", "stable_id": "trk-1", "set": {"title": title}},
    }


def test_a_fault_that_never_fires_fails_the_scenario(tmp_path: Path) -> None:
    """if a scenario whose armed fault never fires can pass then broken"""
    print("if a scenario whose armed fault never fires can pass then broken")
    steps = [
        _edit("edited"),
        {
            "on": "spoke-a",
            "action": "fault",
            "args": {"mode": "fail_before_send", "path": "push", "nth": 2},
        },
        {"on": "spoke-a", "action": "sync"},
    ]
    with pytest.raises(ScenarioError, match="never fired"):
        _run(tmp_path, steps)


def test_expecting_a_transport_error_with_no_fault_fails(tmp_path: Path) -> None:
    """if a sync step expecting a transport fault passes when none fired then broken"""
    print("if a sync step expecting a transport fault passes when none fired then broken")
    with pytest.raises(ScenarioError, match="no armed fault fired"):
        _run(tmp_path, [{"on": "spoke-a", "action": "sync", "args": {"expect_error": "transport"}}])


@pytest.mark.parametrize(
    "steps",
    [
        [
            {
                "on": "spoke-a",
                "action": "delete",
                "args": {"table": "tracks", "pk": {"stable_id": "no-such"}},
            }
        ],
        [
            {
                "on": "spoke-a",
                "action": "delete",
                "args": {"table": "tracks", "pk": {"stable_id": "trk-1"}},
            },
            {
                "on": "spoke-a",
                "action": "delete",
                "args": {"table": "tracks", "pk": {"stable_id": "trk-1"}},
            },
        ],
    ],
    ids=["missing-row", "already-tombstoned"],
)
def test_delete_refuses_a_row_it_cannot_tombstone(
    tmp_path: Path, steps: list[dict[str, Any]]
) -> None:
    """if delete silently no-ops on a missing or already tombstoned row then broken"""
    print("if delete silently no-ops on a missing or already tombstoned row then broken")
    with pytest.raises(ScenarioError, match="no LIVE tracks row"):
        _run(tmp_path, steps)


def test_restoring_an_untaken_snapshot_fails(tmp_path: Path) -> None:
    """if restore_hub restores from a snapshot that was never taken then broken"""
    print("if restore_hub restores from a snapshot that was never taken then broken")
    with pytest.raises(ScenarioError, match="no snapshot 'never-taken'"):
        _run(
            tmp_path,
            [
                {
                    "on": "hub",
                    "action": "restore_hub",
                    "args": {"op": "restore", "name": "never-taken"},
                }
            ],
        )


def test_a_wrong_expectation_after_a_delete_fails(tmp_path: Path) -> None:
    """if assert_converged cannot see the tombstone a delete step wrote then broken"""
    print("if assert_converged cannot see the tombstone a delete step wrote then broken")
    steps = [
        {
            "on": "spoke-a",
            "action": "delete",
            "args": {
                "table": "tracks",
                "pk": {"stable_id": "trk-1"},
                "updated_at": "2026-02-02T00:00:00+00:00",
            },
        },
        {
            "on": "spoke-a",
            "action": "assert_converged",
            "args": {
                "table": "tracks",
                "pk": {"stable_id": "trk-1"},
                "field": "deleted_at",
                "equals": None,
            },
        },
    ]
    with pytest.raises(ScenarioError, match=r"2026-02-02T00:00:00\.000000\+00:00"):
        _run(tmp_path, steps)


def test_every_new_verb_and_table_is_exercised_by_a_committed_scenario() -> None:
    """if a new verb or widened table has no committed scenario exercising it then broken"""
    print("if a new verb or widened table has no committed scenario exercising it then broken")
    actions: set[str] = set()
    tables: set[str] = set()
    for path in discover_scenarios():
        for step in load_scenario(path).steps:
            actions.add(step.action)
            if step.action == "edit":
                tables.add(str(step.args["table"]))
    assert _ADVERSARIAL_ACTIONS | {"reorder"} <= actions, (
        f"no committed scenario uses {sorted((_ADVERSARIAL_ACTIONS | {'reorder'}) - actions)}"
    )
    assert WIDENED_TABLES <= _EDITABLE_TABLES, "a widened table is not editable at all"
    assert tables >= WIDENED_TABLES, (
        f"no committed scenario edits {sorted(WIDENED_TABLES - tables)}"
    )
