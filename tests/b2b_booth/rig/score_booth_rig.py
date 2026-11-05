"""B2BBOOTH rig scorer (S1b): order, loss, the reload, and CONTROL LATENCY decomposed per hop.

Each page reports ``ops`` (``[seq, by, trace, received_us]`` in the order its store delivered them)
and ``controls`` (the PARTNER's fader values: ``[unit, value, trace, received_us]``). Every time is
on the receiving laptop's Link clock (the engine converted the partner's stamps; the page's clock
is its engine's through a least-RTT ping). Hops of a control from page X to page Y:

- ``page_to_engine``: X's engine received the intent (``engine_us``) minus X's page sent it (``page_us``);
- ``engine_to_engine``: Y's engine received it (``peer_us``) minus X's engine received it;
- ``engine_to_page``: Y's page received the push minus Y's engine received it;
- ``total``: Y's page received it minus X's page sent it.

    uv run --no-sync python -m tests.b2b_booth.rig.score_booth_rig <inviter.json> <joiner.json> <ops-per-side> [datagram|door [latency-informational]]

B2BBOOTH-97: the optional fourth argument is the CTL lane both engines must report active
(``lanes.ctl_active`` in ``GET /booth``). ``latency-informational`` (the rig's ``--block-udp`` run)
REPORTS the control latency bar without gating on it: the door lane under loss is the measured gap
the datagram lane exists to close; that run must still be correct and on the fallback lane.
"""

from __future__ import annotations

import json
import sys
from itertools import pairwise
from pathlib import Path
from statistics import quantiles
from typing import Any

HOPS = ("page_to_engine", "engine_to_engine", "engine_to_page", "total")
#: Contract v3 "Metrics": control latency p95 <= one RTT + 50 ms (here up to the partner page's
#: receipt; page scheduling and output latency are the S4 instruments' half).
BAR_SLACK_MS = 50.0
#: Contract v3 "Metrics": the control laptop's shown playhead, p95 within 10 ms of the audio laptop's.
DISPLAY_DRIFT_BAR_MS = 10.0
#: A positive-control run must read its injected display bias back within this.
BIAS_READBACK_TOLERANCE_MS = 1.0


def _pct(values: list[float]) -> dict[str, float | int | None]:
    if len(values) < 2:
        return {"n": len(values), "p50_ms": None, "p95_ms": None, "max_ms": None}
    cuts = quantiles(values, n=100, method="inclusive")
    return {"n": len(values), "p50_ms": round(cuts[49], 2), "p95_ms": round(cuts[94], 2), "max_ms": round(max(values), 2)}


def _order(ops: list[list[Any]], total: int) -> dict[str, int]:
    seqs = [row[0] for row in ops]
    return {
        "applied": len(seqs),
        "reorders": sum(1 for i, seq in enumerate(seqs) if seq != i + 1),
        "losses": total - len(set(seqs)),
        "duplicates": len(seqs) - len(set(seqs)),
    }


def _hops(rows: list[list[Any]]) -> dict[str, list[float]]:
    """Per-hop latencies (ms) of every row carrying a complete trace; incomplete traces are counted, not guessed."""
    out: dict[str, list[float]] = {hop: [] for hop in HOPS}
    for row in rows:
        trace, received = row[2], row[3]
        if not trace or received is None or any(trace.get(k) is None for k in ("page_us", "engine_us", "peer_us")):
            continue
        out["page_to_engine"].append((trace["engine_us"] - trace["page_us"]) / 1000)
        out["engine_to_engine"].append((trace["peer_us"] - trace["engine_us"]) / 1000)
        out["engine_to_page"].append((received - trace["peer_us"]) / 1000)
        out["total"].append((received - trace["page_us"]) / 1000)
    return out


def _control_report(rows: list[list[Any]]) -> dict[str, Any]:
    hops = _hops(rows)
    report: dict[str, Any] = {hop: _pct(values) for hop, values in hops.items()}
    report["untraced"] = len(rows) - len(hops["total"])
    return report


#: A page that sent none of its own intents (one every ``--every-ms``) for this long was frozen
#: (headless Chromium starved on a loaded host): latency samples inside it are attributed, not hidden.
FREEZE_MS = 100.0


