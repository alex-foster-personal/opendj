"""Selection lanes, lane results, and the own-producer backend naming scheme.

`specs/native-analysis-v1.md` section 3 splits native analysis into five
SELECTION LANES (the packages of D5, not the scoring rows of section 2):
``beatgrid`` (grid, downbeat, BPM and tempo-change together), ``key`` (key
and key-change), ``waveform``, ``loudness`` and ``vocal``. Grid and BPM are
one lane on purpose: a beat entry carries both its time and its local BPM,
so a separate BPM toggle would let a composed read mix rekordbox BPM onto
own beat times.

Two things live here because both the record and the store need them and
neither owns the other:

* :class:`LaneResult` plus :func:`validate_lane_payload`, the pure
  shape check for what a producer put in a lane block.
* The own-producer backend naming scheme, ``own_<lane>.<producer>`` and
  ``own_<lane>.cand.<name>``, with :func:`parse_own_backend` as its only
  reader. Producer identity is part of the ``analysis`` primary key, so
  rows never overwrite across producers and run order cannot change what
  exists (spec section 3, "Record").

Semver lives here too: the canonical pointer ranks own rows by
``backend_version``, so that version has to be an ordered value rather
than the free-form build string the pre-v1 backends wrote
(``librosa==0.10.2.post1+madmom==0.17.dev0``). :func:`semver_key` refuses
anything it cannot order instead of sorting it lexically.

-Claude
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .lane_enums import (
    LANE_STATUSES,
    LANES,
    OWN_BACKEND_PREFIX,
    PRODUCERS,
    Lane,
    LaneContractError,
    LaneStatus,
    Producer,
)
from .lane_payloads import check_lane_confidence as _check_lane_confidence
from .lane_payloads import validate_lane_payload as _payload_validate

#-----------------------------------------------------------------------------
# lane result
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class LaneResult:
    """One lane's outcome inside an :class:`~apps.analysis.record.AnalysisRecord`.

    ``status`` is the whole point: a lane that could not measure records
    ``failed`` plus the lane's named reason (``no_trackable_pulse``,
    ``not_decoded``, ``no_tonal_center``, ...), and a lane that has not run
    records ``missing``. Neither is ever rendered as a value, so a consumer
    can tell a failed analysis from an unanalyzed track without guessing.
    """

    status: LaneStatus
    reason: str | None = None
    confidence: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "confidence": self.confidence,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LaneResult:
        return cls(
            status=data["status"],
            reason=data.get("reason"),
            confidence=data.get("confidence"),
            payload=dict(data.get("payload") or {}),
        )


#-----------------------------------------------------------------------------
# lane payload shapes
#-----------------------------------------------------------------------------

def validate_lane_payload(lane: str, payload: Mapping[str, Any]) -> None:
    """Pure shape check for one lane's ``ok`` payload. Raises, returns None.

    The per-lane shapes and every range rule live in
    :mod:`apps.analysis.lane_payloads`; this is the entry point the record and
    the store call. Shapes are documented on
    :func:`apps.analysis.lane_payloads.validate_lane_payload`.
    """
    _payload_validate(lane, payload)


def validate_lane_result(lane: str, result: LaneResult) -> None:
    """Check one lane block: status, reason discipline, and payload shape."""
    if lane not in LANES:
        raise LaneContractError(f"unknown lane {lane!r}; lanes are {LANES}")
    if result.status not in LANE_STATUSES:
        raise LaneContractError(
            f"{lane}.status must be one of {LANE_STATUSES}, got {result.status!r}"
        )
    _check_lane_confidence(lane, result.confidence)
    if result.status == "failed":
        if not result.reason:
            raise LaneContractError(
                f"{lane}.status is failed without a reason; a failed lane names "
                "why (no_trackable_pulse, not_decoded, no_tonal_center, ...)"
            )
        if result.payload:
            raise LaneContractError(
                f"{lane}.status is failed but carries a payload; a failed lane "
                "has no measurement to serve"
            )
    elif result.status == "missing":
        if result.payload:
            raise LaneContractError(
                f"{lane}.status is missing but carries a payload"
            )
    elif result.status == "ok":
        validate_lane_payload(lane, result.payload)


#-----------------------------------------------------------------------------
# backend naming
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class OwnBackend:
    """The parsed halves of an ``own_<lane>.<producer>`` backend name."""

    lane: str
    producer: Producer
    candidate: str | None = None


_CAND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def own_backend(lane: str, producer: Producer, candidate: str | None = None) -> str:
    """Build the backend name for an own producer's rows.

    ``own_<lane>.<producer>`` for the two shipping producers, and
    ``own_<lane>.cand.<name>`` for a bench candidate, which the canonical
    pointer never selects.
    """
    if lane not in LANES:
        raise LaneContractError(f"unknown lane {lane!r}; lanes are {LANES}")
    if producer not in PRODUCERS:
        raise LaneContractError(f"unknown producer {producer!r}; producers are {PRODUCERS}")
    if producer == "cand":
        if not candidate or not _CAND_NAME_RE.match(candidate):
            raise LaneContractError(
                f"a cand backend needs a [a-z0-9_] candidate name, got {candidate!r}"
            )
        return f"{OWN_BACKEND_PREFIX}{lane}.cand.{candidate}"
    if candidate is not None:
        raise LaneContractError(
            f"producer {producer!r} takes no candidate name, got {candidate!r}"
        )
    return f"{OWN_BACKEND_PREFIX}{lane}.{producer}"


def parse_own_backend(backend: str) -> OwnBackend | None:
    """Parse an own backend name, or return ``None`` for a pre-v1 backend.

    ``None`` is the answer for ``librosa-only`` and ``librosa+madmom``,
    the two backends the 347 stored rows on this machine were written by:
    they predate the v1 contract and are not own records, so they are not
    subject to it and are never eligible for the canonical pointer.
    """
    if not backend.startswith(OWN_BACKEND_PREFIX):
        return None
    body = backend[len(OWN_BACKEND_PREFIX):]
    lane, _, tail = body.partition(".")
    if lane not in LANES or not tail:
        raise LaneContractError(
            f"backend {backend!r} starts with {OWN_BACKEND_PREFIX!r} but is not a "
            f"valid own backend name (own_<lane>.<producer> with lane in {LANES})"
        )
    if tail in ("inapp", "backfill"):
        return OwnBackend(lane=lane, producer=tail, candidate=None)  # type: ignore[arg-type]
    prefix, _, candidate = tail.partition(".")
    if prefix != "cand" or not candidate or not _CAND_NAME_RE.match(candidate):
        raise LaneContractError(
            f"backend {backend!r} is neither own_<lane>.inapp/.backfill nor "
            "own_<lane>.cand.<name>"
        )
    return OwnBackend(lane=lane, producer="cand", candidate=candidate)


#-----------------------------------------------------------------------------
# semver
#-----------------------------------------------------------------------------

# Semver 2.0.0 grammar for the prerelease: dot-separated identifiers, each
# either alphanumeric-with-hyphens or a NUMERIC identifier with no leading
# zero, and none of them empty. The loose `[0-9a-zA-Z.-]+` this replaced
# accepted `1.0.0-01` and `1.0.0-alpha..1`, and worse, `semver_key("1.0.0-01")`
# EQUALLED the key for `1.0.0-1` -- two distinct version strings ranking
# identically, which defeats the write-order independence the canonical
# pointer promises (Codex P2, PR #1549).
_PRERELEASE_IDENT_RE = re.compile(r"^(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)$")

_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9a-zA-Z.-]+))?$"
)


class SemverError(ValueError):
    """A version string the canonical pointer cannot order."""


def semver_key(version: str) -> tuple[int, int, int, int, tuple[Any, ...]]:
    """Ordering key for an own producer's ``backend_version``.

    Refuses anything that is not ``MAJOR.MINOR.PATCH`` with an optional
    prerelease, because the alternative is a lexical sort that would rank
    ``0.10.0`` below ``0.9.0`` and pick the wrong canonical row in silence.
    A release outranks its own prereleases (semver 2.0.0 rule 11).
    """
    match = _SEMVER_RE.match(version)
    if match is None:
        raise SemverError(
            f"own backend_version {version!r} is not semver MAJOR.MINOR.PATCH; "
            "the canonical pointer orders own rows by version and cannot rank "
            "a free-form build string"
        )
    pre = match.group("pre")
    if pre is None:
        return (
            int(match.group("major")), int(match.group("minor")),
            int(match.group("patch")), 1, (),
        )
    tokens = pre.split(".")
    bad = [t for t in tokens if not _PRERELEASE_IDENT_RE.match(t)]
    if bad:
        raise SemverError(
            f"own backend_version {version!r} has invalid semver prerelease "
            f"identifier(s) {bad}; each must be non-empty, and a numeric one "
            "must have no leading zero"
        )
    parts: list[Any] = [
        (0, int(token), "") if token.isdigit() else (1, 0, token)
        for token in tokens
    ]
    return (
        int(match.group("major")), int(match.group("minor")),
        int(match.group("patch")), 0, tuple(parts),
    )


__all__ = [
    "LANES",
    "LANE_STATUSES",
    "OWN_BACKEND_PREFIX",
    "PRODUCERS",
    "Lane",
    "LaneContractError",
    "LaneResult",
    "LaneStatus",
    "OwnBackend",
    "Producer",
    "SemverError",
    "own_backend",
    "parse_own_backend",
    "semver_key",
    "validate_lane_payload",
    "validate_lane_result",
]
