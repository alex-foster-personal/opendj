"""Helpers for building ``ProvenanceValue<T>`` dicts (spec section 4.1).

Keeping this out of ``canon.py`` because provenance construction is a
semantic operation (enum-constrained ``source``, timezone-aware timestamp)
while ``canon`` is purely syntactic.

``modified_at`` is a REQUIRED keyword argument to :func:`wrap`, by design.
The open-dj spec (section 6) defines ``modified_at`` as "when the SOURCE
last modified this value" -- e.g. when rekordbox's DjmdContent row was
last touched, not when ``open-dj-tool`` happened to run an export. A
caller that reaches for the wall clock ("now") as a stand-in for that is
not missing a default; it is asserting a fact it does not have, and two
exports of the same untouched library would then disagree on bytes purely
because they ran a second apart (caught live: PR #4119 fast-tier CI flake
in ``test_export_deterministic``). Callers must supply the real source
timestamp, or -- when the vendor genuinely does not expose one -- an
explicit clock value decided at the call site, so the choice is visible
in a diff rather than buried in this helper.
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
    modified_at: datetime | str,
    confidence: float | None = None,
) -> dict:
    """Return a ``ProvenanceValue<T>`` dict for ``value``.

    ``source`` must be one of the strawman section 4.1 enum values. A
    :class:`ValueError` is raised on unknown sources to catch typos early.

    ``modified_at`` is REQUIRED (no wall-clock default -- see module
    docstring) and accepts a :class:`datetime` or a pre-formatted
    ISO-8601 string. Naive datetimes are assumed to be UTC.
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


def _format_timestamp(modified_at: datetime | str) -> str:
    if isinstance(modified_at, str):
        return modified_at
    # datetime
    dt = modified_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
