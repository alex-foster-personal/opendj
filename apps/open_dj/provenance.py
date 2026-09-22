"""Helpers for building ``ProvenanceValue<T>`` dicts (spec section 4.1).

Keeping this out of ``canon.py`` because provenance construction is a
semantic operation (enum-constrained ``source``, timezone-aware timestamp)
while ``canon`` is purely syntactic.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

ProvenanceSource = Literal[
    "mik", "rekordbox", "djay", "serato", "traktor",
    "open-dj-tool", "manual", "inferred",
]

# Mirror of the schema enum -- keep this in sync with
# open-dj/schema/v0.2/open-dj.schema.json $defs/ProvenanceValue.
_ALLOWED_SOURCES = frozenset({
    "mik", "rekordbox", "djay", "serato", "traktor",
    "open-dj-tool", "manual", "inferred",
})


def wrap(
    value: Any,
    *,
    source: str,
    modified_at: datetime | str | None = None,
    confidence: float | None = None,
) -> dict:
    """Return a ``ProvenanceValue<T>`` dict for ``value``.

    ``source`` must be one of the strawman section 4.1 enum values. A
    :class:`ValueError` is raised on unknown sources to catch typos early.

    ``modified_at`` accepts a :class:`datetime`, a pre-formatted ISO-8601
    string, or ``None`` (now, UTC, second-precision). Naive datetimes are
    assumed to be UTC.
    """
    if source not in _ALLOWED_SOURCES:
        raise ValueError(
            f"{source!r} is not a valid open-dj ProvenanceValue.source. "
            f"Allowed: {sorted(_ALLOWED_SOURCES)}."
        )

    stamp = _format_timestamp(modified_at)
    out: dict = {"value": value, "source": source, "modified_at": stamp}
    if confidence is not None:
        out["confidence"] = confidence
    return out


def _format_timestamp(modified_at: datetime | str | None) -> str:
    if modified_at is None:
        now = datetime.now(UTC).replace(microsecond=0)
        return now.isoformat().replace("+00:00", "Z")
    if isinstance(modified_at, str):
        return modified_at
    # datetime
    dt = modified_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