def _freezes(page: dict[str, Any], side: str) -> list[tuple[int, int]]:
    """Windows (Link us) in which ``side``'s page sent none of its own ops: the page itself was frozen."""
    sent = sorted(r[2]["page_us"] for r in page["ops"] if r[1] == side and r[2] and r[2].get("page_us") is not None)
    return [(a, b) for a, b in pairwise(sent) if (b - a) / 1000 > FREEZE_MS]


def _outside(rows: list[list[Any]], windows: list[tuple[int, int]]) -> list[list[Any]]:
    """Rows whose send-to-receive span overlaps no freeze window."""
    def clear(row: list[Any]) -> bool:
        trace, received = row[2], row[3]
        if not trace or trace.get("page_us") is None or received is None:
            return False
        return not any(trace["page_us"] < b and received > a for a, b in windows)
    return [r for r in rows if clear(r)]


def score(inviter: dict[str, Any], joiner: dict[str, Any], ops_per_side: int, expect_lane: str | None = None,
          latency_gates: bool = True) -> dict[str, Any]:
    """The verdict and its numbers; ``ok`` only when every measured property holds."""
    total = 2 * ops_per_side
    a, b = inviter["ops"], joiner["ops"]
    joiner_ops_at_inviter = [r for r in a if r[1] == "joiner" and r[2]]
    rtt_us = (joiner.get("engine", {}).get("peer_clock") or {}).get("rtt_us")
    out: dict[str, Any] = {
        "total_ops": total,
        "inviter": _order(a, total),
        "joiner": _order(b, total),
        "identical_order": [row[:2] for row in a] == [row[:2] for row in b],
        "by_side": {"inviter": sum(1 for r in a if r[1] == "inviter"), "joiner": sum(1 for r in a if r[1] == "joiner")},
        "control_inviter_to_joiner": _control_report(joiner["controls"]),
        "control_joiner_to_inviter": _control_report(inviter["controls"]),
        # A joiner op: its page -> its engine -> the inviter's engine commits -> the inviter's page.
        "joiner_op_to_inviter_page": _pct([(r[3] - r[2]["page_us"]) / 1000 for r in joiner_ops_at_inviter if r[2].get("page_us") is not None]),
        "joiner_op_back_to_joiner_page": _pct(
            [(r[3] - r[2]["page_us"]) / 1000 for r in b if r[1] == "joiner" and r[2] and r[2].get("page_us") is not None]),
        "door_rtt_ms": None if rtt_us is None else round(rtt_us / 1000, 2),
        "reload": {"inviter": inviter.get("reload"), "joiner": joiner.get("reload")},
        "epochs": {"inviter": inviter.get("epoch"), "joiner": joiner.get("epoch")},
        "transport": {"inviter": inviter.get("engine", {}).get("transport"), "joiner": joiner.get("engine", {}).get("transport")},
        "engine_counters": {"inviter": inviter.get("engine", {}).get("counters"), "joiner": joiner.get("engine", {}).get("counters")},
        "lanes": {"inviter": inviter.get("engine", {}).get("lanes"), "joiner": joiner.get("engine", {}).get("lanes")},
        "clock_sync_rtt_ms": {"inviter": round(inviter["clock"]["rtt"] / 1000, 3), "joiner": round(joiner["clock"]["rtt"] / 1000, 3)},
    }
    reload_ok = all(
        isinstance(r, dict) and r.get("epoch") == out["epochs"]["inviter"] and r.get("committed") == total and r.get("seqs_in_order")
        for r in out["reload"].values()
    )
    out["reload_ok"] = reload_ok
    totals = [out[k]["total"]["p95_ms"] for k in ("control_inviter_to_joiner", "control_joiner_to_inviter")]
    bar = None if out["door_rtt_ms"] is None else round(out["door_rtt_ms"] + BAR_SLACK_MS, 2)
    out["control_bar_ms"] = bar
    out["control_within_bar"] = bar is not None and all(t is not None and t <= bar for t in totals)
    sides_ok = out["by_side"] == {"inviter": ops_per_side, "joiner": ops_per_side}
    freezes = {"inviter": _freezes(inviter, "inviter"), "joiner": _freezes(joiner, "joiner")}
    windows = freezes["inviter"] + freezes["joiner"]
    out["page_freezes"] = {s: [round((b - a) / 1000) for a, b in w] for s, w in freezes.items()}
    out["engine_health"] = {"inviter": inviter.get("engine", {}).get("health"), "joiner": joiner.get("engine", {}).get("health")}
    # Attribution only, never the verdict: the same control totals with samples spanning a page freeze left out.
    out["total_outside_page_freezes"] = {
        "inviter_to_joiner": _pct(_hops(_outside(joiner["controls"], windows))["total"]),
        "joiner_to_inviter": _pct(_hops(_outside(inviter["controls"], windows))["total"]),
    }
    out["ctl_lane"] = {s: ((page.get("engine", {}).get("lanes") or {}).get("ctl_active")) for s, page in (("inviter", inviter), ("joiner", joiner))}
    out["ctl_lane_expected"] = expect_lane
    out["ctl_lane_ok"] = expect_lane is None or out["ctl_lane"] == {"inviter": expect_lane, "joiner": expect_lane}
    out["correct"] = bool(
        out["identical_order"]
        and sides_ok
        and out["epochs"]["inviter"] == out["epochs"]["joiner"]
        and all(out[s][k] == 0 for s in ("inviter", "joiner") for k in ("reorders", "losses", "duplicates"))
        and all(t is not None for t in totals)
        and reload_ok
    )
    out["control_bar_gates"] = latency_gates
    out["ok"] = bool(out["correct"] and out["ctl_lane_ok"] and (out["control_within_bar"] or not latency_gates))
    return out


