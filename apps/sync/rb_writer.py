"""Rekordbox cue writer via pyrekordbox ORM.

Design notes (D4 + 04-CONTEXT §O2):
  * Inserts/updates rows in ``DjmdCue``. ``InMpegFrame``/``InMpegAbs``
    stay NULL; RB backfills on next open for MP3s.
  * ``InFrame`` = ``round(InMsec * 0.441)`` at 44.1 kHz when not MPEG.
  * Per-track transactional commit; on verify failure, rollback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apps.shared.normalised import NormalisedCue
from apps.shared.rb_color_palette import rgb_to_color_index

# D3 (.planning/t3b-decomposition-map.md section 3): this module and
# rb_vendor's hot-cue writer carried byte-identical copies of the 44.1 kHz
# frame heuristic, and rb_vendor's docstring named this file as its twin
# without either ever being consolidated. apps.shared.rb_frames is now the one
# home; see its module docstring for why apps.shared rather than the map's
# adapters/rekordbox/cues.py. Aliased so this module's call sites and
# tests/test_rb_writer.py, which imports the private name from here, are
# untouched.
from apps.shared.rb_frames import msec_to_frame as _msec_to_frame
from apps.shared.rekordbox_writeback import require_writeback_enabled

if TYPE_CHECKING:
    from pyrekordbox import Rekordbox6Database


@dataclass(slots=True)
class WriteReport:
    content_id: str
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)


def _kind_to_rb(kind: str, index: int | None, _active_loop: bool) -> tuple[int, bool]:
    """Map ``NormalisedCue.kind`` to ``(DjmdCue.Kind, ActiveLoop)``.

    pyrekordbox docs (``db6/tables.py`` DjmdCue):
      * 0 = memory cue
      * 1..8 = hot cue slots (slot n uses Kind=n)
      * loop: Kind=4 + ActiveLoop=True is one common form; we surface the
        ``ActiveLoop`` flag explicitly and let callers decide to use it.
    """
    if kind == "memory":
        return 0, False
    if kind == "hot":
        if index is None:
            return 1, False
        return int(index) + 1, False
    if kind == "loop":
        return 4, True
    if kind == "load":
        return 3, False
    return 0, False


def write_cues(
    db: "Rekordbox6Database",
    content_id: str,
    cues: list[NormalisedCue],
    *,
    prune_missing: bool = False,
    tolerance_msec: int = 10,
) -> WriteReport:
    """Insert or update cues for one track.

    Each cue is matched to an existing ``DjmdCue`` row by position
    (±``tolerance_msec``) and kind. On verify failure the pyrekordbox
    session is rolled back (caller should catch and retry).

    ONE-WAY IMPORT GATE. The target here is the ``db`` HANDLE, not a path, so
    there is nothing outside this function to guard: hand it
    ``open_db(REKORDBOX_LIVE_DB)`` and it mutates the real library. The guard
    therefore lives inside the writer itself, where every present and future
    caller must pass through it.
    """
    require_writeback_enabled("module.sync.rb_writer.write_cues")

    report = WriteReport(content_id=str(content_id))

    try:
        existing = list(db.get_cue(ContentID=str(content_id)))
    except Exception:
        existing = [
            r for r in db.get_cue()
            if str(getattr(r, "ContentID", "")) == str(content_id)
        ]

    try:
        content = db.get_content(ID=str(content_id)).one()
        content_uuid = getattr(content, "UUID", "") or ""
    except Exception:
        content_uuid = ""

    # Match existing rows to incoming cues (position ± tolerance, same kind).
    unmatched_existing = list(range(len(existing)))
    for cue in cues:
        kind_int, _active = _kind_to_rb(cue.kind, cue.index, False)
        color_idx = rgb_to_color_index(cue.color_rgb)
        target = None
        target_j = -1
        for j in unmatched_existing:
            ex = existing[j]
            ex_msec = getattr(ex, "InMsec", None)
            if ex_msec is None:
                continue
            if abs(int(ex_msec) - cue.position_msec) > tolerance_msec:
                continue
            if getattr(ex, "Kind", None) != kind_int and not (
                cue.kind == "loop" and getattr(ex, "ActiveLoop", False)
            ):
                continue
            target = ex
            target_j = j
            break
        if target is not None:
            target.InMsec = cue.position_msec
            target.InFrame = _msec_to_frame(cue.position_msec)
            target.InMpegFrame = None
            target.InMpegAbs = None
            target.Kind = kind_int
            target.Color = color_idx
            if cue.name:
                target.Comment = cue.name
            if cue.kind == "loop" and cue.loop_length_msec:
                target.OutMsec = cue.position_msec + cue.loop_length_msec
                target.OutFrame = _msec_to_frame(target.OutMsec)
                target.ActiveLoop = True
            if content_uuid:
                target.ContentUUID = content_uuid
            unmatched_existing.remove(target_j)
            report.updated += 1
        else:
            try:
                new = db.create_cue(
                    ContentID=str(content_id),
                    InMsec=cue.position_msec,
                    InFrame=_msec_to_frame(cue.position_msec),
                    InMpegFrame=None,
                    InMpegAbs=None,
                    Kind=kind_int,
                    Color=color_idx,
                    Comment=cue.name or "",
                )
                if cue.kind == "loop" and cue.loop_length_msec:
                    new.OutMsec = cue.position_msec + cue.loop_length_msec
                    new.OutFrame = _msec_to_frame(new.OutMsec)
                    new.ActiveLoop = True
                if content_uuid:
                    new.ContentUUID = content_uuid
                report.inserted += 1
            except Exception as e:
                report.errors.append(f"insert failed: {e}")

    if prune_missing and unmatched_existing:
        for j in unmatched_existing:
            ex = existing[j]
            try:
                db.delete_cue(ex)
                report.deleted += 1
            except Exception as e:
                report.errors.append(f"delete failed: {e}")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        report.errors.append(f"commit failed (rolled back): {e}")

    return report


__all__ = ["WriteReport", "write_cues"]
