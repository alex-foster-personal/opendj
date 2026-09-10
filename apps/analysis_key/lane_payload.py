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

**`depends_on.beatgrid` is the dependency identity, not a second staleness
check.** Spec section 5: a key record whose segments are `ok` was computed on
a SPECIFIC own beatgrid record, and the five fields here
(`backend`/`producer_version`/`model_sha256`/`decode_fingerprint`/
`record_digest`) are what let a later reader tell whether that beatgrid is
still the canonical one. This module only BUILDS and SHAPE-VALIDATES the
block; comparing it against the CURRENT canonical beatgrid (the staleness
enforcement, canonical-pointer exclusion and re-queue) is the canonical
rebuild / queue's own job, in files this lane does not own -- see the PR body
for the named overlap with the in-flight queue lane.

-Claude
"""
from __future__ import annotations

import hashlib
import json
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

#: The exact five-field dependency-identity schema (spec section 5). Compared
#: field for field by whichever reader checks staleness; a block missing any
#: of them is not a weaker match, it is not a block.
DEPENDS_ON_FIELDS: tuple[str, ...] = (
    "backend",
    "producer_version",
    "model_sha256",
    "decode_fingerprint",
    "record_digest",
)

#: The beatgrid lane payload fields the content digest is taken over, IN THIS
#: ORDER (spec section 5). Only these fields: a beatgrid record carries other
#: top-level columns (analyzed_at, ...) that move for reasons a key lane does
#: not depend on.
_BEATGRID_DIGEST_FIELDS: tuple[str, ...] = (
    "beats", "bpm", "octave_reason", "tempo_changes", "static_grid_untrusted",
)


def beatgrid_record_digest(beatgrid_payload: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of a beatgrid lane payload.

    Content identity for the beatgrid record itself, independent of a
    producer ever bumping its version number (spec section 5): two beatgrid
    payloads that agree on every digested field produce the same digest
    regardless of key ordering or float formatting in the source dict, since
    ``json.dumps(..., sort_keys=True)`` normalizes both.
    """
    canonical = {
        field_name: beatgrid_payload[field_name] for field_name in _BEATGRID_DIGEST_FIELDS
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def depends_on_identity(
    *,
    backend: str,
    producer_version: str,
    model_sha256: str | None,
    decode_fingerprint: str,
    beatgrid_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """The five-field block naming one beatgrid record as a dependency."""
    return {
        "backend": backend,
        "producer_version": producer_version,
        "model_sha256": model_sha256,
        "decode_fingerprint": decode_fingerprint,
        "record_digest": beatgrid_record_digest(beatgrid_payload),
    }


def validate_key_depends_on(payload: Mapping[str, Any]) -> None:
    """Shape-only check: an `ok` segments block carries a complete block.

    Called from :mod:`apps.analysis.lane_payloads`'s key validator. This is
    the write-boundary contract (a missing or malformed block on an `ok`
    segments block is a contract error, per spec section 5) -- it does not
    compare the block against any OTHER record, which is the staleness
    enforcement the canonical rebuild owns.
    """
    segments_block = payload.get("segments")
    if not isinstance(segments_block, Mapping) or segments_block.get("status") != "ok":
        return
    depends_on = payload.get("depends_on")
    if not isinstance(depends_on, Mapping):
        raise TypeError(
            "key.depends_on is missing; an ok segments block was computed on "
            "an own beatgrid record and must name it"
        )
    beatgrid = depends_on.get("beatgrid")
    if not isinstance(beatgrid, Mapping):
        raise TypeError(
            "key.depends_on.beatgrid is missing; an ok segments block was "
            "computed on an own beatgrid record and must name it"
        )
    missing = [name for name in DEPENDS_ON_FIELDS if name not in beatgrid]
    if missing:
        raise ValueError(
            f"key.depends_on.beatgrid is missing field(s) {missing}; all of "
            f"{DEPENDS_ON_FIELDS} are required on an ok segments block"
        )
    if not isinstance(beatgrid["decode_fingerprint"], str) or not beatgrid["decode_fingerprint"]:
        raise ValueError(
            "key.depends_on.beatgrid.decode_fingerprint must be a non-empty string"
        )
    if not isinstance(beatgrid["record_digest"], str) or not beatgrid["record_digest"]:
        raise ValueError(
            "key.depends_on.beatgrid.record_digest must be a non-empty string"
        )


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
    depends_on_beatgrid: Mapping[str, Any] | None = None,
) -> KeyLane:
    """One estimate (plus its flag and segment block) into one lane block.

    `depends_on_beatgrid` is the five-field block from :func:`depends_on_identity`.
    Required exactly when `segments_block["status"] == "ok"` (an own beatgrid
    record was necessarily read to produce those segments); omitted otherwise,
    since `missing`/`failed` segments name no beatgrid record to depend on.
    """
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
    if segments_block.get("status") == "ok" and depends_on_beatgrid is None:
        raise ValueError(
            "segments block is status ok with no depends_on_beatgrid; an ok "
            "key-change segmentation was computed on an own beatgrid record "
            "and must name it"
        )
    key = estimate.key
    payload: dict[str, Any] = {
        "camelot": canon.to_camelot(key),
        "openkey": canon.to_open_key(key),
        "pitch_class": key.pitch_class,
        "is_minor": key.is_minor,
        "confidence": float(estimate.confidence),
        "segments": dict(segments_block),
    }
    if depends_on_beatgrid is not None:
        payload["depends_on"] = {"beatgrid": dict(depends_on_beatgrid)}
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


__all__ = [
    "DEPENDS_ON_FIELDS",
    "REASON_NO_TONAL_CENTER",
    "KeyLane",
    "beatgrid_record_digest",
    "build_key_lane",
    "depends_on_identity",
    "validate_key_depends_on",
]
