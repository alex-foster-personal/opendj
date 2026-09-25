"""The comparable key of one requirement, shared by the lock gate's comparisons
(stdlib only, like scripts/lock_metadata_check.py, which owns the parsers)."""

from __future__ import annotations

from collections import Counter

from scripts.lock_marker_semantics import markers_equivalent, spelled_markers_equivalent

Key = tuple[str, tuple[str, ...], str, tuple[str, ...], str]


def same_requirement(key: Key, other: Key) -> bool:
    """Two requirements as SPELLED in pyproject.toml that uv records once (round 26)."""
    if key[:3] != other[:3] or key[4] != other[4]:
        return False
    return spelled_markers_equivalent(key[3], other[3])


def pair_by_meaning(
    expected: Counter[Key], recorded: Counter[Key]
) -> tuple[Counter[Key], Counter[Key]]:
    """Drop, from both leftover sides, requirements that differ only by an equivalent marker."""
    left, right = Counter(expected), Counter(recorded)
    for key_a in list(left):
        for key_b in list(right):
            if left[key_a] == 0 or right[key_b] == 0:
                continue
            if key_a[:3] != key_b[:3] or key_a[4] != key_b[4]:
                continue
            if markers_equivalent(key_a[3], key_b[3]):
                left[key_a] -= 1
                right[key_b] -= 1
    return +left, +right


def fmt_key(key: Key) -> str:
    name, extras, spec, marker, source = key
    text = name + (f"[{','.join(extras)}]" if extras else "") + spec
    text += f" @ {source}" if source else ""
    return text + (f"; {' and '.join(marker)}" if marker else "")
