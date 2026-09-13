"""Pure duplicate-policy helpers for playlist membership writes (LIBM-D2)."""
from __future__ import annotations


def new_stable_ids(existing: list[str], requested: list[str]) -> list[str]:
    """Request-order ids not already present.

    First copy of a repeated request id wins. ``existing`` is live membership
    stable_ids (duplicates allowed).
    """
    seen_existing = set(existing)
    seen_request: set[str] = set()
    out: list[str] = []
    for sid in requested:
        if sid in seen_existing or sid in seen_request:
            continue
        seen_request.add(sid)
        out.append(sid)
    return out


def first_repeated_stable_id(stable_ids: list[str]) -> str | None:
    """First id that has already appeared earlier in the list, else None."""
    seen: set[str] = set()
    for sid in stable_ids:
        if sid in seen:
            return sid
        seen.add(sid)
    return None


__all__ = ["first_repeated_stable_id", "new_stable_ids"]
