"""Reconcile this machine's feedback pin store with the synced ``feedback_pins``
table (FBSYNC-01..FBSYNC-05, ADR-0013).

``comments.json`` (plus the ``archive-*.json`` files beside it) stays the
offline-first store of record: every feedback route keeps reading and writing
it exactly as before, and none of them needs CloudSync to be reachable. This
module is the bridge between that store and the ``feedback_pins`` table, which
IS in the CloudSync sync set and therefore rides the same push fence, pull
watermark, digest and last-writer-wins merge as ``playlist_pins``. There is no
second sync channel.

One rule decides every pin, in both directions:

* Each side of a pin has a last-writer-wins key ``(updated_at,
  origin_device_id)``. The local side's ``updated_at`` is the pin's own edit
  time (``updated_at``, else ``created_at``); its origin is this machine.
* Equal docs: nothing to do, whatever the keys say. This is what stops a pin
  that was just pulled from being re-exported under this machine's id.
* Different docs: the greater key wins, and the winner is written over the
  loser. A local win becomes a stamped ``feedback_pins`` row plus a
  ``local_changelog`` entry (so the next push offers it); a row win is written
  into ``comments.json`` or, for a tombstone, the archive.

A pin present on only one side is copied to the other. Absence NEVER means
deleted: archive is the explicit record, either ``status: archived`` or a
pin's presence in an ``archive-*.json`` file, and on the table side the
``deleted_at`` tombstone. That is what makes "a pin on either side after a
sync exists on both" hold.

Every read-modify-write of ``comments.json`` here holds ``_COMMENTS_LOCK``,
the same lock every feedback route holds, so a reconcile cannot interleave
with a pin being dropped or patched.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apps.shared.state import sync_stamp
from apps.sync_hub.protocol_common import canonical_bytes, nfc

from .feedback import (
    _COMMENTS_FILE,
    _COMMENTS_LOCK,
    CommentOut,
    _load,
    _save,
    keep_unknown_fields,
)
from .feedback_attachments import _EXT_BY_CONTENT_TYPE
from .feedback_pins import _append_to_archive

log = logging.getLogger(__name__)

FEEDBACK_PINS_TABLE: str = "feedback_pins"
ARCHIVED: str = "archived"

#: ``harvested``: this machine's bulk harvest took the pin off its own board.
#: That is local only (ADR-0013): other machines keep the pin.
PinSyncState = Literal["synced", "pending_push", "unreconciled", "harvested"]
AttachmentBytes = Literal["none", "present", "missing"]


class FeedbackReplicaError(RuntimeError):
    """A pin store or row the bridge cannot read as a pin. Never swallowed."""


# ----- the two sides ---------------------------------------------------------


@dataclass(frozen=True)
class PinVersion:
    """One side's copy of one pin: its doc, LWW key and archive flag.

    ``harvested`` marks a local copy that only the bulk harvest
    (``POST /feedback/archive``) moved into an archive file. It hides the pin
    on this machine and is never exported: only the per-pin archive route
    produces a synced tombstone (PR #1978 review, ADR-0013).
    """

    doc: dict[str, Any]
    doc_text: str
    updated_at: str
    origin_device_id: str
    archived: bool
    harvested: bool = False

    def lww_key(self) -> tuple[str, str]:
        return (self.updated_at, self.origin_device_id)


@dataclass(frozen=True)
class ReconcileResult:
    """What one reconcile moved. ``exported`` rows now wait for the next push."""

    exported: int
    imported: int
    unchanged: int


def canonical_doc_text(doc: dict[str, Any]) -> str:
    """The one serialization both sides compare and the row stores.

    NFC-normalized because the sync wire NFC-normalizes every non-key string,
    so a doc that has crossed a hub always comes back normalized; comparing
    un-normalized text would call two identical pins different forever.
    """
    return nfc(canonical_bytes(doc).decode("utf-8"))


def _normalized(doc: dict[str, Any]) -> dict[str, Any]:
    """The pin as ``CommentOut`` serializes it, keeping every field it does not know.

    Older pins gain this build's defaults once. Fields a NEWER build added are
    kept verbatim: dropping them would strip them from every machine on the
    next export, or, on a tie, make this machine re-import the pin forever
    (PR #1978 review). Both sides of a comparison go through here.
    """
    return keep_unknown_fields(doc, CommentOut.model_validate(doc).model_dump(mode="json"))


def _edit_stamp(doc: dict[str, Any]) -> str:
    raw = doc.get("updated_at") or doc.get("created_at")
    if not isinstance(raw, str) or not raw:
        raise FeedbackReplicaError(
            f"pin {doc.get('id')!r} has neither updated_at nor created_at, so "
            f"it cannot be ordered against another machine's copy"
        )
    return sync_stamp.to_canonical(raw)


def _version(
    doc: dict[str, Any],
    origin: str,
    *,
    archived: bool,
    harvested: bool = False,
    updated_at: str | None = None,
) -> PinVersion:
    normalized = _normalized(doc)
    return PinVersion(
        doc=normalized,
        doc_text=canonical_doc_text(normalized),
        updated_at=_edit_stamp(normalized) if updated_at is None else updated_at,
        origin_device_id=origin,
        archived=archived,
        harvested=harvested,
    )


def _newer(current: PinVersion | None, candidate: PinVersion) -> PinVersion:
    if current is None or candidate.lww_key() > current.lww_key():
        return candidate
    return current


def _archived_versions(root: Path, machine_id: str) -> dict[str, PinVersion]:
    """Every pin an archive file records, newest copy per id.

    A pin archived through ``POST /comments/{id}/archive`` carries
    ``status: archived`` and its own archive time: that is a tombstone. One
    moved by the bulk harvest (``POST /feedback/archive``) keeps whatever
    status it had: that is a local ``harvested`` copy, ordered at the file's
    ``archived_at``, and it never becomes a tombstone.
    """
    versions: dict[str, PinVersion] = {}
    for path, comment, archived_at in _archive_entries(root):
        if comment.get("status") == ARCHIVED:
            version = _version(comment, machine_id, archived=True)
        else:
            # Agent bulk harvest (issue #3981): archive file is provenance only;
            # live pins stay on the board, so this must not export as a tombstone.
            version = _version(
                comment, machine_id, archived=False, harvested=True,
                updated_at=sync_stamp.to_canonical(_harvest_stamp(path, archived_at)),
            )
        versions[version.doc["id"]] = _newer(versions.get(version.doc["id"]), version)
    return versions


def _archive_entries(root: Path) -> list[tuple[Path, dict[str, Any], object]]:
    """Every pin in every archive file, with its file and that file's ``archived_at``."""
    entries: list[tuple[Path, dict[str, Any], object]] = []
    for path in sorted(root.glob("archive-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        comments = payload.get("comments") if isinstance(payload, dict) else None
        if not isinstance(comments, list):
            raise FeedbackReplicaError(f"{path} does not hold a 'comments' list")
        entries.extend((path, comment, payload.get("archived_at")) for comment in comments)
    return entries


def _harvest_stamp(path: Path, archived_at: object) -> str:
    if not isinstance(archived_at, str) or not archived_at:
        raise FeedbackReplicaError(f"{path} has no archived_at to order its harvest by")
    return archived_at


def local_versions(root: Path, machine_id: str) -> dict[str, PinVersion]:
    """This machine's pins: the live board plus the archive, newest per id.

    A live copy at least as new as an archived one wins, because a later edit
    arriving from another machine is what un-archives a pin.
    """
    versions = dict(_archived_versions(root, machine_id))
    for comment in _load(root / _COMMENTS_FILE, "comments"):
        harvested = comment.get("status") == "harvested"
        live = _version(comment, machine_id, archived=False, harvested=harvested)
        archived = versions.get(live.doc["id"])
        if archived is None or live.updated_at >= archived.updated_at:
            versions[live.doc["id"]] = live
    return versions


def stored_versions(conn: sqlite3.Connection) -> dict[str, PinVersion]:
    """Every ``feedback_pins`` row, tombstones included."""
    rows = conn.execute(
        f"SELECT pin_id, doc, updated_at, origin_device_id, deleted_at "
        f"FROM {FEEDBACK_PINS_TABLE}"
    ).fetchall()
    versions: dict[str, PinVersion] = {}
    for pin_id, doc_text, updated_at, origin, deleted_at in rows:
        doc = json.loads(doc_text)
        if not isinstance(doc, dict) or doc.get("id") != pin_id:
            raise FeedbackReplicaError(
                f"{FEEDBACK_PINS_TABLE} row {pin_id!r} does not hold that pin's doc"
            )
        # Normalized exactly as the local side is, so a row exported by a
        # build with other defaults does not read as a different pin forever.
        versions[pin_id] = _version(
            doc, origin or "", archived=deleted_at is not None,
            updated_at=sync_stamp.to_canonical(updated_at),
        )
    return versions


# ----- reconcile -------------------------------------------------------------


def _same(local: PinVersion, stored: PinVersion) -> bool:
    return local.doc_text == stored.doc_text and local.archived == stored.archived


def _export(conn: sqlite3.Connection, pin_id: str, local: PinVersion, machine_id: str) -> None:
    stamp = sync_stamp.stamp_and_log(
        conn, FEEDBACK_PINS_TABLE, (pin_id,), machine_id, now=local.updated_at
    )
    conn.execute(
        f"""
        INSERT INTO {FEEDBACK_PINS_TABLE}(
            pin_id, doc, updated_at, origin_device_id, deleted_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(pin_id) DO UPDATE SET
            doc              = excluded.doc,
            updated_at       = excluded.updated_at,
            origin_device_id = excluded.origin_device_id,
            deleted_at       = excluded.deleted_at
        """,
        (
            pin_id,
            local.doc_text,
            stamp.updated_at,
            stamp.origin_device_id,
            stamp.updated_at if local.archived else None,
        ),
    )


def _import(root: Path, imports: list[PinVersion], live: list[dict[str, Any]]) -> None:
    """Write winning rows into the live board or the archive."""
    by_id = {comment.get("id"): index for index, comment in enumerate(live)}
    archived_texts = {
        canonical_doc_text(_normalized(comment)) for _, comment, _ in _archive_entries(root)
    }
    removed: set[str] = set()
    for stored in imports:
        pin_id = stored.doc["id"]
        if stored.archived:
            if pin_id in by_id:
                removed.add(pin_id)
            if stored.doc_text not in archived_texts:
                _append_to_archive(root, stored.doc)
                archived_texts.add(stored.doc_text)
        elif pin_id in by_id:
            live[by_id[pin_id]] = stored.doc
        else:
            by_id[pin_id] = len(live)
            live.append(stored.doc)
    kept = [comment for comment in live if comment.get("id") not in removed]
    _save(root / _COMMENTS_FILE, "comments", kept)


@dataclass(frozen=True)
class _ReconcilePlan:
    """Which side of each pin moves: local wins export, stored wins import."""

    exports: list[tuple[str, PinVersion]]
    imports: list[PinVersion]
    unchanged: int


def _winning_side(
    mine: PinVersion | None, theirs: PinVersion | None, machine_id: str
) -> Literal["export", "import", "unchanged"]:
    """The one LWW rule (ADR-0013) for one pin id held by either side.

    A side that does not hold the pin never wins by absence being read as
    deletion: absence only means the other side's version is copied over.

    Two refinements from the PR #1978 review:

    * A ``harvested`` local copy is never exported. It keeps the pin off this
      board until a strictly later version arrives from elsewhere.
    * An equal edit instant against a row another machine wrote defers to
      that row. The local origin is always this machine, even for a copy it
      imported verbatim, so breaking that tie on the local id would re-export
      someone else's pin under this machine's name, or re-import it forever.
    """
    if theirs is None:
        return "unchanged" if mine is not None and mine.harvested else "export"
    if mine is None:
        return "import"
    if _same(mine, theirs):
        return "unchanged"
    if theirs.archived and not mine.archived and mine.updated_at > theirs.updated_at:
        log.info(
            "feedback pins: rejected tombstone for %s: local %s > remote %s",
            mine.doc.get("id"),
            mine.updated_at,
            theirs.updated_at,
        )
        return "export"
    if mine.harvested:
        return "import" if theirs.updated_at > mine.updated_at else "unchanged"
    if mine.updated_at == theirs.updated_at and theirs.origin_device_id != machine_id:
        return "import"
    return "export" if mine.lww_key() > theirs.lww_key() else "import"


def _plan_reconcile(
    local: dict[str, PinVersion], stored: dict[str, PinVersion], machine_id: str
) -> _ReconcilePlan:
    exports: list[tuple[str, PinVersion]] = []
    imports: list[PinVersion] = []
    unchanged = 0
    for pin_id in sorted(local.keys() | stored.keys()):
        mine, theirs = local.get(pin_id), stored.get(pin_id)
        side = _winning_side(mine, theirs, machine_id)
        if side == "export" and mine is not None:
            exports.append((pin_id, mine))
        elif side == "import" and theirs is not None:
            imports.append(theirs)
        elif side == "unchanged":
            unchanged += 1
    return _ReconcilePlan(exports=exports, imports=imports, unchanged=unchanged)


def reconcile(conn: sqlite3.Connection, root: Path) -> ReconcileResult:
    """Bring ``comments.json`` and ``feedback_pins`` to the same set of pins.

    Idempotent: a second call with nothing edited in between moves nothing.
    """
    machine_id = sync_stamp.ensure_local_machine(conn)
    with _COMMENTS_LOCK:
        plan = _plan_reconcile(
            local_versions(root, machine_id), stored_versions(conn), machine_id
        )
        with sync_stamp.stamped_transaction(conn):
            for pin_id, mine in plan.exports:
                _export(conn, pin_id, mine, machine_id)
        if plan.imports:
            _import(root, plan.imports, _load(root / _COMMENTS_FILE, "comments"))
    exported, imported, unchanged = len(plan.exports), len(plan.imports), plan.unchanged
    if exported or imported:
        log.info(
            "feedback pins reconciled: %d exported for push, %d imported, %d unchanged",
            exported, imported, unchanged,
        )
    return ReconcileResult(exported=exported, imported=imported, unchanged=unchanged)


# ----- per-pin status --------------------------------------------------------


@dataclass(frozen=True)
class PinSyncStatus:
    pin_id: str
    state: PinSyncState
    archived: bool
    updated_at: str
    origin_device_id: str | None
    attachment_bytes: AttachmentBytes


def _push_fence(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(last_push_seq) FROM sync_state").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _last_logged_seq(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT row_pk, MAX(seq) FROM {sync_stamp.LOCAL_CHANGELOG_TABLE} "
        f"WHERE table_name = ? GROUP BY row_pk",
        (FEEDBACK_PINS_TABLE,),
    ).fetchall()
    return {row_pk: int(seq) for row_pk, seq in rows}


def attachment_bytes(root: Path, doc: dict[str, Any]) -> AttachmentBytes:
    """Whether the screenshot a pin names is on THIS machine (FBSYNC-05).

    Attachment bytes do not sync yet, only their metadata does, so a pin
    pulled from another machine names a file this one never received.
    """
    attachment = doc.get("attachment")
    if not attachment:
        return "none"
    ext = _EXT_BY_CONTENT_TYPE.get(attachment.get("content_type", ""))
    if ext is None:
        raise FeedbackReplicaError(
            f"pin {doc.get('id')!r} attachment has unknown content type "
            f"{attachment.get('content_type')!r}"
        )
    path = root / "attachments" / f"{attachment['id']}.{ext}"
    return "present" if path.is_file() else "missing"


def pin_statuses(conn: sqlite3.Connection, root: Path) -> list[PinSyncStatus]:
    """Every pin on either side and where it stands against the hub."""
    machine_id = sync_stamp.ensure_local_machine(conn)
    # Both sides under the lock, so a reconcile cannot land between the two
    # reads and make a settled pin look unreconciled.
    with _COMMENTS_LOCK:
        local = local_versions(root, machine_id)
        stored = stored_versions(conn)
    fence = _push_fence(conn)
    logged = _last_logged_seq(conn)
    statuses: list[PinSyncStatus] = []
    for pin_id in sorted(local.keys() | stored.keys()):
        mine, theirs = local.get(pin_id), stored.get(pin_id)
        side = _winning_side(mine, theirs, machine_id)
        harvested = side == "unchanged" and mine is not None and mine.harvested
        shown = mine if harvested or theirs is None else theirs
        assert shown is not None  # the id came from one of the two maps
        if side != "unchanged":
            state: PinSyncState = "unreconciled"
        elif harvested:
            state = "harvested"
        elif logged.get(sync_stamp.encode_row_pk((pin_id,)), 0) > fence:
            state = "pending_push"
        else:
            state = "synced"
        statuses.append(PinSyncStatus(
            pin_id=pin_id,
            state=state,
            archived=shown.archived,
            updated_at=shown.updated_at,
            origin_device_id=theirs.origin_device_id if theirs is not None else None,
            attachment_bytes=attachment_bytes(root, shown.doc),
        ))
    return statuses


__all__ = [
    "ARCHIVED",
    "FEEDBACK_PINS_TABLE",
    "FeedbackReplicaError",
    "PinSyncStatus",
    "PinVersion",
    "ReconcileResult",
    "attachment_bytes",
    "canonical_doc_text",
    "local_versions",
    "pin_statuses",
    "reconcile",
    "stored_versions",
]
