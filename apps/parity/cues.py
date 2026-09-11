"""Cues lanes: djmdCue (DB) and ANLZ PCOB/PCO2 vs own_cues."""

from __future__ import annotations

from typing import Any

from apps.audit.cue_comparison import TOLERANCE_MSEC, compare_cue_lists
from apps.parity.figure import LaneFigure
from apps.parity.lanes import DENOMINATOR_NAME
from apps.shared.normalised import NormalisedCue

_FORBIDDEN_PAYLOAD_KEYS = frozenset({"djmdSongHotCueBanklist", "rb_cue_banklist"})


def refuse_forbidden_cue_banklist_keys(payload: dict[str, Any]) -> None:
    """Never read djmdSongHotCueBanklist; refuse if the payload offers it."""
    for key in _FORBIDDEN_PAYLOAD_KEYS:
        if key in payload:
            raise ValueError(
                f"payload contains forbidden key {key!r}; "
                "PARITY-01 reads djmdCue only, never djmdSongHotCueBanklist"
            )
    tracks = payload.get("tracks")
    if isinstance(tracks, list):
        for row in tracks:
            if isinstance(row, dict):
                for key in _FORBIDDEN_PAYLOAD_KEYS:
                    if key in row:
                        raise ValueError(
                            f"track {row.get('stable_id')!r} contains forbidden "
                            f"key {key!r}; PARITY-01 reads djmdCue only"
                        )


def _as_normalised(obj: dict[str, Any]) -> NormalisedCue:
    kind = obj["kind"]
    if kind not in ("memory", "hot", "load", "loop"):
        raise ValueError(f"unsupported cue kind {kind!r}")
    color = obj.get("color_rgb")
    color_rgb: tuple[int, int, int] | None = None
    if color is not None:
        color_rgb = (int(color[0]), int(color[1]), int(color[2]))
    index = obj.get("index")
    return NormalisedCue(
        position_msec=int(obj["in_ms"]),
        kind=kind,
        index=index if index is not None else None,
        color_rgb=color_rgb,
        name=obj.get("name"),
        loop_length_msec=obj.get("loop_length_msec"),
    )


def _normalised_list(cues: list[dict[str, Any]]) -> list[NormalisedCue]:
    return [_as_normalised(item) for item in cues]


def _own_list(row: dict[str, Any]) -> list[NormalisedCue] | None:
    """Return own cues, or None when own analysis is missing (no_own)."""
    if "own_cues" not in row:
        return None
    own = row["own_cues"]
    if own is None:
        return None
    return _normalised_list(own)


