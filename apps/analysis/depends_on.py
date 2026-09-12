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

The content digest is defined once in
:mod:`apps.analysis_key.beatgrid_digest` (stdlib-only) and imported here so
the queue cascade and the key producer hash the same five beatgrid payload
fields.

-Claude
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from apps.analysis_key.beatgrid_digest import (
    beatgrid_record_digest as _payload_beatgrid_digest,
)

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
    """``sha256:`` digest of the beatgrid lane payload content."""
    result = record.lanes.get(lane)
    if result is None:
        raise KeyError(
            f"record {record.stable_id!r} from {record.backend!r} carries no "
            f"{lane!r} lane block, so it has no digest to depend on"
        )
    return _payload_beatgrid_digest(result.payload)


def dependency_identity(
    record: AnalysisRecord, lane: str = "beatgrid"
) -> dict[str, Any]:
    """The five-field block naming ``record`` as a dependency."""
    if lane == "beatgrid" or "beatgrid" in record.lanes:
        digest = beatgrid_record_digest(record, "beatgrid")
    else:
        # ``cascade_dependents`` builds ``actual`` before checking whether
        # ``lane`` has dependents. A key-only write has no beatgrid block on
        # the same row; hash the named lane for that unused path only.
        named = record.lanes.get(lane)
        if named is None:
            raise KeyError(
                f"record {record.stable_id!r} from {record.backend!r} carries no "
                f"{lane!r} lane block, so it has no digest to depend on"
            )
        blob = json.dumps(named.to_dict(), sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return {
        "backend": record.backend,
        "producer_version": record.producer_version,
        "model_sha256": record.model_sha256,
        "decode_fingerprint": record.decode_fingerprint,
        "record_digest": digest,
    }


def declared_dependency(
    record: AnalysisRecord, lane: str
) -> Mapping[str, Any] | None:
    """The dependency block ``record`` declares on ``lane``, if any."""
    block = record.features_blob.get(DEPENDS_ON_KEY)
    if isinstance(block, Mapping):
        declared = block.get(lane)
        if isinstance(declared, Mapping):
            return declared
    for lane_result in record.lanes.values():
        payload = lane_result.payload
        if not isinstance(payload, Mapping):
            continue
        depends_on = payload.get(DEPENDS_ON_KEY)
        if not isinstance(depends_on, Mapping):
            continue
        declared = depends_on.get(lane)
        if isinstance(declared, Mapping):
            return declared
    return None


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