def score_mirror(audio: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    """S2b (``--mirror``): DISPLAY DRIFT, the control page's shown playhead (the real booth-display
    fed by the real feed from the engine stream) minus the audio page's true position, both on the
    rig's one Link clock, at every drawn frame; and the mirror's plan hop (control receipt to the
    planned command). ``unknown`` when nothing could be measured, never a number."""
    a, c = audio.get("mirror"), control.get("mirror")
    if not isinstance(a, dict) or not isinstance(c, dict):
        return {"verdict": "unknown", "reason": "a page ran without the mirror half (--mirror)", "display_drift": _pct([])}
    drift = [abs(float(d)) for d in c.get("drift_ms", [])]
    report = _pct(drift)
    report["bar_ms"] = DISPLAY_DRIFT_BAR_MS
    out: dict[str, Any] = {
        "display_drift": report,
        "signed_drift_ms": _pct([float(d) for d in c.get("drift_ms", [])]),
        "delivery": {"sent": a.get("positions_sent"), "received": c.get("positions_received")},
        "plan_hop": _pct([float(x) for x in c.get("plan_ms", [])]),
    }
    if not drift or not c.get("positions_received"):
        return {**out, "verdict": "unknown", "reason": "no position sample reached the control page's display"}
    bias = float(a.get("bias_ms") or 0.0)
    if bias != 0.0:
        # Positive control: the audio page sent its truth `bias` ms ahead; the instrument must read it.
        read = out["signed_drift_ms"]["p50_ms"]
        ok = read is not None and abs(read - bias) <= BIAS_READBACK_TOLERANCE_MS
        out["bias_readback"] = {"expected_ms": bias, "read_p50_ms": read, "ok": ok}
        out["verdict"] = "ok" if ok else "fail"
        return out
    out["verdict"] = "ok" if report["p95_ms"] is not None and report["p95_ms"] <= DISPLAY_DRIFT_BAR_MS else "fail"
    return out


def main(argv: list[str]) -> int:
    inviter, joiner = (json.loads(Path(p).read_text()) for p in argv[:2])
    for name, page in (("inviter", inviter), ("joiner", joiner)):
        if "error" in page:
            print(json.dumps({"ok": False, "unknown": f"{name}: {page['error']}"}))
            return 3
    result = score(inviter, joiner, int(argv[2]), argv[3] if len(argv) > 3 else None,
                   latency_gates=not (len(argv) > 4 and argv[4] == "latency-informational"))
    if "mirror" in inviter or "mirror" in joiner:
        audio, control = (inviter, joiner) if (inviter.get("mirror") or {}).get("role") == "audio" else (joiner, inviter)
        result["mirror"] = score_mirror(audio, control)
        result["ok"] = result["ok"] and result["mirror"]["verdict"] == "ok"
    print(json.dumps(result, indent=1))
    if "mirror" in result and result["mirror"]["verdict"] == "unknown":
        return 3
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