def _rb_cues_db_list(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return djmdCue extract, or None when ungradable missing_djmd_cue."""
    if "rb_cues_db" in row:
        val = row["rb_cues_db"]
        if val is None or val is False or val == []:
            return None
        if not isinstance(val, list):
            raise TypeError(f"rb_cues_db must be a list, got {type(val)!r}")
        return val
    if row.get("rb_cue_db") is True:
        raise ValueError(
            f"track {row.get('stable_id')!r} has rb_cue_db=True but no rb_cues_db "
            "list; a fixture must not invent cues"
        )
    return None


def _match(
    rb: list[NormalisedCue], own: list[NormalisedCue]
) -> tuple[list[int], list[int], list[int]]:
    return compare_cue_lists(rb, own, tolerance_msec=TOLERANCE_MSEC)


def _match_anlz(
    rb: list[NormalisedCue], own: list[NormalisedCue]
) -> tuple[list[int], list[int], list[int]]:
    """ANLZ siblings: kind + 20 ms + index; PCO2 name/color is not a conflict."""
    return compare_cue_lists(
        rb, own, tolerance_msec=TOLERANCE_MSEC, check_metadata=False
    )


def _track_agrees(rb_only: list[int], own_only: list[int], conflicts: list[int]) -> bool:
    return not rb_only and not own_only and not conflicts


def _kind_9_11_n(row: dict[str, Any]) -> int:
    val = row.get("kind_9_11_n")
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return 0


def score_cues_db(rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score present rows with at least one djmdCue row in the extract."""
    ungradable_ids: list[str] = []
    no_own_ids: list[str] = []
    agree_ids: list[str] = []
    disagree_ids: list[str] = []
    kind_9_11_total = 0
    total_matched = 0
    total_rb_cues = 0
    total_own_only = 0
    total_conflicts = 0

    for row in rows:
        sid = row["stable_id"]
        rb_raw = _rb_cues_db_list(row)
        if rb_raw is None:
            ungradable_ids.append(sid)
            continue
        kind_9_11_total += _kind_9_11_n(row)
        own = _own_list(row)
        if own is None:
            no_own_ids.append(sid)
            continue
        rb_norm = _normalised_list(rb_raw)
        rb_only, own_only, conflicts = _match(rb_norm, own)
        total_rb_cues += len(rb_norm)
        total_own_only += len(own_only)
        total_conflicts += len(conflicts)
        total_matched += len(rb_norm) - len(rb_only)
        if _track_agrees(rb_only, own_only, conflicts):
            agree_ids.append(sid)
        else:
            disagree_ids.append(sid)

    denominator_n = len(rows) - len(ungradable_ids)
    scored_n = len(agree_ids) + len(disagree_ids)
    details: dict[str, int | float | None] = {
        "matched_cues_n": total_matched,
        "rb_cues_n": total_rb_cues,
        "own_only_cues_n": total_own_only,
        "conflicts_n": total_conflicts,
    }
    if kind_9_11_total:
        details["kind_9_11_n"] = kind_9_11_total

    return LaneFigure(
        lane="cues_db",
        status="scored",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME["cues_db"],
        denominator_n=denominator_n,
        scored_n=scored_n,
        exact_n=len(agree_ids),
        no_own_n=len(no_own_ids),
        agree_ids=tuple(agree_ids),
        disagree_ids=tuple(disagree_ids),
        no_own_ids=tuple(no_own_ids),
        ungradable_ids=tuple(ungradable_ids),
        ungradable={"missing_djmd_cue": len(ungradable_ids)},
        details=details,
    )


def _rb_cues_pcob(row: dict[str, Any]) -> list[dict[str, Any]]:
    val = row.get("rb_cues_pcob")
    if val is None:
        return []
    if not isinstance(val, list):
        raise TypeError(f"rb_cues_pcob must be a list, got {type(val)!r}")
    return val


def _rb_cues_pco2(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return PCO2 list, or None when unreadable_ext drops this sibling."""
    if not row.get("rb_ext_readable", True):
        return None
    val = row.get("rb_cues_pco2")
    if val is None:
        return []
    if not isinstance(val, list):
        raise TypeError(f"rb_cues_pco2 must be a list, got {type(val)!r}")
    return val


def _sibling_agrees(rb_raw: list[dict[str, Any]], own: list[NormalisedCue]) -> bool:
    rb_norm = _normalised_list(rb_raw)
    rb_only, own_only, conflicts = _match_anlz(rb_norm, own)
    return _track_agrees(rb_only, own_only, conflicts)


def score_cues_anlz(rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score ANLZ PCOB and PCO2 siblings against own_cues."""
    ungradable_ext_ids: list[str] = []
    no_own_ids: list[str] = []
    agree_ids: list[str] = []
    disagree_ids: list[str] = []
    pcob_n = 0
    pco2_n = 0
    in_denominator: list[str] = []

    for row in rows:
        sid = row["stable_id"]
        pcob_raw = _rb_cues_pcob(row)
        pco2_raw = _rb_cues_pco2(row)
        has_pcob = True
        has_pco2 = pco2_raw is not None
        if not has_pcob and not has_pco2:
            continue
        in_denominator.append(sid)
        if has_pcob:
            pcob_n += 1
        if has_pco2:
            pco2_n += 1
        else:
            ungradable_ext_ids.append(sid)

        own = _own_list(row)
        if own is None:
            no_own_ids.append(sid)
            continue

        siblings_agree: list[bool] = []
        if has_pcob:
            siblings_agree.append(_sibling_agrees(pcob_raw, own))
        if has_pco2 and pco2_raw is not None:
            siblings_agree.append(_sibling_agrees(pco2_raw, own))

        if siblings_agree and all(siblings_agree):
            agree_ids.append(sid)
        else:
            disagree_ids.append(sid)

    denominator_n = len(in_denominator)
    scored_n = len(agree_ids) + len(disagree_ids)
    return LaneFigure(
        lane="cues_anlz",
        status="scored",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME["cues_anlz"],
        denominator_n=denominator_n,
        scored_n=scored_n,
        exact_n=len(agree_ids),
        no_own_n=len(no_own_ids),
        agree_ids=tuple(agree_ids),
        disagree_ids=tuple(disagree_ids),
        no_own_ids=tuple(no_own_ids),
        ungradable_ids=tuple(ungradable_ext_ids),
        ungradable={"unreadable_ext": len(ungradable_ext_ids)},
        details={"pcob_n": pcob_n, "pco2_n": pco2_n},
    )
