"""Estimate -> the `key` lane block of an `AnalysisRecord`.

THE SEAM BETWEEN THE PRODUCER AND THE RECORD, the same seam
`apps/analysis_beatgrid/lane_payload.py` holds for the grid: `profiles.py`
scores chroma in one world, `apps/analysis/lanes.py` validates a lane payload
with a fixed shape in another, and this is the pure function between them. It
takes a `KeyEstimate`, a `TonalCenterFlag` and the segmenter's block, and it
touches no audio, no database and no clock.

THREE DECISIONS, none of them silent:

**`no_tonal_center` is `status: failed`, not a published guess.** The scorer
always returns its best of 24 candidates, so the flag is the ONLY thing that
stops a drone or an unpitched intro being published as a key (spec section 4,
"Confidence + flag on every lane, no silent guesses"). The reason carries both
the lane's named token and the flag's finer symptom
(``no_tonal_center: low_confidence``), the way the beatgrid lane carries
`runner_error: ...`: the token is what a consumer matches on, the symptom is
what a reader needs, and dropping either loses information the other cannot
recover.

**The `segments` block is present on EVERY `ok` payload.** Spec section 3: "A
bare absent array is not a state." A stable key is one segment; the change
analysis being unavailable is the block's own `missing`, which says so; a
measured refusal is its `failed` with a reason. The scalar key and the change
analysis fail independently, which is the whole reason the block carries its
own status rather than inheriting the lane's.

**Notations come from `apps/analysis_key.canon`, never a table here.** The
Camelot/Open Key conversion is the one the bench scorer canonicalizes with, so
a payload that disagrees with a scored round is impossible by construction
(A minor = 8A = 1m; unit-tested in both directions in both modules).

-Claude
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from apps.analysis_key import canon
from apps.analysis_key.flags import TonalCenterFlag
from apps.analysis_key.profiles import KeyEstimate

#: The lane's named failure reason for a track with no publishable tonal
#: center (spec section 3's own example list; `ProvenanceOut.reason` carries it
#: to the track row, which renders the cell inert with the tooltip).
REASON_NO_TONAL_CENTER = "no_tonal_center"


@dataclass(frozen=True)
class KeyLane:
    """One track's key lane outcome, in the record contract's terms.

    ``status`` is only ever `ok` or `failed`: `missing` describes a lane that
    has not run, which is a fact about the STORE and not something a producer
    that just ran can report about itself.
    """

    status: Literal["ok", "failed"]
    reason: str | None = None
    confidence: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def build_key_lane(
    estimate: KeyEstimate,
    flag: TonalCenterFlag,
    segments_block: Mapping[str, Any],
) -> KeyLane:
    """One estimate (plus its flag and segment block) into one lane block."""
    if flag.no_tonal_center:
        if flag.reason is None:
            # evaluate_tonal_center names every failure; a flag that says
            # "no tonal center" without saying why would be published as a
            # `failed` lane with no reason, which the record contract refuses.
            raise ValueError(
                "tonal-center flag is set with no reason; a failed key lane "
                "names why it could not measure"
            )
        return KeyLane(
            status="failed",
            reason=f"{REASON_NO_TONAL_CENTER}: {flag.reason}",
            confidence=None,
            payload={},
        )

    _check_segments_block(segments_block)
    key = estimate.key
    payload: dict[str, Any] = {
        "camelot": canon.to_camelot(key),
        "openkey": canon.to_open_key(key),
        "pitch_class": key.pitch_class,
        "is_minor": key.is_minor,
        "confidence": float(estimate.confidence),
        "segments": dict(segments_block),
    }
    return KeyLane(
        status="ok", reason=None, confidence=float(estimate.confidence), payload=payload
    )


def _check_segments_block(segments_block: Mapping[str, Any]) -> None:
    """Refuse the one shape the contract refuses, at the point it is built.

    The store's own validator catches this too, and the read side catches it
    again (`own_key_overlay`); it is checked here as well because this is the
    only place that can say WHICH producer path built it, and an `ok` block
    with no segments -- a stable key that names no key -- must never be
    constructible in the first place.
    """
    if not isinstance(segments_block, Mapping):
        raise ValueError(f"segments block must be a mapping, got {segments_block!r}")
    if segments_block.get("status") == "ok" and not segments_block.get("segments"):
        raise ValueError(
            "segments block is status ok with no segments; a stable key is one "
            "segment, and an unavailable analysis is status missing"
        )


__all__ = ["REASON_NO_TONAL_CENTER", "KeyLane", "build_key_lane"]
