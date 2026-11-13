"""Argument checks shared by the OJ tool modules; each failure is ``invalid_arguments``.

A model sends JSON, so ``True`` would pass ``isinstance(value, int)``; these
checks refuse booleans wherever a number is asked for.
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolFailure


def _refuse(name: str, wanted: str, value: Any) -> ToolFailure:
    return ToolFailure("invalid_arguments", f"{name} must be {wanted}, got {value!r}")


def text_arg(args: dict[str, Any], name: str, *, required: bool = False) -> str | None:
    """A non-empty string argument, or None when it is optional and absent."""
    value = args.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _refuse(name, "a non-empty string", value)
    return value


def number_arg(args: dict[str, Any], name: str, *, low: float, high: float) -> float | None:
    """An optional number within [low, high], or None when absent."""
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise _refuse(name, f"a number from {low:g} to {high:g}", value)
    return float(value)


def int_arg(args: dict[str, Any], name: str, *, low: int, high: int, default: int | None = None) -> int | None:
    """An optional whole number within [low, high], or ``default`` when absent."""
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _refuse(name, f"a whole number from {low} to {high}", value)
    return value
