"""Paths for the set-recording data tree.

Every file the recorder writes lives under :data:`SETS_DIR`. A single
session occupies ``<SETS_DIR>/<session_id>/``:

  * ``audio_YYYY-MM-DDTHH-MM-SS.mp3`` -- rolling 5-minute segments
  * ``timeline.jsonl``                -- append-only event log
  * ``manifest.json``                 -- summary written at stop
  * ``recorder.pid``                  -- live only while recording
  * ``transitions.jsonl``             -- Plan 12-02 output
  * ``labels.jsonl``                  -- Plan 12-02/12-03 relabels

``SETS_DB`` is the shim SQLite DB (schema in :mod:`apps.sets.state`). It is
local and derivative; delete-safe. If Phase 5's ``apps.shared.state``
grows a ``sets`` table compatible with ours, a follow-up migration moves
the rows over (see Plan 12-01 Open Question 2).
"""
from __future__ import annotations

from pathlib import Path

from apps.shared.paths import DATA_DIR

SETS_DIR: Path = DATA_DIR / "sets"
SETS_DB: Path = SETS_DIR / "sets.db"
MODELS_DIR: Path = Path(__file__).resolve().parent / "models"


def session_dir(session_id: str, root: Path | None = None) -> Path:
    """Return the on-disk directory for ``session_id``.

    ``root`` lets tests inject a tmp dir in place of :data:`SETS_DIR`.
    """
    base = Path(root) if root is not None else SETS_DIR
    return base / session_id


__all__ = ["SETS_DIR", "SETS_DB", "MODELS_DIR", "session_dir"]
