"""Classic sampler banks: one versioned JSON document beside ``state.db`` (issue #9994).

``<state dir>/sampler-banks.json`` holds the rekordbox-style sampler unit's four banks of
sixteen slots and the active bank:

    {"v": 1, "active_bank": 0, "master_gain_db": 0.0, "banks": [[slot | null] x 16] x 4}

A slot is ``{"stable_id": str, "mode", "gain_db", "title"}`` for a library track, or
``{"path": str, "mode", "gain_db", "title"}`` for a file loaded from disk; ``null`` is empty.
Modes are the #9989 play modes; ``gain_db`` is #9989's slot gain range, -24..+6 dB.
``master_gain_db`` is the unit's master gain, #9989's -60..0 dB. It is optional on input: a
document stored before it existed (or a body that leaves it out) reads as 0 dB, and every
write stores it explicitly from then on, so the version stays 1.

The file is replaced whole and atomically (temp file in the same directory, fsync, rename),
and every write and read is validated: an unknown mode, an out-of-range gain or a stray key
is refused with ``InvalidBanksDocument`` (a ``ValueError``), never stored or silently
repaired. An absent file reads as the default document (every slot empty, bank 0 active).

CloudSync: not synced. ``cue_points`` syncs because it is a per-track ``track_fields`` row
(``provenance.WRAPPED_FIELDS``); this document belongs to no track (one unit, 64 slots across
many tracks) and its ``path`` slots name machine-local files, so it cannot be a
``track_fields`` row and stays a local file, like the edge sampler's ``sampler.db``.

Mini-PRD:
  ✔︎ ✅ 🎯 an absent file reads as the default 4 x 16 empty banks
    [if] no file exists [then] read returns v 1, active_bank 0 and 64 empty slots [else ⛔️]
  ✔︎ ✅ 🎯 a written document reads back identically, atomically replaced
    [if] a document is written then read [then] it matches and no temp file is left [else ⛔️]
  ✔︎ ✅ 🎯 an invalid document is refused and nothing is written
    [if] a slot has an unknown mode or a gain outside -24..+6 [then] refused, file unchanged [else ⛔️]
  ✔︎ ✅ 🎯 the master gain is stored, defaults to 0 dB when absent and is refused outside -60..0
    [if] an old document has no master_gain_db [then] it reads as 0.0 [else ⛔️]
    [if] master_gain_db is -61 or +0.5 [then] refused, file unchanged [else ⛔️]
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Final

from apps.shared.atomic_text import write_atomic

FILENAME: Final = "sampler-banks.json"
VERSION: Final = 1
BANK_COUNT: Final = 4
SLOTS_PER_BANK: Final = 16
MODES: Final = ("one_shot", "loop", "gate")
GAIN_DB_MIN: Final = -24.0
GAIN_DB_MAX: Final = 6.0
MASTER_GAIN_DB_MIN: Final = -60.0
MASTER_GAIN_DB_MAX: Final = 0.0
MASTER_GAIN_DB_DEFAULT: Final = 0.0
_DOC_KEYS: Final = frozenset({"v", "active_bank", "banks"})
SOURCE_KEYS: Final = ("stable_id", "path")
_SLOT_KEYS: Final = frozenset({"mode", "gain_db", "title"})


def banks_path(state_db_path: Path) -> Path:
    """Path of ``sampler-banks.json``, beside the given ``state.db``."""
    return Path(state_db_path).parent / FILENAME


def default_document() -> dict[str, Any]:
    """The document an absent file stands for: bank 0 active, every slot empty."""
    return {
        "v": VERSION,
        "active_bank": 0,
        "master_gain_db": MASTER_GAIN_DB_DEFAULT,
        "banks": [[None] * SLOTS_PER_BANK for _ in range(BANK_COUNT)],
    }


# ---------- validation -------------------------------------------------------------


class InvalidBanksDocument(ValueError):
    """A banks document that breaks the shape, a mode or the gain range; nothing is stored."""


def _validated_slot(slot: Any, where: str) -> dict[str, Any] | None:
    if slot is None:
        return None
    if not isinstance(slot, dict):
        raise InvalidBanksDocument(f"{where}: a slot is an object or null, got {type(slot).__name__}")
    sources = [k for k in SOURCE_KEYS if k in slot]
    if len(sources) != 1:
        raise InvalidBanksDocument(f"{where}: a slot names exactly one of stable_id or path, got {sources}")
    source = sources[0]
    if set(slot) != _SLOT_KEYS | {source}:
        raise InvalidBanksDocument(f"{where}: slot keys must be {source}, mode, gain_db, title; got {sorted(slot)}")
    if not isinstance(slot[source], str) or not slot[source]:
        raise InvalidBanksDocument(f"{where}: {source} must be a non-empty string")
    if slot["mode"] not in MODES:
        raise InvalidBanksDocument(f"{where}: unknown mode {slot['mode']!r}, expected one of {MODES}")
    gain = slot["gain_db"]
    if isinstance(gain, bool) or not isinstance(gain, int | float) or not math.isfinite(gain):
        raise InvalidBanksDocument(f"{where}: gain_db must be a finite number, got {gain!r}")
    if not GAIN_DB_MIN <= gain <= GAIN_DB_MAX:
        raise InvalidBanksDocument(f"{where}: gain_db {gain} is outside {GAIN_DB_MIN}..{GAIN_DB_MAX} dB")
    if not isinstance(slot["title"], str):
        raise InvalidBanksDocument(f"{where}: title must be a string")
    return {source: slot[source], "mode": slot["mode"], "gain_db": float(gain), "title": slot["title"]}


def _validated_master_gain(gain: Any) -> float:
    if isinstance(gain, bool) or not isinstance(gain, int | float) or not math.isfinite(gain):
        raise InvalidBanksDocument(f"master_gain_db must be a finite number, got {gain!r}")
    if not MASTER_GAIN_DB_MIN <= gain <= MASTER_GAIN_DB_MAX:
        raise InvalidBanksDocument(f"master_gain_db {gain} is outside {MASTER_GAIN_DB_MIN}..{MASTER_GAIN_DB_MAX} dB")
    return float(gain)


def validated(doc: Any) -> dict[str, Any]:
    """The canonical form of a banks document, or ``InvalidBanksDocument`` naming the first thing wrong."""
    if not isinstance(doc, dict) or set(doc) not in (_DOC_KEYS, _DOC_KEYS | {"master_gain_db"}):
        raise InvalidBanksDocument("a banks document has the keys v, active_bank, banks and optionally master_gain_db")
    if doc["v"] != VERSION:
        raise InvalidBanksDocument(f"unsupported banks document version {doc['v']!r}, expected {VERSION}")
    active = doc["active_bank"]
    if isinstance(active, bool) or not isinstance(active, int) or not 0 <= active < BANK_COUNT:
        raise InvalidBanksDocument(f"active_bank must be an integer 0..{BANK_COUNT - 1}, got {active!r}")
    banks = doc["banks"]
    if not isinstance(banks, list) or len(banks) != BANK_COUNT:
        raise InvalidBanksDocument(f"banks must be a list of {BANK_COUNT} banks")
    out: list[list[dict[str, Any] | None]] = []
    for b, bank in enumerate(banks):
        if not isinstance(bank, list) or len(bank) != SLOTS_PER_BANK:
            raise InvalidBanksDocument(f"bank {b} must be a list of {SLOTS_PER_BANK} slots")
        out.append([_validated_slot(slot, f"bank {b} slot {s}") for s, slot in enumerate(bank)])
    master_gain = _validated_master_gain(doc.get("master_gain_db", MASTER_GAIN_DB_DEFAULT))
    return {"v": VERSION, "active_bank": active, "master_gain_db": master_gain, "banks": out}


# ---------- file ---------------------------------------------------------------------


def read_banks(state_db_path: Path) -> dict[str, Any]:
    """The stored banks document, the default one when the file is absent; a corrupt file raises."""
    path = banks_path(state_db_path)
    if not path.is_file():
        return default_document()
    return validated(json.loads(path.read_text(encoding="utf-8")))


def write_banks(state_db_path: Path, doc: Any) -> dict[str, Any]:
    """Validate the document, then replace the file atomically; returns what was stored."""
    canonical = validated(doc)
    write_atomic(banks_path(state_db_path), json.dumps(canonical, indent=1) + "\n")
    return canonical
