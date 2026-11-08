"""Column docs for the set target and skip-in-set tables (LIBUX-8679).

Owned by ``apps/shared/set_target/store.py``, additive on state.db.
"""

from __future__ import annotations

SET_TARGET_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "set_targets": {
        "playlist_id": "The playlist shown as a set that this target belongs to.",
        "target_s": "Target set length in seconds; always positive, no row means no target.",
    },
    "set_skips": {
        "playlist_id": "The playlist shown as a set.",
        "stable_id": "A track marked skip-in-set: listed in the set but left out of its played length.",
    },
}
