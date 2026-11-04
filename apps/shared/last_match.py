"""Shared ``Pattern.findall`` last-match guards for caller-owned line grammars.

Each caller keeps its regex, missing message, and refusal type. This module
only centralizes the findall-or-refuse-last-match shape so launchers and
operational scripts do not duplicate it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = ["require_last_match"]


def require_last_match(
    text: str,
    pattern: re.Pattern[str],
    *,
    missing_message: str,
    refuse: Callable[[str], Exception] = ValueError,
) -> str:
    """Return the last capture from ``pattern.findall(text)``; otherwise refuse."""
    matches = pattern.findall(text)
    if not matches:
        raise refuse(missing_message)
    return matches[-1]
