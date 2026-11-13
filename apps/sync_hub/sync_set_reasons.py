"""Sync-set hold reasons, the FK graph they follow, and how a report reads them.

Split out of :mod:`apps.sync_hub.sync_set` (issue #9725, parent #1390) and
re-exported there. The reason strings, :data:`PARENT_KEYS`, :data:`FK_ORDER`
and the classifiers over a reason (:func:`exclusion_kind`, :func:`hold_group`,
:func:`remedy_for`, :func:`is_settled`) read no database, so every other
``sync_set_*`` module can import them without a cycle.
"""

from __future__ import annotations

from typing import Literal

from apps.sync_hub.protocol_common import MEMBERSHIP_TABLE, SYNC_TABLES

IDENTITY_HOLD_REASON: str = (
    "inferred-tier track has no content_hash, no audio_hash, and no "
    "normalizable ISRC; offering it would mint a path-tier PK the hub "
    "cannot collapse"
)
IDENTITY_DUP_REASON: str = "content-identity duplicate; the LWW survivor is another tracks row"
#: A duplicate the HUB has already answered for (a persisted
#: ``sync_identity_remap`` names it a loser, CLOUDSYNC-32). Still never
#: offered, but SETTLED: not counted as held and never a fence pin, because
#: no repair on this machine will ever make it travel under its own id.
IDENTITY_SETTLED_REASON: str = "content-identity duplicate the hub already collapsed into another tracks row; settled"
#: The fixed tail of a child of a settled duplicate: it stays with its parent.
SETTLED_PARENT_SUFFIX: str = "is a settled content-identity duplicate"
#: The fixed tail of every "held because its parent is held" reason, and the
#: fixed head of every "held because a member is held" one. Only the one
#: row's parent or member varies between them, which is what
#: :func:`hold_group` takes out.
PARENT_HELD_SUFFIX: str = (
    "is itself held back, and offering a child without its parent is a FOREIGN KEY 409 no retry gets past"
)
MEMBER_HELD_PREFIX: str = "a track in it is not in the sync set"
#: The fixed tail of every "held because this machine no longer stores its
#: parent" reason. SQLite enforces a foreign key only on a connection that
#: turned it on, so a parent deleted with ``foreign_keys=OFF`` leaves its
#: children behind, and offering one is the same FOREIGN KEY 409 (silver, Thu
#: 5 Nov 2026: 209 ``track_vendor_ids`` rows under deleted ``spotify-pending:*``
#: tracks wedged every push).
PARENT_MISSING_SUFFIX: str = (
    "is not stored on this machine, and offering a child without its parent is a FOREIGN KEY 409 no retry gets past"
)
#: What :func:`_stored_row_reason` answers for a parent key no row stores.
#: Internal: :class:`HeldKeys` turns it into :data:`PARENT_MISSING_SUFFIX` on
#: the child, so it never reaches a report.
_MISSING_ROW: str = "no row stores this key"

#: Every parent key a sync-set row must not outrun, as
#: ``child table -> ((parent table, referencing column), ...)``. Mirrors the
#: ``REFERENCES`` clauses in ``apps/shared/state/schema.py``; pinned against
#: them by ``(private path)``.
PARENT_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "lyric_verdict": (("tracks", "stable_id"),),
    "track_vendor_ids": (("tracks", "stable_id"),),
    "track_fields": (("tracks", "stable_id"),),
    "track_locations": (("tracks", "stable_id"),),
    "track_edit": (("tracks", "source_track_id"),),
    "playlist_pins": (("playlists", "playlist_id"),),
    MEMBERSHIP_TABLE: (
        ("track_edit", "edit_id"),
        ("playlists", "playlist_id"),
        ("tracks", "stable_id"),
    ),
}

#: Every digest/offer table, parents before children. ``SYNC_TABLES`` is
#: already the FK-safe apply order (``track_edit`` before ``playlists``);
#: membership goes last because it references ``tracks``, ``playlists``, and
#: ``track_edit``. Any walk that decides exclusions MUST use this order, or a
#: child is judged before its parent is known to be held.
FK_ORDER: tuple[str, ...] = (
    *(spec.name for spec in SYNC_TABLES),
    MEMBERSHIP_TABLE,
)


def is_settled(reason: str | None) -> bool:
    """True when ``reason`` excludes a row the hub already settled (CLOUDSYNC-32).

    Such a row is still never offered, but it is not HELD: it is not counted
    as excluded, not re-logged after a full offer and not a push-fence pin.
    """
    return reason is not None and exclusion_kind(reason) == "identity_settled"


def hold_group(reason: str) -> str:
    """``reason`` with the one row's own parent or member taken out.

    Rows held for the same cause share this, so a report can count them
    rather than print each: the parent's id alone made silver's 7,331 held
    ``tracks`` parents into as many distinct reasons, and one ERROR line per
    reason per sync pass. Every other reason is its own group verbatim, so
    an offending stored value still reaches the log.
    """
    if reason.startswith(MEMBER_HELD_PREFIX):
        return MEMBER_HELD_PREFIX
    for suffix in (PARENT_HELD_SUFFIX, PARENT_MISSING_SUFFIX):
        if reason.startswith("its ") and reason.endswith(suffix):
            parent_table = reason.split(" ", 2)[1]
            return f"its {parent_table} parent {suffix}"
    return reason


ExclusionKind = Literal[
    "stamp",
    "identity_hold",
    "identity_dup",
    "identity_settled",
    "parent_held",
    "parent_missing",
    "membership_held",
]


def exclusion_kind(reason: str) -> ExclusionKind:
    """Classify one exclusion reason for operator messaging."""
    if IDENTITY_SETTLED_REASON in reason or reason.endswith(SETTLED_PARENT_SUFFIX):
        return "identity_settled"
    if IDENTITY_HOLD_REASON in reason:
        return "identity_hold"
    if IDENTITY_DUP_REASON in reason:
        return "identity_dup"
    if reason.startswith(MEMBER_HELD_PREFIX):
        return "membership_held"
    if reason.startswith("its ") and PARENT_HELD_SUFFIX in reason:
        return "parent_held"
    if reason.startswith("its ") and PARENT_MISSING_SUFFIX in reason:
        return "parent_missing"
    if ".updated_at" in reason or ".deleted_at" in reason:
        return "stamp"
    return "stamp"


def remedy_for(reason: str) -> str:
    """Operator next step for one hold reason; normalize_stamps only for stamps."""
    kind = exclusion_kind(reason)
    if kind == "identity_hold":
        return "it carries identity (`python -m apps.shared.state.backfill_content_hash --live` or `/fix-links`)"
    if kind == "identity_dup":
        return "the LWW survivor of this content identity is offered instead"
    if kind == "identity_settled":
        return "nothing to repair: the hub keeps the survivor of this content identity"
    if kind == "parent_missing":
        return (
            "the parent row was deleted on this machine without its children; "
            "the child stays local until its parent is stored again"
        )
    if kind in ("parent_held", "membership_held"):
        return (
            f"the root hold must be cleared first ({hold_group(reason)}); "
            "see the group's ERROR line for the root remedy"
        )
    return "it is repaired with `python -m apps.shared.state.normalize_stamps --live`"
