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
  >0      >0 ne   conflict  report, no write
======  =======  =========  ===========

Only ``direction == "rb->djay"`` records carry a non-None
``new_djay_rating`` and are eligible for a live write.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from apps.sync.matcher import MatchedPair, MatchResult


@dataclass(slots=True)
class RatingDiff:
    """Proposed (or reported) rating delta for one matched pair."""

    djay_uuid: str
    rb_id: str
    rb_rating: int
    djay_rating: int
    direction: str  # "rb->djay" | "djay->rb" | "conflict" | "nop"
    new_djay_rating: int | None
    new_rb_rating: int | None
    rationale: str


def compute_rating_diffs(
    match_result: MatchResult,
    *,
    rb_tracks_by_id: dict[str, Any] | None = None,
    dj_tracks_by_uuid: dict[str, Any] | None = None,
    include_review: bool = False,
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
        When True, also emit diffs for pairs in the review bucket (they
        are always ``direction == "conflict"`` even if the ratings match,
        because we don't trust the weak-signal pairing enough to write).
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

        # Pair is only in the review bucket -> always conflict/no-write.
        if pair.status == "review":
            diffs.append(
                RatingDiff(
                    djay_uuid=pair.djay_uuid,
                    rb_id=pair.rb_id,
                    rb_rating=rb_rating,
                    djay_rating=dj_rating,
                    direction="conflict",
                    new_djay_rating=None,
                    new_rb_rating=None,
                    rationale="review-bucket pair; manual confirmation required",
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
            direction, new_dj, new_rb, why = (
                "conflict",
                None,
                None,
                f"RB={rb_rating} vs djay={dj_rating}; requires --tracks override",
            )

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


__all__ = ["RatingDiff", "compute_rating_diffs", "summarise_diffs"]
