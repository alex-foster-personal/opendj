"""Open DJ's own cue store: hot cues, memory cues and loops (CUES-01).

Cues used to live only in rekordbox's ``djmdCue`` table, so a track with no
rekordbox mapping could not keep a single cue. They now live in ``state.db``
as the provenance-wrapped ``track_fields`` row ``cue_points`` that the state
layer reserved for them, which also puts them in the CloudSync set.

Ownership rule (JIK, Thu 1 Oct 2026: "our own, which should default to
including their rekordbox / other DJ app cues, duplicated on ingestion"):

* Ingest copies a DJ app's cues in with ``source`` set to that app
  (:func:`import_vendor_cues`). Re-ingesting keeps the copy current for as
  long as nobody has edited the track's cues in Open DJ.
* The first edit in Open DJ writes the whole set back with ``source='webui'``.
  From then on the set is Open DJ's, and ingest never overwrites it.
* A track ingested before this store existed has no row. Reads then fall
  back to the vendor cues the caller supplies, and the first edit seeds the
  row from them, so no rekordbox cue is lost by editing another slot.

The value is one JSON object per track::

    {"v": 1,
     "cues": [cue, ...],                      # every kind, sorted by in_ms
     "generations": {"A": 3, ...},            # per hot-cue slot, bumped per write
     "reversals": {"A": [reversal, ...]}}     # newest last, bounded

Revisions and reversal tokens keep the exact contract the rekordbox writer
had (``apps/adapters/rekordbox/writer.py``): an opaque CAS revision bound to
track, slot, slot generation and slot state, and single-use undo tokens that
only restore while the slot still holds the state their mutation produced.

This module owns no connection and opens no transaction. The caller holds
``BEGIN IMMEDIATE`` around a read-modify-write, exactly as the routes do.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

CUE_FIELD = "cue_points"
BLOB_VERSION = 1
HOT_CUE_SLOTS = "ABCDEFGH"
#: Source written by an edit made in Open DJ itself.
OWN_SOURCE: Final = "webui"
#: Undo tokens kept per slot. Older tokens answer HOT_CUE_REVERSAL_NOT_FOUND.
MAX_REVERSALS_PER_SLOT = 8

CUE_KINDS = ("hot_cue", "memory", "loop")

VendorCues = Callable[[], list[dict[str, Any]]]


class CueStoreError(Exception):
    """A refused cue operation, carrying the HTTP status and code it maps to."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        current_revision: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.current_revision = current_revision

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.current_revision is not None:
            out["current_revision"] = self.current_revision
        return out


class HotCueSlotError(ValueError):
    """Unknown hot-cue slot letter."""


@dataclass(frozen=True)
class StoredCues:
    """A track's cue set as the store resolved it."""

    stable_id: str
    cues: list[dict[str, Any]]
    generations: dict[str, int]
    reversals: dict[str, list[dict[str, Any]]]
    #: ``track_fields.source`` of the row, or ``None`` when there is no row
    #: and ``cues`` came from the vendor fallback.
    source: str | None


# ---------- normalization ---------------------------------------------------


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    return int(value)


