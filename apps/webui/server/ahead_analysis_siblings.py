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
The row merge is post-v1 (``(private path)``).

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


def with_path_siblings(
    done: Iterable[str],
    path_of: Mapping[str, str],
    hash_of: Mapping[str, str],
) -> set[str]:
    """``done`` plus present rows that share a path and an audio hash.

    A row with no hash is not covered. There is no path-only fallback: a
    caller that has not loaded hashes must not mark a sibling done.

    ``hash_of`` is each row's hash at the decision. The ahead drain loads it
    from the database then, not from the present-row snapshot, so a rewrite
    inside that snapshot's minute is not treated as the same bytes.
    """
    done_set = set(done)
    covered_pairs = {
        (path_key(path_of[sid]), hash_of[sid])
        for sid in done_set
        if sid in path_of and hash_of.get(sid)
    }
    if not covered_pairs:
        return done_set
    return done_set | {
        sid
        for sid, path in path_of.items()
        if hash_of.get(sid) and (path_key(path), hash_of[sid]) in covered_pairs
    }


__all__ = ["path_key", "with_path_siblings"]
