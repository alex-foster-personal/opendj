"""Re-export of the dedup database schema, which lives in the contract.

The DDL and :func:`ensure_schema` moved verbatim to
:mod:`apps.dedup.contract.schema` (M1, ADR-NEW-module-contracts) because
``apps.tags`` writes ``tag_provenance`` into the same database and may import
only dedup's contract. Code inside ``apps.dedup`` keeps importing this module.
"""

from __future__ import annotations

from apps.dedup.contract.schema import SCHEMA_SQL, ensure_schema

__all__ = ["SCHEMA_SQL", "ensure_schema"]
