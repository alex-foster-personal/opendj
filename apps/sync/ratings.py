"""Rating diff computation for Phase 2 cautious sync (SYNC-06).

Given a :class:`apps.sync.matcher.MatchResult` we emit a list of
:class:`RatingDiff` records describing the proposed write for each
matched pair. This layer is **pure**: no DB access, no FS mutation.

Direction policy (Phase 2 cautious; Phase 4 will revisit):

======  =======  =========  ===========
   RB    djay      Dir      Action
======  =======  =========  ===========
   0       0      nop       skip
  >0       0      rb->djay  write djay = rb
   0      >0      djay->rb  SKIP (Phase 4)
  >0      >0 eq   nop       skip
  >0      >0 ne   newest    see below
======  =======  =========  ===========

Both rated, values differ (SYNC-12979): last-modified-wins inside
``window_days``. When both tracks expose a modification timestamp
(``modified_at`` or Rekordbox's ``updated_at``) within the window, the
newer side wins (``rb->djay`` or ``djay->rb``). A missing timestamp, a
tie, or timestamps further apart than the window stay ``conflict``.

Rated tracks with no matched pair (``rb_only`` / ``djay_only``) get a
``direction == "unmatched"`` no-write row so the report accounts for
every rated track that is intentionally not written (SYNC-12985).

Only ``direction == "rb->djay"`` records carry a non-None
``new_djay_rating`` and are eligible for a live write.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apps.sync.conflict import DEFAULT_WINDOW_DAYS
from apps.sync.matcher import MatchedPair, MatchResult


@dataclass(slots=True)
class RatingDiff:
    """Proposed (or reported) rating delta for one matched pair."""

    djay_uuid: str
    rb_id: str
    rb_rating: int
    djay_rating: int
    direction: str  # "rb->djay" | "djay->rb" | "conflict" | "nop" | "unmatched"
    new_djay_rating: int | None
    new_rb_rating: int | None
    rationale: str
    rb_modified_at: datetime | None = None
    djay_modified_at: datetime | None = None


def _modified_at(track: Any) -> datetime | None:
    """Return a track's modification timestamp, or None when it exposes none."""
    for attr in ("modified_at", "updated_at"):
        value = getattr(track, attr, None)
        if isinstance(value, datetime):
            return value
    return None


def _resolve_differing(
    rb_rating: int,
    dj_rating: int,
    rb_mtime: datetime | None,
    dj_mtime: datetime | None,
    window_days: int,
) -> tuple[str, int | None, int | None, str]:
    """Apply last-modified-wins inside the window to two differing ratings."""
    base = f"RB={rb_rating} vs djay={dj_rating}"
    if rb_mtime is None or dj_mtime is None:
        missing = "RB" if rb_mtime is None else "djay"
        if rb_mtime is None and dj_mtime is None:
            missing = "both sides"
        return "conflict", None, None, f"{base}; no modification timestamp on {missing}"
    try:
        delta_s = (rb_mtime - dj_mtime).total_seconds()
    except TypeError:
        return "conflict", None, None, f"{base}; modification timestamps not comparable"
    if abs(delta_s) > window_days * 24 * 60 * 60:
        return "conflict", None, None, f"{base}; modifications outside {window_days}-day window"
    if delta_s > 0:
        return "rb->djay", rb_rating, None, f"{base}; RB modified later within window"
    if delta_s < 0:
        return "djay->rb", None, dj_rating, f"{base}; djay modified later within window"
    return "conflict", None, None, f"{base}; modification timestamps tie"


