"""Fleet policy validator: one failing case and one passing case per rule.

Every test runs against a real migrated state DB (the ``conn`` fixture) with
a two-machine fleet seeded the way the schema requires.

- [if] a rule is silent on its failing case or fires on its passing case [then] fail, [else stop].
"""

from __future__ import annotations

import sqlite3

import pytest

from apps.shared.state.migrations_v10 import ASSET_KIND_CHECK_VALUES
from apps.sync_hub.policy_rules import (
    KindDefault,
    PinCell,
    PolicyCell,
    ProposedPolicy,
    Violation,
    has_errors,
    validate_fleet_policy,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-01")

HUB = "m-hub"
SPOKE = "m-spoke"
STAMP = "2026-09-11T12:00:00+00:00"


def _add_machine(conn: sqlite3.Connection, machine_id: str, *, is_hub: int) -> None:
    conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, first_seen, last_seen) "
        "VALUES (?, ?, 'macos', ?, ?, ?)",
        (machine_id, machine_id, is_hub, STAMP, STAMP),
    )


def _add_playlist(conn: sqlite3.Connection, playlist_id: str, *, deleted: bool) -> None:
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, created_at, "
        "updated_at, deleted_at) VALUES (?, ?, 'rekordbox', ?, ?, ?, ?)",
        (playlist_id, playlist_id, playlist_id, STAMP, STAMP, STAMP if deleted else None),
    )


def _set_policy(conn: sqlite3.Connection, machine_id: str, kind: str, mode: str) -> None:
    conn.execute(
        "INSERT INTO sync_policies(machine_id, asset_kind, mode, cache_budget_mb, "
        "updated_at, origin_device_id) VALUES (?, ?, ?, NULL, ?, ?)",
        (machine_id, kind, mode, STAMP, machine_id),
    )


