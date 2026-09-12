"""Canonical :class:`AnalysisRecord` dataclass + JSON (de)serialisation.

Schema followed 06-CONTEXT §D2 verbatim through v1; native-analysis v1
(`specs/native-analysis-v1.md` section 3, "Record") adds the v2 half:
producer identity, versioning, a decode fingerprint, and per-lane results.
Round-trip stable through :meth:`to_json` / :meth:`from_json`; datetimes
normalise to trailing ``Z``.

## v2, and why every field of it is required of an own record

Both producers, the Rust in-app sidecar and the Python backfill, write the
same shape. Producer identity is part of the ``analysis`` primary key
(``backend`` names the lane AND the producer), so rows never overwrite
across producers and run order cannot change what exists. What run order
COULD change is which row a reader picks, so selection is a separate
deterministic pointer rather than a last-writer-wins column: see
:mod:`apps.analysis.store`.

The record body then carries what the pointer and the scorers need to
trust a row:

* ``producer`` / ``producer_version`` -- who made it and at which semver.
  A record without a version cannot be re-queued on a version bump and
  cannot be ranked, so the write fails (NATIVE-09).
* ``uses_model`` / ``model_sha256`` -- a lane that runs trained weights
  stamps their sha256; a model-free lane records ``None``. The pair is
  checked BOTH ways: a model lane may not omit the hash and a model-free
  lane may not invent one. ``uses_model`` exists because only the producer
  knows: the key lane ships classical DSP with no weights while its S-KEY
  bench candidate is a checkpoint, so lane name alone cannot answer it,
  and a rule nothing can evaluate is decoration.
* ``decode_fingerprint`` -- sha256 of the decoded PCM at fixed parameters.
  It is what makes cross-host parity checkable (spec section 13) and what
  the player's own decode is compared against.
* ``lanes`` -- per-lane :class:`~apps.analysis.lanes.LaneResult`, each with
  its own status and reason, so a failed lane is distinguishable from an
  unanalyzed one all the way to the UI.

## Back-compat

The 347 rows stored on this machine were written by ``librosa-only`` and
``librosa+madmom``, before any of the above existed. :meth:`from_json`
loads them with ``lanes={}`` and ``producer="backfill"``, and
:func:`validate_record_contract` applies ONLY to ``own_*`` backends, which
is exactly the set the v1 contract covers. A pre-v1 backend is not made
retroactively invalid, and it is never eligible for the canonical pointer
either.

-Claude
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .lanes import (
    LaneResult,
    OwnBackend,
    Producer,
    SemverError,
    parse_own_backend,
    semver_key,
    validate_lane_result,
)

EnergySource = Literal["mik", "inferred"]


class RecordContractError(ValueError):
    """An own record violates the native-analysis v1 record contract."""


# `sha256:` followed by exactly 64 lowercase hex digits. Both digest fields
# are checked for SHAPE, not merely for presence: a truncated or mistyped
# hash becomes canonical provenance that cannot identify the model or verify
# a decode, and the cross-host parity gate (spec section 13) then compares
# two strings that mean nothing. A prefix is required so the algorithm is
# stated rather than inferred from the length.
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _check_digest(field: str, value: str, stable_id: str) -> None:
    if not _SHA256_RE.match(value):
        raise RecordContractError(
            f"own record for {stable_id!r} has {field}={value!r}, which is not "
            "a sha256 digest (expected 'sha256:' followed by 64 lowercase hex "
            "digits); an unverifiable hash is provenance in name only"
        )


@dataclass(frozen=True)
class AnalysisRecord:
    """One backend's analysis result for one track."""

    stable_id: str
    backend: str
    backend_version: str
    analyzed_at: datetime

    duration_s: float
    sample_rate: int

    bpm: float
    bpm_confidence: float

    key_camelot: str
    key_openkey: str
    key_confidence: float

    energy: int
    energy_source: EnergySource = "inferred"

    onsets_s: list[float] = field(default_factory=list)
    downbeats_s: list[float] = field(default_factory=list)
    rms_peaks_s: list[float] = field(default_factory=list)

    features_blob: dict[str, Any] = field(default_factory=dict)
    #: Own beatgrid records may carry ``features_blob["activations"]`` with
    #: ``{npz|blob, fps}`` pointing at retained Beat This! framewise logits
    #: for the dynamic-grid fitter. See :func:`apps.analysis_beatgrid.activations.activations_ref`.

    # --- v2 (native-analysis v1, spec section 3) -------------------------
    producer: Producer = "backfill"
    producer_version: str = ""
    uses_model: bool = False
    model_sha256: str | None = None
    decode_fingerprint: str = ""
    lanes: dict[str, LaneResult] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        data["analyzed_at"] = _dt_to_iso(self.analyzed_at)
        data["lanes"] = {k: v.to_dict() for k, v in self.lanes.items()}
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> AnalysisRecord:
        data = json.loads(raw)
        data["analyzed_at"] = _iso_to_dt(data["analyzed_at"])
        for k in ("onsets_s", "downbeats_s", "rms_peaks_s"):
            data.setdefault(k, [])
        data.setdefault("features_blob", {})
        # Pre-v2 rows carry none of the fields below. They load as a
        # backfill-produced record with no lane results, which is what they
        # are: an unversioned pre-contract row that no own selector reads.
        data.setdefault("producer", "backfill")
        data.setdefault("producer_version", "")
        data.setdefault("uses_model", False)
        data.setdefault("model_sha256", None)
        data.setdefault("decode_fingerprint", "")
        data["lanes"] = {
            lane: LaneResult.from_dict(block)
            for lane, block in (data.get("lanes") or {}).items()
        }
        return cls(**data)


