"""Fractional sort keys for playlist membership rows (LIBM-20 / LIBM-02 subset)."""
from __future__ import annotations

_ALPHANUM = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_MID = "V"


def from_index(i: int) -> str:
    """Same string as SQL ``printf('%08d', i)``."""
    if i < 0:
        raise ValueError(f"index must be >= 0, got {i}")
    return f"{i:08d}"


def between(left: str | None, right: str | None) -> str:
    """Return k with left < k < right (None = +/- inf). Strict, unique."""
    if left is not None and right is not None and left >= right:
        raise ValueError(f"left must be < right: {left!r} >= {right!r}")
    if left is None and right is None:
        return from_index(0)
    if left is None:
        if not right:
            raise ValueError("cannot insert before empty right bound")
        for i in range(len(right) - 1, -1, -1):
            pos = _ALPHANUM.find(right[i])
            if pos > 0:
                return right[:i] + _ALPHANUM[pos - 1]
        return ""
    if right is None:
        return left + _MID
    common = 0
    while (
        common < len(left)
        and common < len(right)
        and left[common] == right[common]
    ):
        common += 1
    if common == len(left):
        return left + _MID
    lp = _ALPHANUM.find(left[common])
    rp = _ALPHANUM.find(right[common])
    if lp < 0 or rp < 0:
        raise ValueError(f"non-alphanum key at position {common}")
    if rp - lp > 1:
        return left[:common] + _ALPHANUM[(lp + rp) // 2]
    return left + _MID


__all__ = ["between", "from_index"]
