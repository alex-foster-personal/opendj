"""Parse and validate FB-03 ``vocal-area:`` anchors against the vocals read model."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException

_VOCAL_STATUSES: frozenset[str] = frozenset({"rekordbox", "no_vocals", "demucs", "not_analyzed"})
_REGION_ANCHOR = re.compile(
    r"^vocal-area:([^:@]+):(\d+\.\d{2})-(\d+\.\d{2}):(rekordbox|no_vocals|demucs|not_analyzed)$"
)
_AT_ANCHOR = re.compile(r"^vocal-area:([^:@]+)@(\d+\.\d{2}):(rekordbox|no_vocals|demucs|not_analyzed)$")


@dataclass(frozen=True, slots=True)
class ParsedVocalAnchor:
    stable_id: str
    status: Literal["rekordbox", "no_vocals", "demucs", "not_analyzed"]
    kind: Literal["region", "at"]
    start_s: float | None = None
    end_s: float | None = None
    time_s: float | None = None


def _refuse(code: str, message: str, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _format_seconds(value: float) -> str:
    return f"{value:.2f}"


def _encode_region_anchor(
    stable_id: str,
    start_s: float,
    end_s: float,
    status: str,
) -> str:
    return f"vocal-area:{stable_id}:{_format_seconds(start_s)}-{_format_seconds(end_s)}:{status}"


def _encode_at_anchor(stable_id: str, time_s: float, status: str) -> str:
    return f"vocal-area:{stable_id}@{_format_seconds(time_s)}:{status}"


def parse_vocal_anchor(anchor: str) -> ParsedVocalAnchor | None:
    region = _REGION_ANCHOR.match(anchor)
    if region is not None and region.group(4) in _VOCAL_STATUSES:
        return ParsedVocalAnchor(
            stable_id=region.group(1),
            status=region.group(4),  # type: ignore[arg-type]
            kind="region",
            start_s=float(region.group(2)),
            end_s=float(region.group(3)),
        )
    at = _AT_ANCHOR.match(anchor)
    if at is not None and at.group(3) in _VOCAL_STATUSES:
        return ParsedVocalAnchor(
            stable_id=at.group(1),
            status=at.group(3),  # type: ignore[arg-type]
            kind="at",
            time_s=float(at.group(2)),
        )
    return None


def _region_at_time(
    regions: list[dict[str, Any]],
    time_s: float,
) -> dict[str, Any] | None:
    for region in regions:
        start_s = float(region["start_s"])
        end_s = float(region["end_s"])
        if start_s <= time_s < end_s:
            return region
    return None


def _region_bounds_match(
    regions: list[dict[str, Any]],
    start_s: float,
    end_s: float,
) -> dict[str, Any] | None:
    want_start = round(start_s, 2)
    want_end = round(end_s, 2)
    for region in regions:
        if round(float(region["start_s"]), 2) != want_start:
            continue
        if round(float(region["end_s"]), 2) != want_end:
            continue
        return region
    return None


def _assert_parsed_anchor(path_stable_id: str, anchor: str) -> ParsedVocalAnchor:
    parsed = parse_vocal_anchor(anchor)
    if parsed is None:
        raise _refuse("vocal_anchor_invalid", f"anchor is not a vocal-area string: {anchor!r}")
    if parsed.stable_id != path_stable_id:
        raise _refuse(
            "vocal_stable_id_mismatch",
            f"anchor stable id {parsed.stable_id!r} does not match path {path_stable_id!r}",
        )
    if parsed.kind == "region":
        assert parsed.start_s is not None and parsed.end_s is not None
        if not math.isfinite(parsed.start_s) or not math.isfinite(parsed.end_s):
            raise _refuse("vocal_bounds_invalid", "region bounds must be finite")
        if parsed.start_s >= parsed.end_s:
            raise _refuse(
                "vocal_bounds_invalid",
                f"region start_s must be less than end_s ({parsed.start_s} >= {parsed.end_s})",
            )
    return parsed


def _assert_live_vocals(parsed: ParsedVocalAnchor, vocals: dict[str, Any]) -> str:
    live_status = vocals["status"]
    if parsed.status != live_status:
        raise _refuse(
            "vocal_status_stale",
            f"anchor status {parsed.status!r} does not match live {live_status!r}",
        )
    if live_status == "not_analyzed":
        raise _refuse("vocal_not_analyzed", "track vocals are not analyzed")
    if live_status == "no_vocals":
        raise _refuse("vocal_area_unavailable", "track has no vocal regions")
    return live_status


def _success_payload(
    path_stable_id: str,
    matched: dict[str, Any],
    live_status: str,
    *,
    time_s: float | None,
) -> dict[str, Any]:
    canonical = _encode_region_anchor(
        path_stable_id,
        float(matched["start_s"]),
        float(matched["end_s"]),
        live_status,
    )
    return {
        "stable_id": path_stable_id,
        "anchor": canonical,
        "kind": "region",
        "status": live_status,
        "start_s": float(matched["start_s"]),
        "end_s": float(matched["end_s"]),
        "intensity": int(matched.get("intensity", 0)),
        "time_s": time_s,
    }


def validate_vocal_anchor_for_vocals(
    path_stable_id: str,
    anchor: str,
    vocals: dict[str, Any],
) -> dict[str, Any]:
    """Return a success payload or raise ``HTTPException`` with a named ``code``."""
    parsed = _assert_parsed_anchor(path_stable_id, anchor)
    live_status = _assert_live_vocals(parsed, vocals)
    regions: list[dict[str, Any]] = list(vocals.get("regions") or [])

    if parsed.kind == "region":
        assert parsed.start_s is not None and parsed.end_s is not None
        matched = _region_bounds_match(regions, parsed.start_s, parsed.end_s)
        if matched is None:
            raise _refuse(
                "vocal_area_unknown",
                f"no vocal region matches {parsed.start_s:.2f}-{parsed.end_s:.2f}s",
            )
        return _success_payload(path_stable_id, matched, live_status, time_s=None)

    assert parsed.time_s is not None
    if not math.isfinite(parsed.time_s):
        raise _refuse("vocal_anchor_invalid", "at-time must be finite")
    matched = _region_at_time(regions, parsed.time_s)
    if matched is None:
        raise _refuse(
            "vocal_area_unknown",
            f"no vocal region contains {parsed.time_s:.2f}s",
        )
    return _success_payload(path_stable_id, matched, live_status, time_s=parsed.time_s)
