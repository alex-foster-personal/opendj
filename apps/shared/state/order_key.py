"""Fractional sort keys for playlist membership rows (LIBM-20 / LIBM-02 subset)."""
from __future__ import annotations

MAX_ORDER_KEY_LEN = 64

_ALPHANUM = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_MID = "V"


class PrecisionExhausted(ValueError):
    """No key fits this gap within MAX_ORDER_KEY_LEN; the caller renumbers."""


# Keys written in bulk (a full rewrite or a renumber) start here, not at
# from_index(0): "00000000" is the smallest 8-character key, so nothing could
# ever be inserted before it without renumbering the whole playlist.
KEY_BASE = 1_000_000
_MAX_INDEX = 10**8 - 1  # from_index widens to 9 characters past this and misorders


def from_index(i: int) -> str:
    """Same string as SQL ``printf('%08d', i)``."""
    if i < 0:
        raise ValueError(f"index must be >= 0, got {i}")
    return f"{i:08d}"


def renumbered_keys(count: int) -> list[str]:
    """``count`` evenly spaced 8-character keys with room before the first."""
    if KEY_BASE + count > _MAX_INDEX:
        raise PrecisionExhausted(f"{count} members exceed the 8-character key space")
    return [from_index(KEY_BASE + i) for i in range(count)]


def _digit(key: str, i: int) -> int:
    pos = _ALPHANUM.find(key[i])
    if pos < 0:
        raise ValueError(f"non-alphanum key {key!r} at position {i}")
    return pos


def _decrement(key: str) -> str | None:
    """The largest key of the same length below ``key``; None when every digit is 0."""
    for i in range(len(key) - 1, -1, -1):
        pos = _digit(key, i)
        if pos > 0:
            return key[:i] + _ALPHANUM[pos - 1] + _ALPHANUM[-1] * (len(key) - 1 - i)
    return None


def _increment(key: str) -> str:
    """The smallest key of the same length above ``key``; one longer when every digit is z."""
    for i in range(len(key) - 1, -1, -1):
        pos = _digit(key, i)
        if pos < len(_ALPHANUM) - 1:
            return key[:i] + _ALPHANUM[pos + 1] + _ALPHANUM[0] * (len(key) - 1 - i)
    return key + _MID


def between(left: str | None, right: str | None) -> str:
    """Return k with left < k < right (None = +/- inf). Strict, unique, never empty.

    An append increments the last key like a counter, so its length stays
    fixed; a head insert decrements the first key. Raises PrecisionExhausted
    when no key fits, e.g. before an all-zero or empty key.
    """
    if left is not None and right is not None and left >= right:
        raise ValueError(f"left must be < right: {left!r} >= {right!r}")
    if left is None:
        if right is None:
            return from_index(KEY_BASE)
        below = _decrement(right) if right else None
        if below is None:
            raise PrecisionExhausted(f"no key sorts before {right!r}")
        return below
    if right is None:
        return _increment(left)
    common = 0
    while (
        common < len(left)
        and common < len(right)
        and left[common] == right[common]
    ):
        common += 1
    if common == len(left):
        # left is a proper prefix of right: extend left with a suffix below
        # right's remainder, since left + "V" can sort after right.
        suffix = _decrement(right[common:])
        if suffix is None:
            raise PrecisionExhausted(f"no key between {left!r} and {right!r}")
        return left + suffix
    lp = _digit(left, common)
    rp = _digit(right, common)
    if rp - lp > 1:
        return left[:common] + _ALPHANUM[(lp + rp) // 2]
    return left + _MID


def allocate_keys(left: str | None, right: str | None, n: int) -> list[str]:
    """Return n strictly increasing keys with left < k0 < ... < k{n-1} < right."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    max_len = MAX_ORDER_KEY_LEN
    keys: list[str] = []
    prev = left
    for _ in range(n):
        candidate = between(prev, right)
        if len(candidate) > max_len:
            raise PrecisionExhausted(
                f"order_key length {len(candidate)} exceeds MAX_ORDER_KEY_LEN={max_len}"
            )
        if prev is not None and candidate <= prev:
            raise PrecisionExhausted(
                f"order_key {candidate!r} is not strictly after {prev!r}"
            )
        if right is not None and candidate >= right:
            raise PrecisionExhausted(
                f"order_key {candidate!r} is not strictly before {right!r}"
            )
        keys.append(candidate)
        prev = candidate
    return keys


__all__ = [
    "KEY_BASE",
    "MAX_ORDER_KEY_LEN",
    "PrecisionExhausted",
    "allocate_keys",
    "between",
    "from_index",
    "renumbered_keys",
]
