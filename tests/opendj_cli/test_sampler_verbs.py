"""SAMPLER agent parity: every edge sampler and classic unit action has a CLI verb with the exact wire payload.

[if] a sampler verb builds a payload other than the one the bus guard accepts [then] the MCP
     ``command`` tool cannot reach that UI action.
[if] a slot verb accepts both ref and slot_id, or neither [then] the address is guessed.
[if] an out-of-domain ref, edge or page step parses [then] the CLI sends a payload the bus refuses.
[if] a settings patch outside the sampler-types enums and ranges parses [then] the bus refuses it late.
[if] a slot update names nothing to change [then] the CLI sends a payload the bus refuses.
[if] a classic unit verb sends a bank, slot, mode or gain outside the unit [then] the bus refuses it late.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.confirm import checks_for, verdict
from apps.opendj_cli.verbs import InvocationError, parse_invocation
from tests.opendj_cli import ts_contract

_CASES: list[tuple[list[str], dict[str, object]]] = [
    (["sampler_enabled", "true"], {"type": "sampler_enabled", "on": True}),
    (["sampler_enabled", "off"], {"type": "sampler_enabled", "on": False}),
    (["sampler_trigger", "S1-05"], {"type": "sampler_trigger", "ref": "S1-05"}),
    (["sampler_trigger", "slot_id=abc"], {"type": "sampler_trigger", "slot_id": "abc"}),
    (["sampler_stop", "R-03"], {"type": "sampler_stop", "ref": "R-03"}),
    (["sampler_stop_all"], {"type": "sampler_stop", "all": True}),
    (["sampler_release", "S1-05"], {"type": "sampler_release", "ref": "S1-05"}),
    (["sampler_release", "slot_id=abc"], {"type": "sampler_release", "slot_id": "abc"}),
    (["sampler_preview", "S2-01"], {"type": "sampler_preview", "ref": "S2-01"}),
    (["sampler_preview_stop"], {"type": "sampler_preview", "stop": True}),
    (["sampler_set_page", "L", "3"], {"type": "sampler_set_page", "edge": "L", "set": 3}),
    (["sampler_set_page", "R", "delta=-1"], {"type": "sampler_set_page", "edge": "R", "delta": -1}),
    (["sampler_pin", "S1-02", "true"], {"type": "sampler_pin", "ref": "S1-02", "pinned": True}),
    (["sampler_pin", "slot_id=x1", "pinned=false"], {"type": "sampler_pin", "slot_id": "x1", "pinned": False}),
    (["sampler_search", "L", "vocal chop"], {"type": "sampler_search", "edge": "L", "query": "vocal chop"}),
    (["sampler_search", "R", ""], {"type": "sampler_search", "edge": "R", "query": ""}),
    (["sampler_filter", "R", "vocals", "on"], {"type": "sampler_filter", "edge": "R", "filter": "vocals", "on": True}),
    (["sampler_results", "L", "true"], {"type": "sampler_results", "edge": "L", "show": True}),
    (
        ["sampler_slot_update", "S1-01", "name=Horn", "settings=launch:beat,repeat:4,level:80,downbeat_offset:-2"],
        {
            "type": "sampler_slot_update",
            "ref": "S1-01",
            "name": "Horn",
            "settings": {"launch": "beat", "repeat": 4, "level": 80, "downbeat_offset": -2},
        },
    ),
    (
        ["sampler_slot_update", "slot_id=x1", "description="],
        {"type": "sampler_slot_update", "slot_id": "x1", "description": ""},
    ),
    (
        ["sampler_set_name", "set-1", "Warmup", ""],
        {"type": "sampler_set_name", "set_id": "set-1", "name": "Warmup", "description": ""},
    ),
    (["sampler_capture", "2", "0", "7"], {"type": "sampler_capture", "deck": 2, "set": 0, "index": 7}),
    (["sampler_edit", "S1-05"], {"type": "sampler_edit", "ref": "S1-05"}),
    (["sampler_edit_close"], {"type": "sampler_edit", "close": True}),
    (["sampler_delete", "slot_id=cap-1"], {"type": "sampler_delete", "slot_id": "cap-1"}),
    (
        ["sampler_unit_load", "0", "3", "stable_id=t1", "title=Horn"],
        {"type": "sampler_unit_load", "bank": 0, "slot": 3, "stable_id": "t1", "title": "Horn"},
    ),
    (
        ["sampler_unit_load", "1", "0", "path=/a/b.wav", "duration_ms=750"],
        {"type": "sampler_unit_load", "bank": 1, "slot": 0, "path": "/a/b.wav", "duration_ms": 750.0},
    ),
    (["sampler_unit_load", "3", "15"], {"type": "sampler_unit_load", "bank": 3, "slot": 15}),
    (["sampler_unit_trigger", "2", "7"], {"type": "sampler_unit_trigger", "bank": 2, "slot": 7}),
    (["sampler_unit_release", "2", "7"], {"type": "sampler_unit_trigger", "held": False, "bank": 2, "slot": 7}),
    (["sampler_unit_stop", "0", "1"], {"type": "sampler_unit_stop", "bank": 0, "slot": 1}),
    (["sampler_unit_stop_all"], {"type": "sampler_unit_stop"}),
    (["sampler_unit_mode", "0", "1", "gate"], {"type": "sampler_unit_mode", "bank": 0, "slot": 1, "mode": "gate"}),
    (["sampler_unit_gain", "0", "1", "-3.5"], {"type": "sampler_unit_gain", "bank": 0, "slot": 1, "gain_db": -3.5}),
    (["sampler_unit_master_gain", "-60"], {"type": "sampler_unit_master_gain", "gain_db": -60.0}),
]


@pytest.mark.parametrize(("tokens", "expected"), _CASES, ids=[" ".join(case[0]) for case in _CASES])
def test_sampler_verb_builds_the_exact_wire_payload(tokens: list[str], expected: dict[str, object]) -> None:
    """[if] the built command differs from the bus payload [then] the UI action is unreachable, [else stop]."""
    assert parse_invocation(tokens).command == expected


@pytest.mark.parametrize(
    "tokens",
    [
        ["sampler_trigger"],
        ["sampler_trigger", "S1-05", "slot_id=abc"],
        ["sampler_set_page", "L"],
        ["sampler_set_page", "L", "2", "delta=1"],
        ["sampler_pin", "S1-05", "true", "slot_id=abc"],
        ["sampler_pin", "pinned=true"],
    ],
)
def test_exactly_one_address_is_enforced(tokens: list[str]) -> None:
    """[if] both or neither of ref/slot_id (or set/delta) parse [then] the address is guessed, [else stop]."""
    with pytest.raises(InvocationError, match="exactly one"):
        parse_invocation(tokens)


@pytest.mark.parametrize(
    "tokens",
    [
        ["sampler_pin", "S0-05", "true"],
        ["sampler_pin", "S1-00", "true"],
        ["sampler_pin", "S1-5", "true"],
        ["sampler_pin", "abc", "true"],
        ["sampler_pin", "S1-05", "maybe"],
        ["sampler_enabled", "maybe"],
        ["sampler_trigger", "S1-5"],
        ["sampler_set_page", "X", "1"],
        ["sampler_set_page", "L", "-1"],
        ["sampler_set_page", "L", "delta=0"],
        ["sampler_filter", "L", "loud", "true"],
        ["sampler_capture", "5", "0", "0"],
        ["sampler_capture", "1", "0", "-1"],
        ["sampler_slot_update", "S1-01"],
        ["sampler_slot_update", "S1-01", "settings=level:101"],
        ["sampler_slot_update", "S1-01", "settings=repeat:3"],
        ["sampler_slot_update", "S1-01", "settings=downbeat_offset:4"],
        ["sampler_slot_update", "S1-01", "settings=launch:never"],
        ["sampler_slot_update", "S1-01", "settings=volume:1"],
        ["sampler_slot_update", "S1-01", "settings=level:80,level:70"],
        ["sampler_slot_update", "S1-01", "settings="],
        ["sampler_set_name", "set-1", "", ""],
        ["sampler_delete"],
        ["sampler_unit_load", "0", "0", "stable_id=t1", "path=/a.wav"],
        ["sampler_unit_load", "0", "0", "path=/a.wav", "duration_ms=0"],
        ["sampler_unit_trigger", "4", "0"],
        ["sampler_unit_trigger", "0", "16"],
        ["sampler_unit_trigger", "-1", "0"],
        ["sampler_unit_stop", "0"],
        ["sampler_unit_mode", "0", "0", "reverse"],
        ["sampler_unit_gain", "0", "0", "6.5"],
        ["sampler_unit_gain", "0", "0", "-25"],
        ["sampler_unit_master_gain", "1"],
        ["sampler_unit_master_gain", "-61"],
    ],
)
def test_out_of_domain_values_are_refused_locally(tokens: list[str]) -> None:
    """[if] an out-of-domain value parses [then] the CLI ships a payload the guard rejects, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(tokens)


def test_sampler_enabled_is_confirmed_against_the_published_mirror() -> None:
    """[if] ui-mirror stops publishing sampler or the verb stops observing it [then] the CLI cannot confirm, [else stop]."""
    assert "sampler" in ts_contract.mirror_top_level_keys()
    checks = checks_for(VERBS["sampler_enabled"], parse_invocation(["sampler_enabled", "false"]).command)
    assert [check.path for check in checks] == [("sampler", "enabled")]
    assert verdict(checks, {"sampler": {"enabled": False}})[0] == "confirmed"
    assert verdict(checks, {"sampler": {"enabled": True}})[0] == "unconfirmed"
