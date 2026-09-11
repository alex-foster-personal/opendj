"""User-ordered job lanes (stems, lyrics). Not analysis record lanes.

Issue #1865 / PERFBATCH-05. These names must never join
:data:`apps.analysis.lane_enums.LANES`: mixing them would drag stems and
lyrics through memory admission, producer-version skip, and the key ->
beatgrid cascade.
"""
from __future__ import annotations

USER_JOB_LANES: tuple[str, str] = ("stems", "lyrics")
USER_BATCH_IDS: dict[str, str] = {
    "stems": "ub_stems",
    "lyrics": "ub_lyrics",
}
USER_BACKENDS: dict[str, str] = {
    "stems": "user.stems",
    "lyrics": "user.lyrics",
}

SKIP_UP_TO_DATE: str = "up_to_date"
SKIP_DETAIL: str = "skipped: up to date"
MAX_ENQUEUE_IDS: int = 5000
