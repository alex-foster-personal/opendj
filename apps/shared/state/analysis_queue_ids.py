"""Analysis-queue row literals shared by the queue and its non-analysis readers.

Issue #13074. ``apps.analysis.queue_store`` (item states) and
``apps.analysis.queue_user_lanes`` (user batch ids) re-export these names;
``apps.genre_infer`` reads them here so it never imports ``apps.analysis``
and the ``analysis <-> genre_infer`` package cycle stays closed.

-Claude
"""

from __future__ import annotations

#: Item states. Terminal states are the ones a resume never revisits.
ITEM_PENDING: str = "pending"
ITEM_RUNNING: str = "running"
ITEM_DONE: str = "done"
ITEM_SKIPPED: str = "skipped"
ITEM_FAILED: str = "failed"
ITEM_REFUSED: str = "refused"
ITEM_CANCELLED: str = "cancelled"
ITEM_DEFERRED: str = "deferred"

#: Fixed batch id per user-ordered job lane (stems, lyrics, genre).
USER_BATCH_IDS: dict[str, str] = {
    "stems": "ub_stems",
    "lyrics": "ub_lyrics",
    "genre": "ub_genre",
}
