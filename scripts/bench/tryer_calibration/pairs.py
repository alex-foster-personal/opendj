"""The calibration pair list: a checksummed fixture of stable ids (TRYER-21).

The fixture names tracks only by ``tracks.stable_id`` (40-hex sha1), never by
path or title, so it is safe in the tracked tree and in the public mirror.
Paths are resolved at run time from the data dir by ``try_pair``.

``sha256`` in the file is the hash of the canonical JSON of ``pairs`` (sorted
keys, no whitespace). Editing a pair without re-stamping the hash fails loud,
so a round's denominator can never drift silently.

Acceptance:
  [if] the stored sha256 does not match the pairs [then ⛔️] PairsError
  [if] a stable id is not 40 lowercase hex [then ⛔️] PairsError naming the pair
  [if] two pairs share an id or an out/in ordering [then ⛔️] PairsError
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE_PATH: Path = Path(__file__).parent / "fixtures" / "oltf_pairs_v1.json"
_STABLE_ID = re.compile(r"^[0-9a-f]{40}$")


class PairsError(ValueError):
    """The pair fixture is malformed or its checksum does not match."""


@dataclass(frozen=True)
class CalibrationPair:
    """One outgoing -> incoming pair to calibrate on."""

    pair_id: str
    stratum: str
    out_id: str
    in_id: str


def pairs_checksum(pairs: list[dict[str, Any]]) -> str:
    """sha256 of the canonical JSON of ``pairs``."""
    canonical = json.dumps(pairs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_pairs(path: Path = FIXTURE_PATH) -> list[CalibrationPair]:
    """Read and verify the fixture; raise on any defect."""
    return load_pairs_verified(path)[0]


def load_pairs_verified(path: Path = FIXTURE_PATH) -> tuple[list[CalibrationPair], str]:
    """(the verified pairs, the checksum they were verified against), read once: a caller that records provenance
    records THIS checksum, never a re-read of a file that may have changed since."""
    document = json.loads(path.read_text(encoding="utf-8"))
    raw: list[dict[str, Any]] = document["pairs"]
    actual = pairs_checksum(raw)
    if document.get("sha256") != actual:
        raise PairsError(f"{path}: stored sha256 {document.get('sha256')!r} != computed {actual}")
    pairs = [CalibrationPair(p["pair_id"], p["stratum"], p["out_id"], p["in_id"]) for p in raw]
    for pair in pairs:
        for side in (pair.out_id, pair.in_id):
            if not _STABLE_ID.match(side):
                raise PairsError(f"{pair.pair_id}: {side!r} is not a 40-hex stable id")
        if pair.out_id == pair.in_id:
            raise PairsError(f"{pair.pair_id}: outgoing and incoming are the same track")
    if len({p.pair_id for p in pairs}) != len(pairs):
        raise PairsError(f"{path}: duplicate pair_id")
    if len({(p.out_id, p.in_id) for p in pairs}) != len(pairs):
        raise PairsError(f"{path}: duplicate out/in ordering")
    return pairs, actual
