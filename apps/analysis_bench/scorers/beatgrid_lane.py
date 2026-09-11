"""Beatgrid lane adapter: the existing scorer 1.1.0, behind the shared interface.

NOTHING IS RESCORED HERE. Every number still comes from
`apps.analysis_bench.scorers.beatgrid` (SCORER_VERSION 1.1.0) through
`beatgrid_report`, which is the same code path rounds 0 and 1 used and the same
code `tests/beatbench` pins. This module only translates: it takes the shared
`{arm name: candidate payload}` shape the harness produces and hands back the
report and markdown the round log wants. Reimplementing the aggregation to fit
a new interface would have made a round-over-round delta unattributable.

WHY THE RENDERER IS STILL UNDER `scripts/`. The versioned RULER (scorer.py,
continuity.py) moved into this package because four lanes share it. The beatgrid
REPORT is presentation for one lane, lives beside the beatgrid runners that
produced rounds 0 and 1, and carries three functions radon already scores over
the complexity limit. Moving it would have added those three blocks to the
gated `apps` tree without changing a line of them -- a ratchet regression bought
for nothing. It is imported here rather than reimplemented.

THE FIXED/DYNAMIC SPLIT IS KEPT. A rekordbox grid is either one stored tempo or
a tempo curve, and the two are different problems: round 1 measured beat_this at
F 0.876 on fixed grids against a 0.280 do-nothing floor, while on dynamic grids
aubio cleared its floor by 0.033. Collapsing them into one mean would hide that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.analysis_bench.scorers.beatgrid import SCORER_VERSION
from scripts.beatbench import report as beatgrid_report

__all__ = ["SCORER_VERSION", "render_table", "score_bundle"]

# Controls first: a reader should meet the floor and the ceiling before the row
# whose number they give meaning to.
_ROLE_ORDER = {"negative_control": 0, "positive_control": 1}


# What a fixture the candidate never answered for is scored as. Not a dropped
# row: an arm that omits its hard tracks and keeps its easy ones would otherwise
# be aggregated over a smaller, friendlier denominator while the report went on
# advertising the bundle's full fixture count (Codex P1 BLOCKING, PR #1582).
_OMITTED = "candidate emitted no result for this fixture"


def _cells(fixtures: dict[str, Any], payload: dict[str, Any]) -> tuple[dict, dict, int]:
    """Aggregate one arm into (fixed cell, dynamic cell, count omitted).

    The loop walks the BUNDLE's fixtures, never the candidate's answers, so the
    denominator is the question rather than whatever the arm chose to answer.
    """
    results = payload.get("results") or {}
    unknown = sorted(set(results) - set(fixtures))
    if unknown:
        raise ValueError(
            f"this arm answered {len(unknown)} fixture(s) the bundle does not contain, "
            f"first {unknown[0]!r}. It was run against a different fixture set; "
            "scoring it here would attribute one bundle's numbers to another."
        )

    fixed: list[dict] = []
    dynamic: list[dict] = []
    omitted = 0
    for stable_id, fixture in fixtures.items():
        result = results.get(stable_id)
        if result is None:
            omitted += 1
            result = {"beats": [], "downbeats": None, "native_bpm": None, "error": _OMITTED}
        row = beatgrid_report.score_track(fixture, result)
        if row is None:
            continue
        (dynamic if fixture["is_dynamic"] else fixed).append(row)
    return beatgrid_report.aggregate(fixed), beatgrid_report.aggregate(dynamic), omitted


def score_bundle(bundle: Path, arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score every arm of a round against one bundle's rekordbox truth."""
    manifest_path = str(Path(bundle) / "manifest.json")
    manifest, fixtures = beatgrid_report.load_fixtures(manifest_path)
    scored: dict[str, Any] = {}
    for name, arm in arms.items():
        fixed, dynamic, omitted = _cells(fixtures, arm["payload"])
        scored[name] = {
            "role": arm["role"],
            "note": arm.get("note", ""),
            "fixed": fixed,
            "dynamic": dynamic,
            "emits_downbeats": bool(arm["payload"].get("emits_downbeats")),
            "n_failed": arm["payload"].get("n_failed"),
            "n_omitted": omitted,
            "realtime_factor": arm["payload"].get("realtime_factor"),
        }
    return {
        "lane": "beatgrid",
        "scorer_version": SCORER_VERSION,
        "n_fixtures": len(fixtures),
        "truth": (manifest.get("reads") or {}).get("truth", "inline ref_beats"),
        "arms": scored,
    }


def render_table(report: dict[str, Any]) -> str:
    """The round's markdown: one table per grid class, controls first."""
    order = sorted(
        report["arms"].items(),
        key=lambda item: (_ROLE_ORDER.get(item[1]["role"], 2), item[0]),
    )
    lines: list[str] = []
    for split in ("fixed", "dynamic"):
        cells = [
            (name, arm[split], {"emits_downbeats": arm["emits_downbeats"]}) for name, arm in order
        ]
        rendered = beatgrid_report.render_table(f"{split} grids", cells)
        # The shared renderer heads its table with an H3, which inside a round
        # block would nest under the round's own H3 and land in the spec's
        # table of contents as if it were a section.
        rendered[0] = f"**{split} grids**"
        lines.extend(rendered)
    return "\n".join(lines).rstrip()
