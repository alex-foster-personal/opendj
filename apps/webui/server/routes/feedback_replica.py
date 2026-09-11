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

from .feedback import _COMMENTS_FILE, _COMMENTS_LOCK, CommentOut, _load, _save
from .feedback_attachments import _EXT_BY_CONTENT_TYPE
from .feedback_pins import _append_to_archive

log = logging.getLogger(__name__)

FEEDBACK_PINS_TABLE: str = "feedback_pins"
ARCHIVED: str = "archived"

PinSyncState = Literal["synced", "pending_push", "unreconciled"]
AttachmentBytes = Literal["none", "present", "missing"]


class FeedbackReplicaError(RuntimeError):
    """A pin store or row the bridge cannot read as a pin. Never swallowed."""


# ----- the two sides ---------------------------------------------------------


@dataclass(frozen=True)
class PinVersion:
    """One side's copy of one pin: its doc, LWW key and archive flag."""

    doc: dict[str, Any]
    doc_text: str
    updated_at: str
    origin_device_id: str
    archived: bool

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
    """The pin as ``CommentOut`` serializes it, so older pins gain defaults once."""
    return CommentOut.model_validate(doc).model_dump(mode="json")


def _edit_stamp(doc: dict[str, Any]) -> str:
    raw = doc.get("updated_at") or doc.get("created_at")
    if not isinstance(raw, str) or not raw:
        raise FeedbackReplicaError(
            f"pin {doc.get('id')!r} has neither updated_at nor created_at, so "
            f"it cannot be ordered against another machine's copy"
        )
    return sync_stamp.to_canonical(raw)


def _version(doc: dict[str, Any], origin: str, *, archived: bool) -> PinVersion:
    normalized = _normalized(doc)
    return PinVersion(
        doc=normalized,
        doc_text=canonical_doc_text(normalized),
        updated_at=_edit_stamp(normalized),
        origin_device_id=origin,
        archived=archived,
    )


def _newer(current: PinVersion | None, candidate: PinVersion) -> PinVersion:
    if current is None or candidate.lww_key() > current.lww_key():
        return candidate
    return current


def _archived_versions(root: Path, machine_id: str) -> dict[str, PinVersion]:
    """Every pin an archive file records, newest copy per id.

    A pin archived through ``POST /comments/{id}/archive`` carries
    ``status: archived`` and its own archive time. One moved by the bulk
    harvest (``POST /feedback/archive``) keeps whatever status it had, so its
    archive time is the file's ``archived_at``.
    """
    versions: dict[str, PinVersion] = {}
    for path in sorted(root.glob("archive-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        comments = payload.get("comments") if isinstance(payload, dict) else None
        if not isinstance(comments, list):
            raise FeedbackReplicaError(f"{path} does not hold a 'comments' list")
        for comment in comments:
            stamp = (
                comment.get("updated_at")
                if comment.get("status") == ARCHIVED
                else payload.get("archived_at")
            )
            doc = {**comment, "status": ARCHIVED, "updated_at": stamp}
            version = _version(doc, machine_id, archived=True)
            versions[version.doc["id"]] = _newer(versions.get(version.doc["id"]), version)
    return versions


def local_versions(root: Path, machine_id: str) -> dict[str, PinVersion]:
    """This machine's pins: the live board plus the archive, newest per id.

    A live copy at least as new as an archived one wins, because a later edit
    arriving from another machine is what un-archives a pin.
    """
    versions = dict(_archived_versions(root, machine_id))
    for comment in _load(root / _COMMENTS_FILE, "comments"):
        live = _version(comment, machine_id, archived=False)
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
        versions[pin_id] = PinVersion(
            doc=doc,
            doc_text=doc_text,
            updated_at=sync_stamp.to_canonical(updated_at),
            origin_device_id=origin or "",
            archived=deleted_at is not None,
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
    removed: set[str] = set()
    for stored in imports:
        pin_id = stored.doc["id"]
        if stored.archived:
            if pin_id in by_id:
                removed.add(pin_id)
            _append_to_archive(root, stored.doc)
        elif pin_id in by_id:
            live[by_id[pin_id]] = stored.doc
        else:
            by_id[pin_id] = len(live)
            live.append(stored.doc)
    kept = [comment for comment in live if comment.get("id") not in removed]
    _save(root / _COMMENTS_FILE, "comments", kept)


def reconcile(conn: sqlite3.Connection, root: Path) -> ReconcileResult:
    """Bring ``comments.json`` and ``feedback_pins`` to the same set of pins.

    Idempotent: a second call with nothing edited in between moves nothing.
    """
    machine_id = sync_stamp.ensure_local_machine(conn)
    exported = imported = unchanged = 0
    with _COMMENTS_LOCK:
        local = local_versions(root, machine_id)
        stored = stored_versions(conn)
        exports: list[tuple[str, PinVersion]] = []
        imports: list[PinVersion] = []
        for pin_id in sorted(local.keys() | stored.keys()):
            mine, theirs = local.get(pin_id), stored.get(pin_id)
            if theirs is None and mine is not None:
                exports.append((pin_id, mine))
            elif mine is None and theirs is not None:
                imports.append(theirs)
            elif mine is not None and theirs is not None and _same(mine, theirs):
                unchanged += 1
            elif mine is not None and theirs is not None and mine.lww_key() > theirs.lww_key():
                exports.append((pin_id, mine))
            elif mine is not None and theirs is not None:
                imports.append(theirs)
        with sync_stamp.stamped_transaction(conn):
            for pin_id, mine in exports:
                _export(conn, pin_id, mine, machine_id)
        if imports:
            _import(root, imports, _load(root / _COMMENTS_FILE, "comments"))
        exported, imported = len(exports), len(imports)
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
    with _COMMENTS_LOCK:
        local = local_versions(root, machine_id)
    stored = stored_versions(conn)
    fence = _push_fence(conn)
    logged = _last_logged_seq(conn)
    statuses: list[PinSyncStatus] = []
    for pin_id in sorted(local.keys() | stored.keys()):
        mine, theirs = local.get(pin_id), stored.get(pin_id)
        shown = theirs if theirs is not None else mine
        assert shown is not None  # the id came from one of the two maps
        if mine is None or theirs is None or not _same(mine, theirs):
            state: PinSyncState = "unreconciled"
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