def normalize_cue(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One cue in the COMPONENT-MAP 2.3 shape, minus the derived ``is_loop``.

    Accepts the rows ``fetch_cues`` returns for rekordbox, so an imported cue
    and an Open DJ cue are byte-identical when they describe the same thing.
    """
    kind = str(raw["kind"])
    if kind not in CUE_KINDS:
        raise ValueError(f"unknown cue kind {kind!r}")
    slot = raw.get("slot")
    if kind == "hot_cue":
        if not isinstance(slot, str) or len(slot) != 1 or slot not in HOT_CUE_SLOTS:
            raise ValueError(f"hot cue needs a slot in {HOT_CUE_SLOTS}, got {slot!r}")
    else:
        slot = None
    in_ms = _opt_int(raw.get("in_ms"))
    if in_ms is None or in_ms < 0:
        raise ValueError(f"cue in_ms must be a non-negative integer, got {raw.get('in_ms')!r}")
    out_ms = _opt_int(raw.get("out_ms"))
    if out_ms is not None and out_ms <= 0:
        out_ms = None
    return {
        "kind": kind,
        "slot": slot,
        "in_ms": in_ms,
        "out_ms": out_ms,
        "active_loop": bool(raw.get("active_loop")),
        "beat_loop_size": _opt_int(raw.get("beat_loop_size")),
        "color_table_index": _opt_int(raw.get("color_table_index")),
        "comment": raw.get("comment") or None,
    }


def _sorted(cues: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(c) for c in cues),
        key=lambda c: (c["in_ms"], c["kind"], c["slot"] or ""),
    )


def cue_view(cue: Mapping[str, Any]) -> dict[str, Any]:
    """The wire shape: the stored cue plus ``is_loop``."""
    out_ms = cue["out_ms"]
    is_loop = bool(out_ms is not None and out_ms > 0)
    return {**cue, "out_ms": out_ms if is_loop else None, "is_loop": is_loop}


def _slot_to_index(slot: str) -> int:
    if len(slot) != 1 or slot not in HOT_CUE_SLOTS:
        raise HotCueSlotError(f"unsupported hot-cue slot {slot!r}; only {HOT_CUE_SLOTS}")
    return HOT_CUE_SLOTS.index(slot)


# ---------- reading ---------------------------------------------------------


def read_row(conn: sqlite3.Connection, stable_id: str) -> tuple[dict[str, Any], str] | None:
    """The stored blob and its source, or ``None`` when the track has no row."""
    row = conn.execute(
        "SELECT value_json, source FROM track_fields "
        "WHERE stable_id = ? AND field_name = ?",
        (stable_id, CUE_FIELD),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    if not isinstance(value, dict) or value.get("v") != BLOB_VERSION:
        raise ValueError(
            f"cue_points for {stable_id} is not a v{BLOB_VERSION} cue set: {row[0][:80]!r}"
        )
    return value, str(row[1])


def _valid_vendor_cues(raw: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The vendor cues ``normalize_cue`` accepts, the rest dropped.

    Vendor rows are another app's data: one with no position or a slot we do
    not model must not turn the whole track's cue list into a 500. Own stored
    rows still raise, because a corrupt own row is ours to see.
    """
    cues: list[dict[str, Any]] = []
    for row in raw:
        try:
            cues.append(normalize_cue(row))
        except (KeyError, TypeError, ValueError):
            continue
    return cues


def load(
    conn: sqlite3.Connection,
    stable_id: str,
    vendor_cues: VendorCues | None = None,
) -> StoredCues:
    """The track's cue set: the stored row, else the vendor fallback."""
    stored = read_row(conn, stable_id)
    if stored is not None:
        blob, source = stored
        return StoredCues(
            stable_id=stable_id,
            cues=_sorted(normalize_cue(c) for c in blob.get("cues", [])),
            generations={str(k): int(v) for k, v in blob.get("generations", {}).items()},
            reversals={str(k): list(v) for k, v in blob.get("reversals", {}).items()},
            source=source,
        )
    fallback = vendor_cues() if vendor_cues is not None else []
    return StoredCues(
        stable_id=stable_id,
        cues=_sorted(_valid_vendor_cues(fallback)),
        generations={},
        reversals={},
        source=None,
    )


def stored_cues_view(conn: sqlite3.Connection, stable_id: str) -> list[dict[str, Any]] | None:
    """Wire-shape cues from the stored row, or ``None`` when there is none."""
    stored = read_row(conn, stable_id)
    if stored is None:
        return None
    blob, _source = stored
    return [cue_view(c) for c in _sorted(normalize_cue(c) for c in blob.get("cues", []))]


def _slot_cue(cues: list[dict[str, Any]], slot: str) -> dict[str, Any] | None:
    found = [c for c in cues if c["kind"] == "hot_cue" and c["slot"] == slot]
    if len(found) > 1:
        raise CueStoreError(
            500, "HOT_CUE_SLOT_CORRUPT", f"slot {slot} holds {len(found)} cues"
        )
    return found[0] if found else None


def revision(stable_id: str, slot: str, generation: int, cue: Mapping[str, Any] | None) -> str:
    """Opaque CAS token bound to track, slot, slot generation and exact state."""
    encoded = json.dumps(
        {"stable_id": stable_id, "slot": slot, "generation": generation, "cue": cue},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _slot_revision(state: StoredCues, slot: str) -> str:
    return revision(
        state.stable_id, slot, state.generations.get(slot, 0), _slot_cue(state.cues, slot)
    )


def hot_cue_slots(state: StoredCues) -> list[dict[str, Any]]:
    """All eight slots with their revisions, empty slots included."""
    out = []
    for slot in HOT_CUE_SLOTS:
        cue = _slot_cue(state.cues, slot)
        rev = _slot_revision(state, slot)
        out.append(
            {
                "slot": slot,
                "cue": {**cue_view(cue), "revision": rev} if cue else None,
                "revision": rev,
            }
        )
    return out


# ---------- writing ---------------------------------------------------------


def _blob(state: StoredCues) -> dict[str, Any]:
    return {
        "v": BLOB_VERSION,
        "cues": _sorted(state.cues),
        "generations": dict(sorted(state.generations.items())),
        "reversals": dict(sorted(state.reversals.items())),
    }


def _require_revision(expected: str, current: str) -> None:
    if not expected:
        raise CueStoreError(
            428, "HOT_CUE_REVISION_REQUIRED", "hot-cue mutation requires an If-Match revision"
        )
    if expected != current:
        raise CueStoreError(
            409,
            "HOT_CUE_REVISION_CONFLICT",
            "hot-cue slot changed since it was read",
            current_revision=current,
        )


def _validate_position(in_ms: int, duration_ms: int | None) -> None:
    if isinstance(in_ms, bool) or not isinstance(in_ms, int) or in_ms < 0:
        raise CueStoreError(422, "INVALID_CUE_POSITION", "in_ms must be a non-negative integer")
    if duration_ms is not None and in_ms > duration_ms:
        raise CueStoreError(
            422,
            "INVALID_CUE_POSITION",
            f"in_ms must satisfy 0 <= in_ms <= {duration_ms}, got {in_ms}",
        )


def _reversal_id(stable_id: str, slot: str) -> str:
    scope = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12]
    return f"{scope}.{slot}.{secrets.token_hex(12)}"


def _mutate_slot(
    state: StoredCues,
    slot: str,
    new_cue: dict[str, Any] | None,
    *,
    record_reversal: bool,
) -> tuple[StoredCues, str, str | None]:
    """Replace one slot. Returns the new state, its revision, and a reversal id."""
    stable_id = state.stable_id
    preimage = _slot_cue(state.cues, slot)
    cues = [c for c in state.cues if not (c["kind"] == "hot_cue" and c["slot"] == slot)]
    if new_cue is not None:
        cues.append(new_cue)
    generations = {**state.generations, slot: state.generations.get(slot, 0) + 1}
    post_revision = revision(stable_id, slot, generations[slot], new_cue)
    reversals = {k: list(v) for k, v in state.reversals.items()}
    reversal_id = None
    if record_reversal:
        reversal_id = _reversal_id(stable_id, slot)
        history = reversals.get(slot, [])
        history.append(
            {
                "id": reversal_id,
                "preimage": preimage,
                "post_revision": post_revision,
                "consumed": False,
            }
        )
        reversals[slot] = history[-MAX_REVERSALS_PER_SLOT:]
    new_state = StoredCues(
        stable_id=stable_id,
        cues=_sorted(cues),
        generations=generations,
        reversals=reversals,
        source=OWN_SOURCE,
    )
    return new_state, post_revision, reversal_id


WriteBlob = Callable[[dict[str, Any]], None]


def save_hot_cue(
    state: StoredCues,
    slot: str,
    in_ms: int,
    *,
    expected_revision: str,
    comment: str | None,
    color_table_index: int | None,
    duration_ms: int | None,
    write: WriteBlob,
) -> dict[str, Any]:
    """CAS-save a hot cue. ``write`` persists the new blob inside the caller's tx."""
    _slot_to_index(slot)
    _validate_position(in_ms, duration_ms)
    _require_revision(expected_revision, _slot_revision(state, slot))
    cue = normalize_cue(
        {
            "kind": "hot_cue",
            "slot": slot,
            "in_ms": in_ms,
            "color_table_index": color_table_index,
            "comment": comment,
        }
    )
    new_state, rev, reversal_id = _mutate_slot(state, slot, cue, record_reversal=True)
    write(_blob(new_state))
    return {
        "cue": {**cue_view(cue), "revision": rev},
        "revision": rev,
        "reversal": {"reversal_id": reversal_id},
    }


def clear_hot_cue(
    state: StoredCues,
    slot: str,
    *,
    expected_revision: str,
    write: WriteBlob,
) -> dict[str, Any]:
    """CAS-clear a hot cue (a no-op on an empty slot still bumps its generation)."""
    _slot_to_index(slot)
    _require_revision(expected_revision, _slot_revision(state, slot))
    new_state, rev, reversal_id = _mutate_slot(state, slot, None, record_reversal=True)
    write(_blob(new_state))
    return {"cue": None, "revision": rev, "reversal": {"reversal_id": reversal_id}}


def _unconsumed_reversal(state: StoredCues, slot: str, reversal_id: str) -> dict[str, Any]:
    """The reversal entry a token names, checked for scope and single use."""
    scope = hashlib.sha256(state.stable_id.encode("utf-8")).hexdigest()[:12]
    parts = reversal_id.split(".")
    if len(parts) != 3 or parts[0] != scope or parts[1] != slot:
        raise CueStoreError(
            409,
            "HOT_CUE_REVERSAL_SCOPE_CONFLICT",
            "reversal token belongs to another track or slot",
        )
    entry = next((r for r in state.reversals.get(slot, []) if r["id"] == reversal_id), None)
    if entry is None:
        raise CueStoreError(404, "HOT_CUE_REVERSAL_NOT_FOUND", "unknown reversal token")
    if entry["consumed"]:
        raise CueStoreError(409, "HOT_CUE_REVERSAL_CONSUMED", "reversal token already used")
    return entry


def restore_hot_cue(
    state: StoredCues,
    slot: str,
    *,
    expected_revision: str,
    reversal_id: str,
    write: WriteBlob,
) -> dict[str, Any]:
    """CAS-restore one single-use reversal token."""
    _slot_to_index(slot)
    entry = _unconsumed_reversal(state, slot, reversal_id)
    current = _slot_revision(state, slot)
    _require_revision(expected_revision, current)
    if entry["post_revision"] != current:
        raise CueStoreError(
            409,
            "HOT_CUE_REVERSAL_STALE",
            "hot-cue slot changed after the reversible mutation",
            current_revision=current,
        )
    preimage = entry["preimage"]
    restored = normalize_cue(preimage) if preimage is not None else None
    new_state, rev, _ = _mutate_slot(state, slot, restored, record_reversal=False)
    new_state.reversals[slot] = [
        {**r, "consumed": True} if r["id"] == reversal_id else r
        for r in new_state.reversals.get(slot, [])
    ]
    write(_blob(new_state))
    return {
        "cue": {**cue_view(restored), "revision": rev} if restored else None,
        "revision": rev,
    }


# ---------- ingest ----------------------------------------------------------


def import_vendor_cues(
    set_field: Callable[..., bool],
    conn: sqlite3.Connection,
    stable_id: str,
    cues: Iterable[Mapping[str, Any]],
    *,
    source: str,
    modified_at: str,
) -> bool:
    """Copy a DJ app's cues in at ingest. Returns True when the row changed.

    ``set_field`` is ``StateWriter.set_field``. The copy is skipped when the
    track's cues already belong to Open DJ or to a different app, and when the
    track has no row and the app has no cues (nothing to copy).
    """
    normalized = _sorted(normalize_cue(c) for c in cues)
    existing = read_row(conn, stable_id)
    if existing is None:
        if not normalized:
            return False
        blob = {"v": BLOB_VERSION, "cues": normalized, "generations": {}, "reversals": {}}
    else:
        current, current_source = existing
        if current_source != source:
            return False
        blob = {**current, "cues": normalized}
    return bool(
        set_field(stable_id, CUE_FIELD, blob, source=source, modified_at=modified_at)
    )


__all__ = [
    "BLOB_VERSION",
    "CUE_FIELD",
    "HOT_CUE_SLOTS",
    "OWN_SOURCE",
    "CueStoreError",
    "HotCueSlotError",
    "StoredCues",
    "VendorCues",
    "WriteBlob",
    "clear_hot_cue",
    "cue_view",
    "hot_cue_slots",
    "import_vendor_cues",
    "load",
    "normalize_cue",
    "read_row",
    "restore_hot_cue",
    "revision",
    "save_hot_cue",
    "stored_cues_view",
]