def validate_record_contract(record: AnalysisRecord) -> None:
    """Enforce the v1 record contract on an own record. Raises, returns None.

    A non-own backend (``librosa-only``, ``mik``, anything without the
    ``own_`` prefix) returns without a check: those rows predate the
    contract and no own selector reads them. That exemption is the reason
    the tests assert BOTH directions -- an own record missing a field must
    raise, and a pre-v1 record must not.
    """
    parsed = parse_own_backend(record.backend)
    if parsed is None:
        return
    _check_identity_and_version(record, parsed)
    _check_provenance_and_lanes(record, parsed)


def _check_identity_and_version(record: AnalysisRecord, parsed: OwnBackend) -> None:
    """Who made it, at which version, and does the body agree with the key."""
    if record.producer != parsed.producer:
        raise RecordContractError(
            f"backend {record.backend!r} names producer {parsed.producer!r} but the "
            f"record body says {record.producer!r}; producer identity is part of "
            "the key and the two halves must agree"
        )
    if not record.producer_version:
        raise RecordContractError(
            f"own record for {record.stable_id!r} has no producer_version; a record "
            "that cannot be ranked or re-queued on a version bump is not writable "
            "(NATIVE-09)"
        )
    try:
        semver_key(record.producer_version)
    except SemverError as exc:
        raise RecordContractError(str(exc)) from exc
    if record.producer_version != record.backend_version:
        raise RecordContractError(
            f"own record producer_version {record.producer_version!r} disagrees with "
            f"backend_version {record.backend_version!r}; the canonical pointer ranks "
            "on backend_version, so a body that says something else is a trap"
        )


def _check_provenance_and_lanes(record: AnalysisRecord, parsed: OwnBackend) -> None:
    """Model hash, decode fingerprint, and the lane blocks themselves."""
    if record.uses_model and not record.model_sha256:
        raise RecordContractError(
            f"own record for {record.stable_id!r} declares uses_model but carries no "
            "model_sha256; a model-backed result is not reproducible without it"
        )
    if not record.uses_model and record.model_sha256:
        raise RecordContractError(
            f"own record for {record.stable_id!r} is model-free but carries "
            f"model_sha256 {record.model_sha256!r}; a fabricated hash is worse than "
            "an absent one (NATIVE-09)"
        )
    # Shape check AFTER the pairing check, so a model-free record carrying a
    # perfectly well-formed digest still fails for the right reason.
    if record.model_sha256:
        _check_digest("model_sha256", record.model_sha256, record.stable_id)
    if record.decode_fingerprint:
        _check_digest("decode_fingerprint", record.decode_fingerprint, record.stable_id)
    if not record.decode_fingerprint:
        raise RecordContractError(
            f"own record for {record.stable_id!r} has no decode_fingerprint; without "
            "it the player's decode of the same track cannot be checked against it"
        )
    if not record.lanes:
        raise RecordContractError(
            f"own record for {record.stable_id!r} carries no lane results; an own "
            "record exists to hold at least the lane its backend names"
        )
    if parsed.lane not in record.lanes:
        raise RecordContractError(
            f"own record on backend {record.backend!r} carries lanes "
            f"{sorted(record.lanes)} but not its own lane {parsed.lane!r}"
        )
    if record.duration_s is None:
        # `None` is the lane gate's "no duration supplied" sentinel, so a
        # record deserialized with `duration_s: null` would sail through
        # every lane check and fail later as a raw `TypeError` at
        # `float(record.duration_s)` in the store (Codex P2, PR #1562).
        raise RecordContractError(
            f"own record for {record.stable_id!r} has no duration_s; a record "
            "without a decoded length cannot bound its beats or key segments"
        )
    for lane, result in record.lanes.items():
        validate_lane_result(lane, result, duration_s=record.duration_s)


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.isoformat().replace("+00:00", "Z")


def _iso_to_dt(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def __getattr__(name: str) -> object:
    if name == "activations_ref":
        from apps.analysis_beatgrid.activations import activations_ref

        return activations_ref
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AnalysisRecord",
    "EnergySource",
    "RecordContractError",
    "validate_record_contract",
]
