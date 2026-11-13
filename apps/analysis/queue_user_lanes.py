"""User-ordered job lanes (stems, lyrics, genre). Not analysis record lanes.

Issue #1865 / PERFBATCH-05. These names must never join
:data:`apps.analysis.lane_enums.LANES`: mixing them would drag stems and
lyrics through memory admission, producer-version skip, and the key ->
beatgrid cascade.
"""

from __future__ import annotations

from apps.shared.state.analysis_queue_ids import USER_BATCH_IDS

from .lane_enums import FIRST_IMPORT_DEFERRED_LANES

USER_JOB_LANES: tuple[str, ...] = ("stems", "lyrics", "genre")
FIRST_IMPORT_FORBIDDEN_LANES: tuple[str, ...] = (
    *USER_JOB_LANES,
    *FIRST_IMPORT_DEFERRED_LANES,
)
USER_BACKENDS: dict[str, str] = {
    "stems": "user.stems",
    "lyrics": "user.lyrics",
    "genre": "user.genre",
}

SKIP_UP_TO_DATE: str = "up_to_date"
SKIP_NO_LYRICS_FOUND: str = "no_lyrics_found"
SKIP_DETAIL: str = "skipped: up to date"
MAX_ENQUEUE_IDS: int = 5000

__all__ = [
    "FIRST_IMPORT_FORBIDDEN_LANES",
    "MAX_ENQUEUE_IDS",
    "SKIP_DETAIL",
    "SKIP_NO_LYRICS_FOUND",
    "SKIP_UP_TO_DATE",
    "USER_BACKENDS",
    "USER_BATCH_IDS",
    "USER_JOB_LANES",
]