def compute_rating_diffs(
    match_result: MatchResult,
    *,
    rb_tracks_by_id: dict[str, Any] | None = None,
    dj_tracks_by_uuid: dict[str, Any] | None = None,
    include_review: bool = True,
    include_unmatched: bool = True,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[RatingDiff]:
    """Compute one :class:`RatingDiff` per matched pair.

    Parameters
    ----------
    match_result
        Output of :func:`apps.sync.matcher.match_tracks`. Only the
        matched (and optionally review) buckets are considered.
    rb_tracks_by_id, dj_tracks_by_uuid
        Optional lookups so we can read ``rating`` off the original
        track objects. If omitted, the ``rb_rating`` / ``djay_rating``
        fields fall back to 0 (useful in tests where pairs are built
        directly with ratings already embedded).
    include_review
        When True (default since SYNC-12985), also emit diffs for pairs in the review bucket (they
        are always ``direction == "conflict"`` even if the ratings match,
        because we don't trust the weak-signal pairing enough to write).
    include_unmatched
        When True (default), also emit a no-write ``"unmatched"`` diff
        for every rated track in ``rb_only`` / ``djay_only``.
    window_days
        Last-modified-wins window for pairs whose ratings differ.
    """
    diffs: list[RatingDiff] = []
    pairs: Iterable[MatchedPair] = match_result.matched
    if include_review:
        pairs = [*match_result.matched, *match_result.review]

    for pair in pairs:
        rb = (rb_tracks_by_id or {}).get(pair.rb_id)
        dj = (dj_tracks_by_uuid or {}).get(pair.djay_uuid)
        rb_rating = int(getattr(rb, "rating", 0) or 0)
        dj_rating = int(getattr(dj, "rating", 0) or 0)
        rb_mtime = _modified_at(rb)
        dj_mtime = _modified_at(dj)

        # Pair is only in the review bucket -> always conflict/no-write.
        if pair.status == "review":
            why = "review-bucket pair; manual confirmation required"
            if rb_rating != dj_rating:
                why = f"review-bucket pair with RB={rb_rating} vs djay={dj_rating}; manual confirmation required"
            diffs.append(
                RatingDiff(
                    djay_uuid=pair.djay_uuid,
                    rb_id=pair.rb_id,
                    rb_rating=rb_rating,
                    djay_rating=dj_rating,
                    direction="conflict",
                    new_djay_rating=None,
                    new_rb_rating=None,
                    rationale=why,
                    rb_modified_at=rb_mtime,
                    djay_modified_at=dj_mtime,
                )
            )
            continue

        if rb_rating == 0 and dj_rating == 0:
            direction, new_dj, new_rb, why = "nop", None, None, "both unrated"
        elif rb_rating > 0 and dj_rating == 0:
            direction, new_dj, new_rb, why = (
                "rb->djay",
                rb_rating,
                None,
                f"RB has {rb_rating}-star; djay is unrated",
            )
        elif rb_rating == 0 and dj_rating > 0:
            direction, new_dj, new_rb, why = (
                "djay->rb",
                None,
                dj_rating,
                "djay has rating; RB write path is Phase 4",
            )
        elif rb_rating == dj_rating:
            direction, new_dj, new_rb, why = (
                "nop",
                None,
                None,
                f"both rated {rb_rating}-star",
            )
        else:
            direction, new_dj, new_rb, why = _resolve_differing(rb_rating, dj_rating, rb_mtime, dj_mtime, window_days)

        diffs.append(
            RatingDiff(
                djay_uuid=pair.djay_uuid,
                rb_id=pair.rb_id,
                rb_rating=rb_rating,
                djay_rating=dj_rating,
                direction=direction,
                new_djay_rating=new_dj,
                new_rb_rating=new_rb,
                rationale=why,
                rb_modified_at=rb_mtime,
                djay_modified_at=dj_mtime,
            )
        )

    if include_unmatched:
        rb_only = {str(getattr(t, "id", "")): int(getattr(t, "rating", 0) or 0) for t in match_result.rb_only}
        dj_only = {str(getattr(t, "uuid", "")): int(getattr(t, "rating", 0) or 0) for t in match_result.djay_only}
        diffs.extend(unmatched_rating_diffs(rb_only, dj_only, []))

    return diffs


def unmatched_rating_diffs(
    rb_rating_by_id: Mapping[str, int],
    djay_rating_by_uuid: Mapping[str, int],
    pairs: Iterable[tuple[str, str]],
) -> list[RatingDiff]:
    """Return a no-write ``"unmatched"`` diff for every rated track absent from ``pairs``."""
    pair_list = list(pairs)
    paired_rb = {rb_id for rb_id, _ in pair_list}
    paired_dj = {djay_uuid for _, djay_uuid in pair_list}
    diffs: list[RatingDiff] = []
    for rb_id, rating in rb_rating_by_id.items():
        if rb_id and int(rating or 0) > 0 and rb_id not in paired_rb:
            diffs.append(
                RatingDiff(
                    djay_uuid="",
                    rb_id=rb_id,
                    rb_rating=int(rating),
                    djay_rating=0,
                    direction="unmatched",
                    new_djay_rating=None,
                    new_rb_rating=None,
                    rationale=f"RB has {int(rating)}-star but no matched djay track; not written",
                )
            )
    for djay_uuid, rating in djay_rating_by_uuid.items():
        if djay_uuid and int(rating or 0) > 0 and djay_uuid not in paired_dj:
            diffs.append(
                RatingDiff(
                    djay_uuid=djay_uuid,
                    rb_id="",
                    rb_rating=0,
                    djay_rating=int(rating),
                    direction="unmatched",
                    new_djay_rating=None,
                    new_rb_rating=None,
                    rationale=f"djay has {int(rating)}-star but no matched RB track; not written",
                )
            )
    return diffs


def summarise_diffs(diffs: list[RatingDiff]) -> dict[str, int]:
    """Return counts per direction for summary output."""
    out: dict[str, int] = {}
    for d in diffs:
        out[d.direction] = out.get(d.direction, 0) + 1
    out["total"] = len(diffs)
    return out


__all__ = [
    "RatingDiff",
    "compute_rating_diffs",
    "summarise_diffs",
    "unmatched_rating_diffs",
]
