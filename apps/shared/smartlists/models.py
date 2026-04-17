"""Frozen dataclass for one ``smartlists`` row."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SmartlistRow:
    id: str
    name: str
    rule: dict
    rule_schema_version: int
    referenced_fields: frozenset[str]
    order_by: str
    last_evaluated_at: datetime | None
    last_materialized_track_ids: list[str]
    created_at: datetime
    modified_at: datetime
    _raw_rule_json: str = field(default="", repr=False, compare=False)

    def rule_as_json(self) -> str:
        if self._raw_rule_json:
            return self._raw_rule_json
        return json.dumps(self.rule, sort_keys=True)


__all__ = ["SmartlistRow"]
