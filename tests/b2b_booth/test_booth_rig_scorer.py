"""B2BBOOTH-03/05/91/94/96: the rig scorer can say no (controls for the S1b rig's instrument).

- [if] a reordered, lost or duplicated op, or two pages that disagree on order, scores ok [then] fail
- [if] a clean run does not score ok with every control hop decomposed exactly [then] fail
- [if] a reload that changed the epoch or lost an op scores ok [then] fail
- [if] a control total over one RTT + 50 ms scores ok, or a trace missing a hop is guessed [then] fail
- [if] the scorer cannot tell a broken booth run from a clean one [then] fail, [else stop].
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.b2b_booth.rig.score_booth_rig import score

pytestmark = [pytest.mark.requirement(r) for r in ("B2BBOOTH-03", "B2BBOOTH-05", "B2BBOOTH-91", "B2BBOOTH-94", "B2BBOOTH-96")]
EPOCH = "0123456789abcdef"
T = 1_000_000


def _page(side: str, n: int = 4) -> dict[str, Any]:
    ops = [[i + 1, "joiner" if i % 2 else "inviter", {"page_us": T + i * 1000, "engine_us": T + i * 1000 + 500, "inviter_us": T + i * 1000 + 3500},
            T + i * 1000 + 4000] for i in range(2 * n)]
    # Partner controls: page 1.0 ms -> engine, 6.0 ms engine -> engine, 0.5 ms engine -> page (7.5 total).
    controls = [["strip:1", (i + 1) / n, {"page_us": T + i * 1000, "engine_us": T + i * 1000 + 1000, "peer_us": T + i * 1000 + 7000},
                 T + i * 1000 + 7500] for i in range(n)]
    return {"side": side, "epoch": EPOCH, "ops": ops, "controls": controls, "clock": {"rtt": 200.0},
            "engine": {"transport": "lan:runtime", "peer_clock": {"rtt_us": 10_000}, "counters": {}, "lanes": {}},
            "reload": {"epoch": EPOCH, "committed": 2 * n, "seqs_in_order": True}}


def _run() -> tuple[dict[str, Any], dict[str, Any]]:
    return _page("inviter"), _page("joiner")


def test_clean_run_scores_ok_with_every_hop() -> None:
    inviter, joiner = _run()
    result = score(inviter, joiner, 4)
    assert result["ok"], result
    hops = result["control_joiner_to_inviter"]
    assert (hops["page_to_engine"]["p50_ms"], hops["engine_to_engine"]["p50_ms"], hops["engine_to_page"]["p50_ms"], hops["total"]["p50_ms"]) == (
        1.0, 6.0, 0.5, 7.5)
    assert result["control_bar_ms"] == 60.0 and result["control_within_bar"]
    assert result["joiner_op_to_inviter_page"]["p50_ms"] == 4.0


def test_reorder_loss_duplicate_and_disagreement_each_fail() -> None:
    inviter, joiner = _run()
    joiner["ops"][2], joiner["ops"][3] = joiner["ops"][3], joiner["ops"][2]
    assert not score(inviter, joiner, 4)["ok"], "if a reorder scores ok then the instrument cannot say no"
    inviter, joiner = _run()
    del joiner["ops"][5]
    lost = score(inviter, joiner, 4)
    assert not lost["ok"] and lost["joiner"]["losses"] == 1
    inviter, joiner = _run()
    joiner["ops"].append(copy.deepcopy(joiner["ops"][0]))
    assert not score(inviter, joiner, 4)["ok"]
    inviter, joiner = _run()
    joiner["ops"][1][1] = "inviter"
    assert not score(inviter, joiner, 4)["ok"], "if two pages disagreeing on who proposed seq 2 scores ok then broken"


@pytest.mark.parametrize("broken", [{"epoch": "fedcba9876543210"}, {"committed": 7}, {"seqs_in_order": False}, {"error": "not live"}])
def test_a_bad_reload_fails(broken: dict[str, Any]) -> None:
    inviter, joiner = _run()
    joiner["reload"] = {"error": "not live"} if "error" in broken else {**joiner["reload"], **broken}
    result = score(inviter, joiner, 4)
    assert not result["ok"] and not result["reload_ok"]


def test_a_control_over_the_bar_fails_and_an_incomplete_trace_is_not_guessed() -> None:
    inviter, joiner = _run()
    for row in inviter["controls"]:
        row[3] += 60_000  # 67.5 ms total against a 60 ms bar
    slow = score(inviter, joiner, 4)
    assert not slow["ok"] and not slow["control_within_bar"]
    inviter, joiner = _run()
    inviter["controls"][0][2]["peer_us"] = None
    partial = score(inviter, joiner, 4)
    assert partial["control_joiner_to_inviter"]["untraced"] == 1
    assert partial["control_joiner_to_inviter"]["total"]["n"] == 3


@pytest.mark.requirement("B2BBOOTH-97")
def test_the_expected_ctl_lane_gates_and_a_fallback_run_reports_latency_without_gating_on_it() -> None:
    inviter, joiner = _run()
    for page in (inviter, joiner):
        page["engine"]["lanes"] = {"ctl_active": "datagram"}
    assert score(inviter, joiner, 4, "datagram")["ok"]
    wrong = score(inviter, joiner, 4, "door")
    assert not wrong["ok"] and not wrong["ctl_lane_ok"], "if a run on the wrong CTL lane scores ok then broken"
    joiner["engine"]["lanes"] = {"ctl_active": "door"}
    assert not score(inviter, joiner, 4, "datagram")["ok"], "if one engine on the door passes a datagram run then broken"
    for page in (inviter, joiner):
        page["engine"]["lanes"] = {"ctl_active": "door"}
    for row in inviter["controls"]:
        row[3] += 60_000  # over the bar on the fallback lane
    fallback = score(inviter, joiner, 4, "door", latency_gates=False)
    assert fallback["ok"] and fallback["correct"] and not fallback["control_within_bar"] and not fallback["control_bar_gates"]
    assert not score(inviter, joiner, 4, "door")["ok"], "overshoot control: latency still gates unless told otherwise"
    del joiner["ops"][5]
    assert not score(inviter, joiner, 4, "door", latency_gates=False)["ok"], "a fallback run must still be correct"


def test_a_frozen_page_is_attributed_but_never_changes_the_verdict() -> None:
    inviter, joiner = _run()
    for row in joiner["ops"]:
        if row[1] == "joiner" and row[0] >= 4:
            row[2]["page_us"] += 300_000  # the joiner page sent nothing for 300 ms
    for row in inviter["controls"][1:]:
        row[2]["page_us"] += 300_000
        row[3] += 300_000 + 120_000  # and its controls landed late across the freeze
    result = score(inviter, joiner, 4)
    assert result["page_freezes"]["joiner"] == [302] and result["page_freezes"]["inviter"] == []
    assert not result["ok"] and not result["control_within_bar"], "the raw totals still gate"
    assert result["total_outside_page_freezes"]["joiner_to_inviter"]["n"] < result["control_joiner_to_inviter"]["total"]["n"]
    clean = score(*_run(), 4)
    assert clean["page_freezes"] == {"inviter": [], "joiner": []}, "negative control: a steady page shows no freeze"
