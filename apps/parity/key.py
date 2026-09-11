"""Key lane: djmdKey.ScaleName vs own camelot/openkey.

Reuses `apps.analysis_bench.scorers.key_lane.weighted_score` so PARITY-01
does not grow a second key metric. Exact match is MIREX 1.0; a relative
pair is 0.3 and is reported as related disagreement, not agreement.
"""

from __future__ import annotations

from typing import Any

from apps.analysis_bench.scorers.key_lane import weighted_score
from apps.analysis_key import canon
from apps.parity.figure import LaneFigure
from apps.parity.lanes import DENOMINATOR_NAME

_SENTINEL_SCALE_NAMES = frozenset({"All", "all", ""})


def _parse_rb_key(scale_name: str | None) -> canon.Key | None:
    if scale_name is None or scale_name in _SENTINEL_SCALE_NAMES:
        return None
    try:
        return canon.from_rekordbox_scale_name(scale_name)
    except ValueError:
        return None


def _parse_own_key(row: dict[str, Any]) -> tuple[str, canon.Key | None]:
    camelot, open_key = row.get("own_key_camelot"), row.get("own_key_openkey")
    if not camelot and not open_key:
        return "no_own", None
    try:
        parsed_camelot = canon.from_mik_camelot(camelot) if camelot else None
        parsed_open = canon.from_mik_open_key(open_key) if open_key else None
    except ValueError:
        return "failed_own", None
    if parsed_camelot and parsed_open and parsed_camelot != parsed_open:
        return "failed_own", None
    key = parsed_camelot or parsed_open
    if key is None:
        return "no_own", None
    return "ok", key


def score_key(rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score present rows that carry a parseable rekordbox ScaleName."""
    ungradable_ids: list[str] = []
    no_own_ids: list[str] = []
    failed_ids: list[str] = []
    agree_ids: list[str] = []
    disagree_ids: list[str] = []
    related_ids: list[str] = []
    mirex: list[float] = []
    for row in rows:
        sid = row["stable_id"]
        rb_key = _parse_rb_key(row.get("rb_key_scale_name"))
        if rb_key is None:
            ungradable_ids.append(sid)
            continue
        kind, own_key = _parse_own_key(row)
        if kind == "no_own":
            no_own_ids.append(sid)
            continue
        if kind == "failed_own":
            failed_ids.append(sid)
            continue
        score = weighted_score(rb_key, own_key)
        mirex.append(score)
        if score == 1.0:
            agree_ids.append(sid)
        else:
            disagree_ids.append(sid)
            if score > 0.0:
                related_ids.append(sid)
    denominator_n = len(rows) - len(ungradable_ids)
    scored_n = len(agree_ids) + len(disagree_ids)
    mirex_mean = (sum(mirex) / len(mirex)) if mirex else None
    return LaneFigure(
        lane="key",
        status="scored",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME["key"],
        denominator_n=denominator_n,
        scored_n=scored_n,
        exact_n=len(agree_ids),
        related_n=len(related_ids),
        mirex_mean=mirex_mean,
        failed_own_n=len(failed_ids),
        no_own_n=len(no_own_ids),
        agree_ids=tuple(agree_ids),
        disagree_ids=tuple(disagree_ids),
        no_own_ids=tuple(no_own_ids),
        ungradable_ids=tuple(ungradable_ids),
        ungradable={"missing_rb_key": len(ungradable_ids)},
    )
