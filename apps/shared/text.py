"""None-and-blank text coercion shared by scanners and CloudSync identity."""

from __future__ import annotations


def as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
