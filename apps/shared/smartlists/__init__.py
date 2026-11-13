"""Smartlist rule AST + validator (SMART-01)."""

from __future__ import annotations

from .ast import is_logical, is_predicate, is_top_n, referenced_fields, walk
from .models import SmartlistRow
from .schema import (
    ALLOWED_FIELDS,
    ALLOWED_OPS_BY_FIELD,
    FIELD_TYPES,
    LOGICAL_OPS,
    TOP_N_OP,
    TOP_N_SORT_FIELDS,
    SmartlistRuleError,
    validate_rule,
)

__all__ = [
    "ALLOWED_FIELDS",
    "ALLOWED_OPS_BY_FIELD",
    "FIELD_TYPES",
    "LOGICAL_OPS",
    "TOP_N_OP",
    "TOP_N_SORT_FIELDS",
    "SmartlistRow",
    "SmartlistRuleError",
    "is_logical",
    "is_predicate",
    "is_top_n",
    "referenced_fields",
    "validate_rule",
    "walk",
]
