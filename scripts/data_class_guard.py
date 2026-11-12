"""Push-time data-class check: every table the schema code creates has exactly one data class.

The cheap form of `(private path)`'s coverage and doc tests, for
`scripts.pre_push_guards` (guard `data-classes`). It derives the table set the same way the test
does (the declared table constants, the tables both ladders really create in sqlite, and the
sync tables), never from the registry, and calls the same `coverage_problems` the test calls.
#12086 (`enrichment_claims`) and #12009 (`control_events`) each landed a table with no class and
turned main red; this names the miss before the push.

    python -m scripts.data_class_guard   # exit 1 and one fix line per unclassified table

- [if] a table the schema code creates maps to no class [then] exit 1 naming it and the fix, [else stop].
- [if] every table maps to exactly one class and the doc matches [then] exit 0, [else stop].
- [if] (private path) differs from the generator [then] exit 1 with the regen command, [else stop].
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable

from apps.engine_core.store import schema as engine_schema
from apps.shared.state import schema as state_schema
from apps.sync_hub.data_classes import DOC_PATH, coverage_problems, render_markdown
from apps.sync_hub.protocol_common import SYNC_TABLES

REGEN = "uv run python -m apps.sync_hub.data_classes --markdown"
FIX = f"add to a class in apps/sync_hub/data_classes_tables.py, then run {REGEN}"


def _ladder_created_tables() -> set[str]:
    """Every table executing the shared, engine and cache ladders creates in sqlite."""
    created: set[str] = set()
    for apply in (state_schema.apply_migrations, engine_schema.apply_migrations, engine_schema.apply_cache_migrations):
        conn = sqlite3.connect(":memory:")
        try:
            apply(conn)
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            created.update(str(r[0]) for r in rows)
        finally:
            conn.close()
    return created


def schema_tables() -> frozenset[str]:
    """The table set the data-class test derives from schema code, not from the registry."""
    return (
        state_schema.ALL_KNOWN_TABLES
        | frozenset(engine_schema.ALL_TABLES)
        | frozenset(engine_schema.ALL_CACHE_TABLES)
        | frozenset(engine_schema.VENDOR_SIDECAR_TABLES)
        | frozenset(spec.name for spec in SYNC_TABLES)
        | frozenset(_ladder_created_tables())
    )


def table_fixes(tables: Iterable[str]) -> list[str]:
    """One line per table not owned by exactly one class: the table name, then the fix."""
    return [
        f"{table}: {FIX} ({problem})"
        for table in sorted(set(tables))
        for problem in coverage_problems((table,), "table")
    ]


def doc_fixes() -> list[str]:
    if DOC_PATH.read_text(encoding="utf-8") == render_markdown():
        return []
    return [f"{DOC_PATH.name} differs from the registry: run {REGEN}"]


def check(tables: Iterable[str]) -> int:
    fixes = table_fixes(tables) + doc_fixes()
    for line in fixes:
        print(line)
    if not fixes:
        print("every schema table maps to exactly one data class")
    return 1 if fixes else 0


if __name__ == "__main__":
    sys.exit(check(schema_tables()))
