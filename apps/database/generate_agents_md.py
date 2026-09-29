"""Generate ``<data-dir>/state/AGENTS.md`` from a live state DB.

Usage::

    python -m apps.database.generate_agents_md --data-dir X

Merges sqlite introspection (``sqlite_master``, ``PRAGMA table_info``,
``PRAGMA foreign_key_list``) against the curated descriptions in
:mod:`apps.database.column_docs`, so the generated file cannot silently omit
a column's meaning: a live column with no curated description makes the
whole run fail (:class:`MissingColumnDocsError`), rather than quietly
generating an incomplete file. That is the drift guard specs/cloudsync-
spec.md D3 asks for.

This is the SECOND half of the two-file AGENTS.md pattern
(specs/cloudsync-spec.md section 2, D3). The FIRST half is
``apps/database/AGENTS.md`` -- authored, git-tracked prose, including the
verbatim founding brief. This module produces the copy that travels with a
specific machine's live DB file and is regenerated whenever migrations run,
so it can never go stale the way a hand-maintained doc can.

``open_rw`` locally imports :func:`apps.database.regenerate_agents_md_if_writable`
after migrations (see ``apps/database/__init__.py``). This module must not
import :mod:`apps.shared.state.db` in return -- even inside :func:`main` --
because that pair is a package cycle the quality gate counts
(``python.package_cycles``). The CLI opens sqlite read-only itself.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from apps.database.column_docs import COLUMN_DOCS, TABLE_DOCS

# PyYAML is imported inside :func:`_table_block`, the one function that
# needs it, rather than here. Everything above the rendering layer --
# :func:`introspect`, :class:`TableInfo`, the fts5 shadow detection -- is
# stdlib sqlite3 only, and scripts/quality_gate.py's sync-drift evaluator
# imports exactly that half from a throwaway environment holding the pinned
# measurement tools and NOTHING of the project's own dependencies
# (ops/quality/requirements.txt says why). A module-level `import yaml` made
# that evaluator die with ModuleNotFoundError in CI while passing in every
# local venv. tests/quality/test_sync_drift_imports.py pins the invariant --
# the drift gate's import graph reaches no third-party package -- so a future
# import that reintroduces the dependency fails there, by name, instead of
# reappearing as a red CI job that names PyYAML rather than drift.

GENERATOR_VERSION: int = 1
"""Bump this whenever :func:`render`, :func:`introspect`, ``_HEADER``, or a
curated docs module (``column_docs.py`` and the modules it merges in) changes
in a way that changes the bytes ``write_agents_md`` produces. The bump is
the code-side half of :func:`agents_md_cache_marker`'s cache key (issue
#4015); the DB-side half is sqlite's own ``PRAGMA schema_version``, so
``open_rw`` regenerates AGENTS.md once after either moves and skips it on
every reopen where neither did, instead of re-running introspection and
YAML rendering on every request that opens a connection.
"""


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    """A content hash of every ``sqlite_master`` row on ``conn``.

    sqlite's ``PRAGMA schema_version`` is only a counter, so two databases
    that ran different DDL (one creates ``pairings``, another
    ``launcher_meta``) can reach the same value. A restored copy of one
    landing beside the other's sidecar would then carry a matching marker
    and skip regeneration past the drift guard (issue #4015 review). The
    hash covers every table, index, view and trigger definition (sqlite
    rewrites a table's stored ``sql`` on ``ALTER TABLE``), so equal
    fingerprints mean equal schemas. One small indexed read, not the
    per-table introspection this cache exists to skip.
    """
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:16]


def agents_md_cache_marker(
    *,
    sqlite_schema_version: int,
    schema_fingerprint: str,
    owned_tables: frozenset[str] | None,
    generator_version: int,
) -> str:
    """The ``schema_meta_markers`` key that gates AGENTS.md regeneration.

    ``sqlite_schema_version`` MUST be a fresh ``PRAGMA schema_version`` read
    on the connection being checked, not this app's own
    :data:`apps.shared.state.schema.SCHEMA_VERSION`. sqlite's schema-version
    counter is bumped by sqlite itself on every DDL statement that touches
    this file -- CREATE/ALTER/DROP TABLE -- whether it ran through this
    app's migration ladder or not (an ad-hoc ``ALTER TABLE`` from a test, a
    foreign-authority table created by another subsystem, a hand-run DDL
    script). The app's own ``SCHEMA_VERSION`` constant only advances on a
    migration and would miss all of those, silently caching a stale
    AGENTS.md past the drift guard's whole purpose
    (:class:`MissingColumnDocsError`, specs/cloudsync-spec.md D3). It costs
    one pragma read, not a walk of ``sqlite_master``, so the cache still
    pays for itself against the introspect-and-render cost this exists to
    skip.

    ``schema_fingerprint`` (:func:`schema_fingerprint`) is the schema's
    CONTENT, and the counter alone is not enough: two different schemas can
    share a counter value.

    ``generator_version`` is passed explicitly (callers pass
    :data:`GENERATOR_VERSION`) so a test can build the marker an OLDER
    generator wrote without rebinding the module constant. It changes
    whenever the code that turns a schema into text changes, and a cache hit
    means none of the three moved since the marker was inserted, so the
    DB's AGENTS.md is still correct.
    """
    tables_part = ",".join(sorted(owned_tables)) if owned_tables else ""
    return (
        f"agents_md_generated:v{generator_version}:"
        f"sqliteschema{sqlite_schema_version}:fp{schema_fingerprint}:{tables_part}"
    )


_FTS5_SHADOW_SUFFIXES: tuple[str, ...] = (
    "_data",
    "_idx",
    "_docsize",
    "_config",
    "_content",
)

_HEADER = """\
# state.db -- generated table reference

GENERATED FILE. Do not hand-edit -- it is overwritten the next time
migrations run against this database file. Curated descriptions live in
`apps/database/column_docs.py` (repo); narrative context and the founding
brief live in `apps/database/AGENTS.md` (repo). Regenerate with:

```
python -m apps.database.generate_agents_md --data-dir <data-dir>
```

One fenced YAML block per table below, introspected from THIS database file
at generation time. `columns` holds the curated one-line meaning of each
column; `schema` holds its raw declared type/constraints as SQLite reports
them right now; `foreign_keys` lists what this table currently references.
fts5 shadow tables (SQLite's own opaque index storage for a virtual table,
not application columns) are omitted.
"""


@dataclass(frozen=True)
class ColumnInfo:
    """One row of ``PRAGMA table_info(<table>)``."""

    name: str
    type: str
    not_null: bool
    default: object
    primary_key: bool


@dataclass(frozen=True)
class ForeignKeyInfo:
    """One row of ``PRAGMA foreign_key_list(<table>)``."""

    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]


class MissingColumnDocsError(RuntimeError):
    """A live table or column has no curated description in column_docs.py.

    This is the drift guard: generating an AGENTS.md that silently omits a
    column's meaning is impossible by construction.
    """


class ForeignAgentsMdError(RuntimeError):
    """``out_path`` exists and is not a generated state.db sidecar.

    ``write_agents_md`` overwrites the traveling generated file on purpose.
    It must not replace a hand-authored AGENTS.md (``apps/database/AGENTS.md``,
    a repo-root Agents.md on a case-insensitive volume, a leftover note)
    sitting in the same directory as a DB opened at a non-standard path.
    """


def _existing_is_generated_sidecar(path: Path) -> bool:
    """True when ``path`` is missing or already a generated sidecar."""
    if not path.exists():
        return True
    with path.open("r", encoding="utf-8") as handle:
        first = handle.readline()
    return first.startswith(_HEADER.splitlines()[0])


# ----- introspection --------------------------------------------------------


def _virtual_table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
    ).fetchall()
    return {row[0] for row in rows}


def _is_fts5_shadow_table(name: str, virtual_tables: set[str]) -> bool:
    """True if ``name`` is a shadow table SQLite auto-generated for an fts5
    virtual table (``<vtab>_data``, ``_idx``, ``_docsize``, ``_config``,
    and, in content-storing mode, ``_content``).

    These hold opaque index-internal storage, not application columns, so
    they are excluded from the drift guard rather than requiring
    meaningless per-column docs. Detected structurally (any known suffix on
    a name derived from a real virtual table in this DB) so a future fts5
    table needs no code change here.
    """
    return any(
        name == f"{vtab}{suffix}" for vtab in virtual_tables for suffix in _FTS5_SHADOW_SUFFIXES
    )


def introspect(
    conn: sqlite3.Connection,
    *,
    owned_tables: frozenset[str] | None = None,
) -> list[TableInfo]:
    """Return every live, documentable table in ``conn``, name-sorted.

    Excludes sqlite-internal tables (``sqlite_%``) and fts5 shadow tables.
    Everything else -- including infrastructure tables like ``schema_meta``
    -- is documentable and therefore subject to the drift guard.

    When ``owned_tables`` is set, live tables outside that set are omitted
    rather than becoming a coverage failure. ``open_rw`` passes
    ``schema.ALL_KNOWN_TABLES`` so a leftover one-shot table
    (``lyric_verdict_legacy``, ``lyric_word_legacy``) cannot abort opening
    the app, while foreign-authority tables stay in the sidecar. The CLI
    leaves this unset, so extras still fail there.
    """
    virtual_tables = _virtual_table_names(conn)
    names = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        if not row[0].startswith("sqlite_") and not _is_fts5_shadow_table(row[0], virtual_tables)
    ]
    if owned_tables is not None:
        names = [name for name in names if name in owned_tables]

    tables: list[TableInfo] = []
    for name in names:
        columns = tuple(
            ColumnInfo(
                name=col[1],
                type=col[2],
                not_null=bool(col[3]),
                default=col[4],
                primary_key=bool(col[5]),
            )
            for col in conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        )
        foreign_keys = tuple(
            ForeignKeyInfo(column=fk[3], ref_table=fk[2], ref_column=fk[4])
            for fk in conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        )
        tables.append(TableInfo(name=name, columns=columns, foreign_keys=foreign_keys))

    return sorted(tables, key=lambda t: t.name)


# ----- coverage check (the drift guard) -------------------------------------


def _missing_descriptions(tables: list[TableInfo]) -> list[str]:
    missing: list[str] = []
    for table in tables:
        if table.name not in TABLE_DOCS:
            missing.append(f"{table.name} (whole table undocumented in TABLE_DOCS)")
            continue
        column_docs = COLUMN_DOCS.get(table.name, {})
        missing.extend(
            f"{table.name}.{column.name}"
            for column in table.columns
            if column.name not in column_docs
        )
    return missing


def _check_coverage(tables: list[TableInfo]) -> None:
    missing = _missing_descriptions(tables)
    if not missing:
        return
    raise MissingColumnDocsError(
        f"apps/database/column_docs.py is missing {len(missing)} "
        "description(s) for tables/columns live in this database. Add "
        "them before regenerating AGENTS.md -- this is the drift guard "
        "doing its job, not a bug:\n" + "\n".join(f"  - {item}" for item in missing)
    )


# ----- rendering -------------------------------------------------------------


def _column_schema_repr(column: ColumnInfo) -> str:
    """Raw declared shape of ``column``, straight from PRAGMA table_info.

    Distinct from the curated meaning in ``columns`` -- this can never
    drift because it is read live, not hand-maintained.
    """
    parts = [column.type or "(no declared type -- BLOB affinity)"]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    if column.not_null:
        parts.append("NOT NULL")
    if column.default is not None:
        parts.append(f"DEFAULT {column.default}")
    return " ".join(parts)


def _table_block(table: TableInfo) -> str:
    # Local, so the introspection half of this module imports no third party;
    # see the note beside the imports at the top of the file.
    import yaml

    payload = {
        table.name: {
            "description": TABLE_DOCS[table.name],
            "columns": {
                column.name: COLUMN_DOCS[table.name][column.name] for column in table.columns
            },
            "schema": {column.name: _column_schema_repr(column) for column in table.columns},
            "foreign_keys": [
                {
                    "column": fk.column,
                    "references_table": fk.ref_table,
                    "references_column": fk.ref_column,
                }
                for fk in table.foreign_keys
            ],
        }
    }
    # width huge: descriptions stay on one line each rather than
    # PyYAML's default 80-col soft-wrap, so the file stays grep-able for a
    # full curated description and diffs cleanly one line per change.
    body = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1_000_000,
    )
    return f"```yaml\n{body}```\n"


def render(tables: list[TableInfo]) -> str:
    """Render the full generated document. Raises on missing coverage."""
    _check_coverage(tables)
    sections = [_HEADER.rstrip(), ""]
    for table in tables:
        sections.append(f"## `{table.name}`")
        sections.append("")
        sections.append(_table_block(table))
    return "\n".join(sections).rstrip() + "\n"


def agents_md_cache_line(marker: str) -> str:
    """The trailing line that ties a written AGENTS.md to its cache marker.

    The marker row lives INSIDE state.db, so it travels with a restored or
    copied database while the sidecar does not. A cache hit therefore needs
    both halves: the row in the DB and this line in the file beside it
    (issue #4015 review). A missing sidecar, or one written for another
    schema, lacks the line and is regenerated.
    """
    return f"<!-- agents-md-cache: {marker} -->"


def sidecar_carries_cache_line(path: Path, marker: str) -> bool:
    """True when ``path`` exists and ends with :func:`agents_md_cache_line`.

    Reads only the file's tail, never the whole sidecar, so the per-open
    cost stays one small read.
    """
    expected = (agents_md_cache_line(marker) + "\n").encode("utf-8")
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size < len(expected):
                return False
            handle.seek(size - len(expected))
            return handle.read() == expected
    except FileNotFoundError:
        return False


def write_agents_md(
    conn: sqlite3.Connection,
    out_path: Path,
    *,
    owned_tables: frozenset[str] | None = None,
    cache_marker: str | None = None,
) -> str:
    """Introspect ``conn``, render, and write to ``out_path``.

    Returns the text written. Raises :class:`MissingColumnDocsError` before
    writing anything if coverage is incomplete -- no partial file is ever
    left behind. Raises :class:`ForeignAgentsMdError` before writing if
    ``out_path`` already exists and does not start with the generated
    header. ``owned_tables`` is forwarded to :func:`introspect`.
    ``cache_marker``, when given (``open_rw``'s path), appends
    :func:`agents_md_cache_line` so a later open can tell this file belongs
    to that marker; the CLI omits it, and its output then regenerates on the
    next ``open_rw``.
    """
    if not _existing_is_generated_sidecar(out_path):
        raise ForeignAgentsMdError(
            f"{out_path} exists and is not a generated state.db sidecar; "
            "refusing to overwrite"
        )
    tables = introspect(conn, owned_tables=owned_tables)
    text = render(tables)
    if cache_marker is not None:
        text += "\n" + agents_md_cache_line(cache_marker) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(
        f".{out_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return text


# ----- CLI --------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.database.generate_agents_md",
        description=(
            "Generate <data-dir>/state/AGENTS.md from a live state DB by "
            "merging sqlite introspection with apps/database/column_docs.py."
        ),
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help=(
            "Data root containing state/state.db, e.g. the value of $MDT_DATA_DIR on this machine."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    db_path = Path(args.data_dir) / "state" / "state.db"
    if not db_path.exists():
        raise FileNotFoundError(
            f"state DB not found at {db_path}; pass the data root that "
            "contains state/state.db (the parent of state/, not the db file)."
        )
    # Open sqlite here, not via apps.shared.state.db.open_ro: open_rw now
    # imports this package, so importing db from here would close a
    # database <-> shared package cycle. This CLI only introspects.
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = ON")
        out_path = Path(args.data_dir) / "state" / "AGENTS.md"
        write_agents_md(conn, out_path)
        print(f"wrote {out_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()


__all__ = [
    "GENERATOR_VERSION",
    "ColumnInfo",
    "ForeignKeyInfo",
    "MissingColumnDocsError",
    "TableInfo",
    "agents_md_cache_line",
    "agents_md_cache_marker",
    "introspect",
    "main",
    "render",
    "sidecar_carries_cache_line",
    "schema_fingerprint",
    "write_agents_md",
]
