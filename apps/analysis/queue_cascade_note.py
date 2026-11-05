"""Which batch a dependency-cascade batch was cascaded from.

A run that moves a canonical record enqueues one follow-up batch for the
dependent lane (beatgrid moved, key re-queues; see
:func:`apps.analysis.queue_runner._enqueue_cascade`). That batch has no runner
of its own: the drain that created it runs it next, in the same process. When
that process dies before it does, whoever started the drain has to find the
follow-up batch to settle it, and the batch's note is where the link lives:
it ends with ``(from batch <parent id>)``.

Requirements (mini-PRD):
  ✔︎ ✅ cascade_note(): the note names the parent batch.
    [if] two parents' cascade notes are equal [then ✖︎]
  ✔︎ ✅ cascade_children(): exactly the batches cascaded from one parent.
    [if] a cascade from another parent is returned [then ✖︎]
    [if] a batch whose note merely mentions the parent is returned [then ✖︎]
  ✔︎ ✅ cascade_parent(): the parent a cascade note names, else None.
    [if] a note that is not a cascade note yields a parent [then ✖︎]
  ✔︎ ✅ reserved_note_reason(): an operator's note cannot pass for the engine's.
    [if] an operator's note reads as a drained or a cascade batch [then ✖︎]
         (the next start would settle that operator's batch as interrupted)
    [if] any other note is refused [then ✖︎]

-Claude
"""

from __future__ import annotations

import sqlite3

CASCADE_NOTE_PREFIX: str = "dependency cascade: "
#: Ends the note of every batch the engine drains itself (a Re-analyze); the
#: daemon's next start settles such a batch if it is still open.
DRAIN_NOTE_MARK: str = "(run by the engine)"


def cascade_note(lane: str, parent_batch_id: str) -> str:
    """The note a cascade batch for ``lane`` carries, naming its parent."""
    return f"{CASCADE_NOTE_PREFIX}{lane} re-queued after its dependency moved (from batch {parent_batch_id})"


def cascade_children(conn: sqlite3.Connection, parent_batch_id: str) -> list[str]:
    """Batch ids cascaded directly from ``parent_batch_id``, oldest first."""
    suffix = f"(from batch {parent_batch_id})"
    rows = conn.execute(
        "SELECT batch_id, note FROM analysis_queue_batch WHERE substr(note, 1, ?) = ? ORDER BY created_at, batch_id",
        (len(CASCADE_NOTE_PREFIX), CASCADE_NOTE_PREFIX),
    ).fetchall()
    return [batch_id for batch_id, note in rows if note.endswith(suffix)]


def cascade_parent(note: str | None) -> str | None:
    """The parent batch id a cascade batch's note names, or None for any other note."""
    if note is None or not note.startswith(CASCADE_NOTE_PREFIX):
        return None
    head, marker, parent = note.rpartition("(from batch ")
    if not marker or not head or not parent.endswith(")"):
        return None
    return parent[:-1] or None


def is_drain_note(note: str | None) -> bool:
    """True for the note of a batch the engine drains itself."""
    return note is not None and note.endswith(DRAIN_NOTE_MARK)


def reserved_note_reason(note: str | None) -> str | None:
    """Why ``note`` cannot be an operator's own, or None when it can.

    The note is where the engine records that it owns a batch: a drained
    batch's mark, a cascade's parent. An operator's note that reads either
    way would be settled as the engine's on its next start, so every path an
    operator enqueues through refuses it, with the same test the readers use.
    """
    if is_drain_note(note):
        return f"a note may not end with {DRAIN_NOTE_MARK!r}: it marks a batch the engine drains"
    if note is not None and note.startswith(CASCADE_NOTE_PREFIX):
        return f"a note may not start with {CASCADE_NOTE_PREFIX!r}: it marks a dependency-cascade batch"
    return None


__all__ = [
    "CASCADE_NOTE_PREFIX",
    "DRAIN_NOTE_MARK",
    "cascade_children",
    "cascade_note",
    "cascade_parent",
    "is_drain_note",
    "reserved_note_reason",
]
