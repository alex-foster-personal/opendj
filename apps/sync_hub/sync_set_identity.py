"""Content-identity keys, the LWW duplicate election, and the per-track identity hold.

Split out of :mod:`apps.sync_hub.sync_set` (issue #9725, parent #1390) and
re-exported there. :class:`apps.sync_hub.sync_set_held.HeldKeys` asks
:func:`_track_identity_reason` about every ``tracks`` row it judges, and
:mod:`apps.sync_hub.identity_verdicts` elects losers with the same keys.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.shared.state.ids import normalise_isrc
from apps.sync_hub.protocol_common import (
    ORIGIN_DEVICE_ID,
    UPDATED_AT,
    is_hash_pending_candidate,
    lww_key,
    stored_stamp_faults,
    table_columns,
)
from apps.sync_hub.sync_set_reasons import IDENTITY_HOLD_REASON

if TYPE_CHECKING:
    from apps.sync_hub.sync_set_held import HeldKeys


def _invert_pk(pk: str) -> str:
    """Sort key so ``max`` prefers the smaller PK on a stamp tie."""
    return "".join(chr(255 - ord(ch)) for ch in pk)


IdentityRow = Sequence[Any]
"""``(stable_id, content_hash, audio_hash, isrc, updated_at, origin_device_id)``."""


@dataclass(frozen=True)
class IdentityKeys:
    """The election keys one ``tracks`` row files under, as hub collapse reads them."""

    hashes: tuple[str, ...]
    isrc: str | None


def identity_keys(content_hash: object, audio_hash: object, isrc: object) -> IdentityKeys:
    """``content_hash`` then a distinct ``audio_hash``; a normalizable ISRC only without either."""
    digest = str(content_hash).strip() if content_hash else ""
    audio_digest = str(audio_hash).strip() if audio_hash else ""
    hashes = tuple(key for key in (digest, audio_digest if audio_digest != digest else "") if key)
    norm = normalise_isrc(None if isrc is None else str(isrc))
    return IdentityKeys(hashes=hashes, isrc=None if hashes else norm)


def identity_row_select(conn: sqlite3.Connection) -> str:
    """``SELECT`` of :data:`IdentityRow` over live tracks, before any further filter."""
    audio_column = "audio_hash" if "audio_hash" in table_columns(conn, "tracks") else "NULL AS audio_hash"
    return (
        f"SELECT stable_id, content_hash, {audio_column}, isrc, "
        "updated_at, origin_device_id FROM tracks WHERE deleted_at IS NULL"
    )


def library_has_identity_keys(conn: sqlite3.Connection) -> bool:
    """True when any live ``tracks`` row may participate in a content-identity election.

    A library of inferred rows with no ``content_hash``, ``audio_hash``, or
    ISRC elects nothing (#11267): skipping the full-table read keeps offer,
    ``HeldKeys``, and ``prepare_spoke_identity`` O(1) per row at 10k scale.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    audio_arm = "OR (TRIM(COALESCE(audio_hash, '')) != '')" if "audio_hash" in columns else ""
    row = conn.execute(
        "SELECT 1 FROM tracks WHERE deleted_at IS NULL AND ("
        "TRIM(COALESCE(content_hash, '')) != '' "
        f"{audio_arm} "
        "OR TRIM(COALESCE(isrc, '')) != ''"
        ") LIMIT 1"
    ).fetchone()
    return row is not None


def identity_duplicate_remap(conn: sqlite3.Connection) -> dict[str, str]:
    """Loser PK -> LWW survivor PK for live tracks sharing a merge key.

    Same keys as hub collapse: ``content_hash`` or ``audio_hash`` first, then a normalizable
    ISRC. Path-tier ``stable_id`` is never a merge key. Groups whose identity
    signals conflict (same hash, disagreeing ISRC) are left unmapped --
    apply-time quarantine owns those.
    """
    if not library_has_identity_keys(conn):
        return {}
    return identity_remap_from_rows(conn.execute(identity_row_select(conn)).fetchall())


def identity_remap_from_rows(rows: Iterable[IdentityRow]) -> dict[str, str]:
    """:func:`identity_duplicate_remap` over ``rows`` alone.

    Exact for every row whose identity keys no row outside ``rows`` shares.
    """
    by_hash: dict[str, list[tuple[str, str | None, tuple[str, str]]]] = {}
    by_isrc: dict[str, list[tuple[str, str | None, tuple[str, str]]]] = {}
    stamp_cols = (UPDATED_AT, ORIGIN_DEVICE_ID)
    for pk, content_hash, audio_hash, isrc, updated_at, origin in rows:
        if stored_stamp_faults("tracks", stamp_cols, (updated_at, origin)):
            continue
        sid = str(pk)
        norm = normalise_isrc(None if isrc is None else str(isrc))
        key = lww_key({UPDATED_AT: updated_at, ORIGIN_DEVICE_ID: origin})
        keys = identity_keys(content_hash, audio_hash, isrc)
        for digest in keys.hashes:
            by_hash.setdefault(digest, []).append((sid, norm, key))
        if keys.isrc:
            by_isrc.setdefault(keys.isrc, []).append((sid, None, key))
    remap: dict[str, str] = {}
    _add_identity_group_remaps(by_hash.values(), remap, preserve_existing=False)
    _add_identity_group_remaps(by_isrc.values(), remap, preserve_existing=True)
    return remap


def _add_identity_group_remaps(
    groups: Iterable[list[tuple[str, str | None, tuple[str, str]]]],
    remap: dict[str, str],
    *,
    preserve_existing: bool,
) -> None:
    """Add safe loser-to-champion mappings for one identity-key family."""
    for group in groups:
        if len(group) < 2:
            continue
        values = {item[1] for item in group if item[1]}
        if len(values) > 1:
            continue
        champion = max(group, key=lambda item: (item[2], _invert_pk(item[0])))
        for sid, _, _ in group:
            if sid != champion[0] and (not preserve_existing or sid not in remap):
                remap[sid] = champion[0]


def _track_identity_reason(table: str, raw: Mapping[str, Any], held: HeldKeys) -> str | None:
    """Why this ``tracks`` row is held for identity, or None."""
    if table != "tracks":
        return None
    if is_hash_pending_candidate("tracks", raw):
        return None
    pk = str(raw.get("stable_id") or "")
    loser = held.loser_reason(pk)
    if loser is not None:
        return loser
    if str(raw.get("stable_id_tier") or "") != "inferred":
        return None
    digest = raw.get("content_hash")
    if digest is not None and str(digest).strip():
        return None
    audio_digest = raw.get("audio_hash")
    if audio_digest is not None and str(audio_digest).strip():
        return None
    if normalise_isrc(None if raw.get("isrc") is None else str(raw.get("isrc"))):
        return None
    return IDENTITY_HOLD_REASON


def is_hash_pending_track(raw: Mapping[str, Any], held: HeldKeys | None = None) -> bool:
    """True when this live ``tracks`` row should travel with ``hash_pending``."""
    if not is_hash_pending_candidate("tracks", raw):
        return False
    if held is None or not held.identity_elections_enabled:
        return True
    pk = str(raw.get("stable_id") or "")
    return not held.is_identity_loser(pk)
