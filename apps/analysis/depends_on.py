"""The five-field dependency identity a downstream lane records.

Spec `specs/native-analysis-v1-lanes/nav1-key-record.md` defines the block a
key record carries so a later reader can tell whether the beatgrid it was
computed against is still the canonical one::

    depends_on: {beatgrid: {backend, producer_version, model_sha256,
                            decode_fingerprint, record_digest}}

Why five fields and not the version triple alone: a beatgrid re-decoded from
DIFFERENT source audio at the SAME producer version has the same backend,
the same producer_version and the same model hash, and is a different
beatgrid. ``decode_fingerprint`` is what catches that, and
``record_digest`` catches a beatgrid whose contents changed without any of
the other four moving.

Ownership note: the lane brief points at nav1-key-record's shared
``beatgrid_record_digest`` helper. That lane has not merged, and the queue's
dependency cascade cannot wait for it (a cascade that compares nothing is
not a cascade), so the helper is defined HERE, in the core package both
lanes already depend on, rather than duplicated later. When nav1-key-record
lands it imports this rather than adding a second definition: two digests of
the same record that disagree is precisely the failure this block exists to
detect.

-Claude
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .record import AnalysisRecord

#: The key a record's ``features_blob`` carries its dependency block under.
DEPENDS_ON_KEY: str = "depends_on"

#: The exact field set. Compared field for field; a block missing any of
#: them is not a weaker match, it is not a block.
DEPENDS_ON_FIELDS: tuple[str, ...] = (
    "backend",
    "producer_version",
    "model_sha256",
    "decode_fingerprint",
    "record_digest",
)


def beatgrid_record_digest(record: AnalysisRecord, lane: str = "beatgrid") -> str:
    """``sha256:`` digest of the CONTENT of one lane block of a record.

    Digests the lane result (status, reason, confidence, payload) rather
    than the whole record, because the whole record carries fields that move
    for reasons the dependent lane does not care about (``analyzed_at``, the
    other lanes' blocks). Two records whose beatgrid block is byte-identical
    under a canonical serialization produce the same digest.
    """
    result = record.lanes.get(lane)
    if result is None:
        raise KeyError(
            f"record {record.stable_id!r} from {record.backend!r} carries no "
            f"{lane!r} lane block, so it has no digest to depend on"
        )
    blob = json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def dependency_identity(
    record: AnalysisRecord, lane: str = "beatgrid"
) -> dict[str, Any]:
    """The five-field block naming ``record`` as a dependency."""
    return {
        "backend": record.backend,
        "producer_version": record.producer_version,
        "model_sha256": record.model_sha256,
        "decode_fingerprint": record.decode_fingerprint,
        "record_digest": beatgrid_record_digest(record, lane),
    }


def declared_dependency(
    record: AnalysisRecord, lane: str
) -> Mapping[str, Any] | None:
    """The dependency block ``record`` declares on ``lane``, if any."""
    block = record.features_blob.get(DEPENDS_ON_KEY)
    if not isinstance(block, Mapping):
        return None
    declared = block.get(lane)
    if not isinstance(declared, Mapping):
        return None
    return declared


def dependency_matches(
    declared: Mapping[str, Any] | None, actual: Mapping[str, Any]
) -> bool:
    """Field-for-field comparison of a declared block against the real one.

    A missing block is a MISMATCH, not a pass: a record that declares no
    dependency identity cannot claim to be current with respect to one. A
    block missing any of the five fields is a mismatch for the same reason.
    """
    if declared is None:
        return False
    for field_name in DEPENDS_ON_FIELDS:
        if field_name not in declared:
            return False
        if declared[field_name] != actual.get(field_name):
            return False
    return True


__all__ = [
    "DEPENDS_ON_FIELDS",
    "DEPENDS_ON_KEY",
    "beatgrid_record_digest",
    "declared_dependency",
    "dependency_identity",
    "dependency_matches",
]
