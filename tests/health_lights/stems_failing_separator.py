"""TEST DOUBLE for the vendor call in ``scripts/stems_modal_worker.py``: every
track fails.

THIS IS TEST CODE AND IS NEVER IMPORTED BY THE APP. The worker reaches it only
when ``MDT_STEMS_SEPARATOR`` names it. Everything around the vendor call is the
real worker: its own ``scope: pending`` selection, its per-track failure
handling and the ``failures:`` report it prints. The double returns the
vendor's per-track failure shape, ``{stable_id, error}``, for every track it
is handed, and writes no bytes.

When ``STEMS_LATE_TRACK`` is set (``stable_id|state_db|audio_path``) it first
adds that library row, with audio on disk and an OLD ``created_at`` like a
CloudSync-preserved row, so the row is pending when the run ends but was never
in the batch the worker selected.

-Claude
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

LATE_ENV: str = "STEMS_LATE_TRACK"
OLD_STAMP: str = "2020-01-01T00:00:00Z"
ERROR: str = "test double: the separator refused this track"


def _add_late_track(spec: str) -> None:
    stable_id, state_db, audio_path = spec.split("|")
    Path(audio_path).write_bytes(b"x" * 4096)
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "duration_ms, file_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (stable_id, "inferred", f"t-{stable_id}", '["Artist"]', 200_000, audio_path, OLD_STAMP, OLD_STAMP),
        )
        conn.commit()
    finally:
        conn.close()


def separate(tracks: Sequence[Any], tier_key: str) -> Iterator[dict[str, Any]]:
    late = os.environ.get(LATE_ENV, "").strip()
    if late:
        _add_late_track(late)
    for track in tracks:
        yield {"stable_id": track.stable_id, "error": ERROR}
