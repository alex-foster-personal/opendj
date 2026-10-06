"""One file, one analysis: a stable_id whose file a sibling row already covers is done (#5578).

Tier-3 ("inferred") stable_ids hash path AND mtime (``apps.shared.state.ids``),
so a file whose mtime moved after import (a tag write, a copy back from a
backup) can get a second row on re-import. Silver's preview data, Tue 6 Oct
2026: 270 present files carry two rows, and the ahead-of-time drain decoded
both rows of 202 files in the loudness lane, about 13 % extra decode work.

Ship-day guard (CORE): never merge or delete rows; the drain instead treats a
row as done for a lane when another present row with the SAME file path has
that lane's result. Selection and coverage both read the widened set, so the
lane neither re-enqueues the sibling nor reports it as work left forever.
The row merge is post-v1 (``.planning/debt``).

Requirements (mini-PRD):
  ✔︎ a covered sibling is never re-enqueued (#5578)
    [if] a row's path matches a row with the lane's result [then] the row counts as done
    [if] a row's path is unique [then] only its own result makes it done
"""
from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping


def path_key(path: str) -> str:
    """Path identity for sibling matching: NFC, so NFD and NFC spellings agree."""
    return unicodedata.normalize("NFC", path)


def with_path_siblings(done: Iterable[str], path_of: Mapping[str, str]) -> set[str]:
    """``done`` plus every present row sharing a file path with a done row."""
    done_set = set(done)
    covered_paths = {path_key(path_of[sid]) for sid in done_set if sid in path_of}
    return done_set | {sid for sid, path in path_of.items() if path_key(path) in covered_paths}


__all__ = ["path_key", "with_path_siblings"]
