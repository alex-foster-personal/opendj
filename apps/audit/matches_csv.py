"""Shared helpers for reading ``(private path)`` rows."""
from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

DEFAULT_MIN_MATCH_CONFIDENCE = 0.70


def parse_match_confidence(raw: str | None) -> float:
    """Parse a matches.csv ``confidence`` cell; non-numeric values become ``0.0``."""
    try:
        return float(raw or 0)
    except ValueError:
        return 0.0


def iter_match_pairs(
    matches_csv: Path,
    *,
    min_confidence: float = DEFAULT_MIN_MATCH_CONFIDENCE,
) -> Iterator[tuple[str, str]]:
    """Yield ``(rb_content_id, djay_uuid)`` for rows above ``min_confidence``.

    Accepts ``rb_content_id`` or legacy ``rb_id`` column names in matches.csv.
    """
    with matches_csv.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for rec in reader:
            if parse_match_confidence(rec.get("confidence")) < min_confidence:
                continue
            rb_id = rec.get("rb_content_id") or rec.get("rb_id") or ""
            djay_uuid = rec.get("djay_uuid") or ""
            if rb_id and djay_uuid:
                yield rb_id, djay_uuid


__all__ = [
    "DEFAULT_MIN_MATCH_CONFIDENCE",
    "iter_match_pairs",
    "parse_match_confidence",
]
