"""The hub's generation token: how a spoke knows the hub went backwards.

Contract: ``specs/design_decision_08.md`` point 4, amended by round 2
finding N6. Round 2 inferred "the hub was restored" from its ``MAX(seq)``
going backwards, which is true of a Litestream point-in-time restore and
ALSO true of any maintenance that prunes the changelog -- so a routine
prune made every spoke log an ERROR and re-offer its whole library over the
tailnet (minutes per 8k tracks, ADR 08 consequence 3).

The signal is a token instead. ``hello`` reports it; a spoke that sees a
DIFFERENT token than the one it stored concludes the hub is a different
lineage and resets both floors. A prune does not change the token, so it
cannot look like a restore.

**Why the token lives in a file, not in the DB.** A restore rolls the whole
database back, token included, so an in-DB token cannot notice its own
rollback: the restored snapshot carries whatever token it carried when the
snapshot was taken. The anchor has to sit outside the file Litestream
replaces. So it lives beside ``machine-id``, for exactly the reason ADR 05
section 1 puts identity there: a DB restored onto another machine must not
inherit what the file asserts.

The anchor records the greatest ``hub_changelog.seq`` this hub has ever
reported. Three states, no fourth:

* no anchor -- first run (or the data dir was rebuilt): mint a token.
* anchor at or below the DB's seq -- ordinary operation: keep the token,
  move the high-water mark up.
* anchor ABOVE the DB's seq -- the database moved backwards underneath a
  data dir that did not: mint a new token, loudly.

The sanctioned prune (:func:`apps.sync_hub.engine.prune_changelog`) never
removes the newest entry, so it cannot lower the DB's seq and cannot
trigger the third case. A prune that DOES delete the newest entry has
destroyed the hub's ability to answer any spoke's pull, and a full re-offer
is then the repair, not a false alarm.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from apps.sync_hub import engine

log = logging.getLogger(__name__)

#: Beside ``machine-id`` in the data dir. Not in ``state/`` -- Litestream
#: replicates the DB directory, and an anchor that travels with the thing it
#: is anchoring is not an anchor.
GENERATION_FILENAME: str = "hub-generation.json"


class SyncGenerationError(RuntimeError):
    """The generation anchor could not be read or written. Never guessed."""


@dataclass(frozen=True)
class Anchor:
    """What the anchor file asserts about this hub."""

    generation: str
    high_water: int


def anchor_path(data_dir: Path) -> Path:
    """Location of the anchor file for ``data_dir``."""
    return Path(data_dir) / GENERATION_FILENAME


def _mint() -> str:
    return uuid.uuid4().hex


def _read(path: Path) -> Anchor | None:
    """Parse the anchor file, or None when it does not exist yet.

    Anything present but unreadable raises: a corrupt anchor silently
    replaced would re-mint a token and send every spoke into a full
    re-offer, which is exactly the cost this module exists to avoid.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SyncGenerationError(f"cannot read the hub generation file {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncGenerationError(
            f"{path} is not JSON: {exc}. Delete it to re-mint, accepting that "
            f"every spoke will then re-offer its whole library once."
        ) from exc
    if not isinstance(parsed, dict):
        raise SyncGenerationError(f"{path} holds {type(parsed).__name__}, want an object")
    generation = parsed.get("generation")
    high_water = parsed.get("high_water")
    if not isinstance(generation, str) or len(generation) != 32:
        raise SyncGenerationError(
            f"{path} does not hold a 32-char generation token (got "
            f"{generation!r}); refusing to guess."
        )
    if not isinstance(high_water, int) or isinstance(high_water, bool) or high_water < 0:
        raise SyncGenerationError(
            f"{path} does not hold a non-negative integer high_water (got "
            f"{high_water!r}); refusing to guess."
        )
    return Anchor(generation=generation, high_water=high_water)


def _write(path: Path, anchor: Anchor) -> None:
    """Replace the anchor file atomically, or raise."""
    payload = json.dumps(
        {"generation": anchor.generation, "high_water": anchor.high_water},
        sort_keys=True,
    )
    temp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise SyncGenerationError(
            f"cannot write the hub generation file {path}: {exc}"
        ) from exc


def observe(conn: sqlite3.Connection, data_dir: Path) -> str:
    """This hub's generation, re-minted if the DB moved backwards.

    Call it wherever the hub reports or advances its seq: ``hello`` (which
    returns the token) and ``push`` (which advances the seq the anchor
    tracks). Cheap -- one small file read, and a write only when the
    high-water mark actually moves.

    MUST be called after the writing transaction has committed. An anchor
    written inside a transaction that later rolls back would sit ABOVE the
    DB's seq and every subsequent call would read that as a restore.
    """
    path = anchor_path(Path(data_dir))
    seq = engine.current_seq(conn)
    anchor = _read(path)
    if anchor is None:
        minted = Anchor(generation=_mint(), high_water=seq)
        _write(path, minted)
        log.info(
            "minted hub generation %s at seq %d (%s)", minted.generation, seq, path
        )
        return minted.generation
    if anchor.high_water > seq:
        rotated = Anchor(generation=_mint(), high_water=seq)
        _write(path, rotated)
        log.error(
            "this hub's changelog is at seq %d but %s recorded %d: the "
            "database moved BACKWARDS (restored from a Litestream point in "
            "time?). Rotating the generation %s -> %s; every spoke will "
            "reset its sync floors and re-offer its library once.",
            seq,
            path,
            anchor.high_water,
            anchor.generation,
            rotated.generation,
        )
        return rotated.generation
    if seq > anchor.high_water:
        _write(path, Anchor(generation=anchor.generation, high_water=seq))
    return anchor.generation


def rotate(data_dir: Path, *, seq: int = 0) -> str:
    """Force a new generation. Returns the token that replaces the old one.

    For the restore runbook: a restore that also rolled the data dir back
    (a whole-machine restore, not a Litestream DB restore) leaves the anchor
    consistent with the DB, so nothing detects it. Rotating by hand is then
    the signal, and it is idempotent enough to run whenever in doubt -- the
    cost is one full re-offer per spoke.
    """
    path = anchor_path(Path(data_dir))
    previous = _read(path)
    rotated = Anchor(generation=_mint(), high_water=seq)
    _write(path, rotated)
    log.warning(
        "hub generation rotated %s -> %s at seq %d",
        "none" if previous is None else previous.generation,
        rotated.generation,
        seq,
    )
    return rotated.generation


__all__ = [
    "GENERATION_FILENAME",
    "Anchor",
    "SyncGenerationError",
    "anchor_path",
    "observe",
    "rotate",
]