@pytest.fixture
def fleet(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A valid fleet: one hub pinning every kind, one spoke streaming every kind,
    a live playlist 'pl-live' and a tombstoned 'pl-dead'."""
    _add_machine(conn, HUB, is_hub=1)
    _add_machine(conn, SPOKE, is_hub=0)
    for kind in ASSET_KIND_CHECK_VALUES:
        _set_policy(conn, HUB, kind, "pinned")
        _set_policy(conn, SPOKE, kind, "stream")
    _add_playlist(conn, "pl-live", deleted=False)
    _add_playlist(conn, "pl-dead", deleted=True)
    conn.commit()
    return conn


def _proposal(
    *,
    author: str = SPOKE,
    policies: tuple[PolicyCell, ...] = (),
    pins: tuple[PinCell, ...] = (),
    defaults: tuple[KindDefault, ...] = (),
    excluded_tables: tuple[str, ...] = (),
) -> ProposedPolicy:
    return ProposedPolicy(author, policies, pins, defaults, excluded_tables, (), ())


def _fired(conn: sqlite3.Connection, proposed: ProposedPolicy, rule_id: str) -> list[Violation]:
    return [v for v in validate_fleet_policy(conn, proposed) if v.rule_id == rule_id]


# ----- baseline ---------------------------------------------------------------------


def test_valid_fleet_with_empty_proposal_has_no_violations(fleet: sqlite3.Connection) -> None:
    """if a valid fleet with no change reports any violation then broken"""
    assert validate_fleet_policy(fleet, _proposal()) == []


def test_validator_never_writes(fleet: sqlite3.Connection) -> None:
    """if validating a proposal changes a single row then broken"""
    before = fleet.total_changes
    validate_fleet_policy(
        fleet,
        _proposal(
            excluded_tables=("tracks",), policies=(PolicyCell(SPOKE, "audio", "excluded", None),)
        ),
    )
    assert fleet.total_changes == before


# ----- completeness -----------------------------------------------------------------


def test_completeness_fires_for_machine_without_modes(fleet: sqlite3.Connection) -> None:
    """if a machine with no policy rows and no default passes completeness then broken"""
    _add_machine(fleet, "m-new", is_hub=0)
    fired = _fired(fleet, _proposal(), "completeness")
    assert {v.subject for v in fired} == {f"m-new/{k}" for k in ASSET_KIND_CHECK_VALUES}
    assert has_errors(fired)


def test_completeness_passes_with_explicit_defaults(fleet: sqlite3.Connection) -> None:
    """if explicit fleet defaults do not satisfy completeness then broken"""
    _add_machine(fleet, "m-new", is_hub=0)
    defaults = tuple(KindDefault(k, "stream") for k in ASSET_KIND_CHECK_VALUES)
    assert _fired(fleet, _proposal(defaults=defaults), "completeness") == []


# ----- durability -------------------------------------------------------------------


def test_durability_fires_when_every_machine_excludes_a_kind(fleet: sqlite3.Connection) -> None:
    """if an asset kind excluded on every machine passes durability then broken"""
    cells = (
        PolicyCell(HUB, "stem_bundle", "excluded", None),
        PolicyCell(SPOKE, "stem_bundle", "excluded", None),
    )
    fired = _fired(fleet, _proposal(author=HUB, policies=cells), "durability")
    assert [(v.subject, v.severity) for v in fired] == [("stem_bundle", "error")]


def test_durability_passes_when_one_machine_streams(fleet: sqlite3.Connection) -> None:
    """if a kind with an R2-backed stream holder fails durability then broken"""
    cells = (PolicyCell(HUB, "stem_bundle", "excluded", None),)
    assert _fired(fleet, _proposal(author=HUB, policies=cells), "durability") == []


# ----- cache budget ---------------------------------------------------------------------


def test_cache_budget_fires_when_mode_is_not_cached(fleet: sqlite3.Connection) -> None:
    """if a budget on a pinned cell, or a zero budget, passes then broken"""
    cells = (
        PolicyCell(SPOKE, "audio", "pinned", 500),
        PolicyCell(SPOKE, "anlz_cache", "cached", 0),
    )
    fired = _fired(fleet, _proposal(policies=cells), "cache_budget")
    assert {v.subject for v in fired} == {f"{SPOKE}/audio", f"{SPOKE}/anlz_cache"}


def test_cache_budget_passes_when_mode_is_cached(fleet: sqlite3.Connection) -> None:
    """if a positive budget on a cached cell fails then broken"""
    cells = (PolicyCell(SPOKE, "audio", "cached", 500),)
    assert _fired(fleet, _proposal(policies=cells), "cache_budget") == []


# ----- pin target ------------------------------------------------------------------------


def test_pin_target_fires_for_tombstoned_or_missing_playlist(fleet: sqlite3.Connection) -> None:
    """if a pin on a tombstoned or absent playlist passes then broken"""
    pins = (PinCell(SPOKE, "pl-dead", "pinned"), PinCell(SPOKE, "pl-absent", "pinned"))
    fired = _fired(fleet, _proposal(pins=pins), "pin_target")
    assert {v.subject for v in fired} == {f"{SPOKE}/pin/pl-dead", f"{SPOKE}/pin/pl-absent"}


def test_pin_target_passes_for_live_playlist(fleet: sqlite3.Connection) -> None:
    """if a pin on a live playlist fails then broken"""
    assert _fired(fleet, _proposal(pins=(PinCell(SPOKE, "pl-live", "pinned"),)), "pin_target") == []


# ----- library tables non-configurable -------------------------------------------------------


def test_library_table_exclusion_is_an_error(fleet: sqlite3.Connection) -> None:
    """if excluding a sync-set table or an unknown table passes then broken"""
    fired = _fired(
        fleet, _proposal(excluded_tables=("tracks", "no_such_table")), "library_non_configurable"
    )
    assert {(v.subject, v.severity) for v in fired} == {
        ("tracks", "error"),
        ("no_such_table", "error"),
    }


def test_excluding_a_machine_local_table_only_warns(fleet: sqlite3.Connection) -> None:
    """if excluding a table that never syncs is an error rather than a no-op warning then broken"""
    fired = _fired(fleet, _proposal(excluded_tables=("settings",)), "library_non_configurable")
    assert [(v.subject, v.severity) for v in fired] == [("settings", "warn")]
    assert not has_errors(fired)


# ----- single hub -----------------------------------------------------------------------------


def test_single_hub_fires_on_two_hub_fixture(fleet: sqlite3.Connection) -> None:
    """if a fleet with two is_hub=1 machines passes then broken"""
    fleet.execute("UPDATE machines SET is_hub = 1 WHERE machine_id = ?", (SPOKE,))
    fired = _fired(fleet, _proposal(), "single_hub")
    assert [(v.subject, v.severity) for v in fired] == [(f"{HUB},{SPOKE}", "error")]


def test_single_hub_fires_on_zero_hubs(fleet: sqlite3.Connection) -> None:
    """if a fleet with no hub passes then broken"""
    fleet.execute("UPDATE machines SET is_hub = 0")
    assert [v.subject for v in _fired(fleet, _proposal(), "single_hub")] == ["(none)"]


def test_single_hub_passes_with_one_hub(fleet: sqlite3.Connection) -> None:
    """if a fleet with exactly one hub fails then broken"""
    assert _fired(fleet, _proposal(), "single_hub") == []


# ----- cross-machine edit ------------------------------------------------------------------------


def test_cross_machine_edit_warns(fleet: sqlite3.Connection) -> None:
    """if editing another machine's policy is silent then broken"""
    fired = _fired(
        fleet,
        _proposal(author=SPOKE, policies=(PolicyCell(HUB, "audio", "cached", 100),)),
        "cross_machine_edit",
    )
    assert [(v.subject, v.severity) for v in fired] == [(f"{HUB}/audio", "warn")]


def test_own_machine_edit_does_not_warn(fleet: sqlite3.Connection) -> None:
    """if editing your own machine's policy warns then broken"""
    assert (
        _fired(
            fleet,
            _proposal(author=SPOKE, policies=(PolicyCell(SPOKE, "audio", "cached", 100),)),
            "cross_machine_edit",
        )
        == []
    )


# ----- vocabulary ---------------------------------------------------------------------------------


def test_unknown_asset_kind_is_an_error(fleet: sqlite3.Connection) -> None:
    """if a kind outside the sync_policies CHECK passes then broken"""
    fired = _fired(
        fleet,
        _proposal(
            policies=(PolicyCell(SPOKE, "video", "pinned", None),),
            defaults=(KindDefault("midi", "stream"),),
        ),
        "unknown_asset_kind",
    )
    assert {v.subject for v in fired} == {f"{SPOKE}/video", "default/midi"}


def test_known_asset_kind_passes(fleet: sqlite3.Connection) -> None:
    """if a CHECK-listed kind is reported unknown then broken"""
    assert (
        _fired(
            fleet,
            _proposal(policies=(PolicyCell(SPOKE, "karaoke_words", "pinned", None),)),
            "unknown_asset_kind",
        )
        == []
    )


def test_unknown_machine_is_an_error(fleet: sqlite3.Connection) -> None:
    """if a policy or pin for an unregistered machine passes then broken"""
    fired = _fired(
        fleet,
        _proposal(
            policies=(PolicyCell("m-ghost", "audio", "pinned", None),),
            pins=(PinCell("m-ghost", "pl-live", "pinned"),),
        ),
        "unknown_machine",
    )
    assert {v.subject for v in fired} == {"m-ghost/audio", "m-ghost/pin/pl-live"}


def test_registered_machine_passes(fleet: sqlite3.Connection) -> None:
    """if a policy for a registered machine is reported unknown then broken"""
    assert (
        _fired(
            fleet,
            _proposal(policies=(PolicyCell(SPOKE, "audio", "pinned", None),)),
            "unknown_machine",
        )
        == []
    )


def test_mode_outside_allowed_modes_is_an_error(fleet: sqlite3.Connection) -> None:
    """if a mode outside allowed_modes passes, for a cell or a pin, then broken"""
    fired = _fired(
        fleet,
        _proposal(
            policies=(PolicyCell(SPOKE, "audio", "mirror", None),),
            pins=(PinCell(SPOKE, "pl-live", "always"),),
        ),
        "mode_not_allowed",
    )
    assert {v.subject for v in fired} == {f"{SPOKE}/audio", f"{SPOKE}/pin/pl-live"}


def test_allowed_mode_passes(fleet: sqlite3.Connection) -> None:
    """if an allowed mode is rejected then broken"""
    assert (
        _fired(
            fleet,
            _proposal(policies=(PolicyCell(SPOKE, "audio", "excluded", None),)),
            "mode_not_allowed",
        )
        == []
    )
