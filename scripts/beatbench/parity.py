"""Pure helpers for comparing a GPU farm run against a CPU run.

Separated from `scripts/modal_beat_farm.py` because that module imports
`modal` at import time and cannot do otherwise: `@app.cls(gpu=...)` and
`@modal.method()` are evaluated when the module is read. `modal` is not in the
repo venv, so everything living beside those decorators was unimportable in
the test lane, and the alignment rule below - which decides whether a parity
claim is meaningful at all - therefore shipped with no test.

Nothing here touches the network, the filesystem or a GPU. That is the point.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any


def beat_gap_ms(a: list[float], b: list[float]) -> float | None:
    """Worst nearest-neighbour distance in ms between two beat sequences.

    Returns None when either side is empty, which is an unmeasurable pair
    rather than a perfect match. Symmetric on purpose: a run that DROPPED a
    beat and a run that INVENTED one are both divergences, and a
    one-directional nearest-neighbour scan reports zero for one of them.

    A non-finite timestamp raises: `max(worst, nan)` keeps `worst`, so one NaN
    read as a 0.00 ms gap and exited 0 (Codex P2 BLOCKING, PR #1660,
    discussion_r3982585238). `json.loads` accepts NaN, so producers can emit it.
    """
    if not a or not b:
        return None
    bad = [t for t in (*a, *b) if not math.isfinite(t)]
    if bad:
        raise ValueError(f"non-finite beat timestamp(s) {bad[:3]}; not parity evidence")
    worst = 0.0
    for left, right in ((a, b), (b, a)):
        for t in left:
            worst = max(worst, min(abs(t - u) for u in right) * 1000.0)
    return worst


def align(
    gpu: dict[str, dict[str, Any]], cpu: dict[str, dict[str, Any]]
) -> dict[str, str] | None:
    """Map GPU keys to CPU keys, or None when the two runs are not comparable.

    The CPU runner records absolute paths and the farm records paths relative
    to a corpus root, so the SAME clip is legitimately spelled two ways. That
    is reconciled by matching on the FULL relative suffix: a CPU key matches a
    GPU key when its path components END WITH that GPU key's components.

    Matching on the basename alone was the earlier version, and it made the
    checker unusable on exactly the layout this farm now deliberately
    preserves. Two clips called `01 - Intro.mp3` in different album
    directories gave every GPU row two CPU candidates, so the ambiguity test
    fired and the whole comparison returned None, even though
    `a/01 - Intro.mp3` and `b/01 - Intro.mp3` identify their pairs with no
    ambiguity at all (Codex P2, PR #1660).

    A match is still accepted only when it is UNAMBIGUOUS on both sides. If a
    full relative suffix picks out more than one CPU row, the runs genuinely
    are not alignable and saying so is the honest answer.
    """
    by_suffix: dict[tuple[str, ...], list[str]] = {}
    for key in cpu:
        parts = Path(key).parts
        # Every tail of the path, so a relative GPU key of any depth is one
        # lookup rather than a scan over all CPU keys.
        for i in range(len(parts)):
            by_suffix.setdefault(parts[i:], []).append(key)
    mapping: dict[str, str] = {}
    for key in gpu:
        candidates = by_suffix.get(Path(key).parts, [])
        if len(candidates) != 1:
            return None
        mapping[key] = candidates[0]
    # INJECTIVE, not merely equal in size. Two GPU keys can share a suffix
    # (they are relative paths now) while the CPU side has only one row with
    # it, and then a count check would pass coincidentally while one CPU row
    # is compared twice and another never at all.
    if len(set(mapping.values())) != len(mapping):
        return None
    return mapping
