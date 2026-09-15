"""Stable machine-readable codes shared by wire-facing packages."""

from __future__ import annotations

# The code is part of the CloudSync refusal wire shape. Keeping it in the
# stable core lets both the provider and transport layers branch on the same
# value without making either layer depend on the other.
NOT_IN_PLAN_CODE: str = "entitlement_not_in_plan"

__all__ = ["NOT_IN_PLAN_CODE"]
