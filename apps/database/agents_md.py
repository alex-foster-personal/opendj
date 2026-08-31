"""Render AGENTS.md for a state database, from the database itself.

WHY THIS IS GENERATED, NOT WRITTEN

A hand-maintained schema document is wrong within a month and nobody notices,
because nothing fails when it drifts. This introspects the live file, so the
column list, the types, the foreign keys and the indexes are whatever is
actually there. Only the one-line "what is a row of this" descriptions are
hand-written, and a table missing one is rendered as ``UNDOCUMENTED`` rather
than quietly omitted.

WHY ``AGENTS.md``

That filename is the emerging cross-vendor convention for agent-readable
project context, so an agent that has never seen this project looks for it by
name. It is deliberately NOT ``README.md``: this file is for a reader about to
write a query, not for a human browsing the repo.

WHY IT IS WRITTEN NEXT TO THE DATABASE

Per the CLOUDSYNC brief it must "travel with" the db. A copy in the repo would
describe the schema of whatever the repo is at, not the schema of the file an
agent has actually been handed, and those differ the moment a machine is one
migration behind. Writing it beside the file makes it correct by construction.

USAGE

    python -m apps.database.agents_md                      # default state db
    python -m apps.database.agents_md --db PATH --out PATH
    python -m apps.database.agents_md --check              # non-zero if stale
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.database.descriptions import TABLES

#: Generic public requirements; the private founding correspondence is withheld.
BRIEF: str = """CloudSync uses one database for non-audio library metadata, with documented
tables, fields and relationships. Generate AGENTS.md beside that database so
its schema description travels with the file and matches the installed schema.
Storage policy distinguishes metadata synchronization from audio and stem
placement, and selects pinned, cached, streamed or excluded assets per machine.
Library and vocal-region metadata are synchronized independently of whether
playlist audio or large stem files are stored locally."""

UNDOCUMENTED = "**UNDOCUMENTED** - add a line to `apps/database/descriptions.py`."


@dataclass(frozen=True)
class Column:
    name: str
    type_: str
    notnull: bool
    pk: bool
    default: str | None


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[str, ...]
    indexes: tuple[str, ...]
    rows: int


# ----- introspection ------------------------------------------------------


def _shadow_prefixes(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Name prefixes owned by virtual tables, e.g. ``tracks_fts_`` for FTS5.

    An FTS5 table called ``tracks_fts`` causes SQLite to create five more
    tables beside it (``_config``, ``_content``, ``_data``, ``_docsize``,
    ``_idx``). They are engine internals with no stable contract, they are
    invisible to the migration ladder that declared the schema, and nobody
    should ever query them. Documenting them would put five permanently
    UNDOCUMENTED entries in the output and make the undocumented count
    useless as a signal.

    Derived from the virtual tables actually present rather than hardcoded,
    so a second FTS table added later is handled without touching this.
    """
    return tuple(
        f"{row[0]}_"
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
    )


def _table_names(conn: sqlite3.Connection) -> list[str]:
    """Every table worth documenting: real tables plus virtual ones.

    Excludes SQLite's own ``sqlite_%`` tables and the shadow tables belonging
    to virtual tables. The virtual table itself IS included: it is a real
    queryable object.
    """
    shadows = _shadow_prefixes(conn)
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if not row[0].startswith(shadows)
    ]


def _read_table(conn: sqlite3.Connection, name: str) -> Table:
    columns = tuple(
        Column(
            name=row[1],
            type_=row[2] or "(untyped)",
            notnull=bool(row[3]),
            default=row[4],
            pk=bool(row[5]),
        )
        # PRAGMA cannot be parameterised. The name comes from sqlite_master
        # in the same file, so it is not user input, but quote it anyway.
        for row in conn.execute(f'PRAGMA table_info("{name}")')
    )
    foreign_keys = tuple(
        f"`{row[3]}` -> `{row[2]}`.`{row[4] or 'rowid'}`"
        f"{' ON DELETE ' + row[6] if row[6] and row[6] != 'NO ACTION' else ''}"
        for row in conn.execute(f'PRAGMA foreign_key_list("{name}")')
    )
    indexes = tuple(
        f"`{row[1]}`{' (unique)' if row[2] else ''}"
        for row in conn.execute(f'PRAGMA index_list("{name}")')
    )
    rows = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    return Table(name, columns, foreign_keys, indexes, rows)


def read_schema(db_path: Path) -> list[Table]:
    """Every table in ``db_path``, with columns, keys, indexes and row counts."""
    if not db_path.is_file():
        raise FileNotFoundError(
            f"no database at {db_path}. This tool documents a REAL file; it "
            "will not render a schema from the migration ladder, because the "
            "point is to describe the database an agent was actually handed."
        )
    # Read-only: documenting a database must never be able to change it.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [_read_table(conn, name) for name in _table_names(conn)]
    finally:
        conn.close()


# ----- rendering ----------------------------------------------------------


def _render_table(table: Table) -> list[str]:
    out = [f"### `{table.name}`", "", TABLES.get(table.name, UNDOCUMENTED), ""]
    out += [f"{table.rows:,} rows.", ""]
    out += ["| column | type | null | key | default |", "|---|---|---|---|---|"]
    for col in table.columns:
        key = "PK" if col.pk else ""
        default = f"`{col.default}`" if col.default is not None else ""
        out.append(
            f"| `{col.name}` | {col.type_} | "
            f"{'no' if col.notnull or col.pk else 'yes'} | {key} | {default} |"
        )
    out.append("")
    if table.foreign_keys:
        out += ["Foreign keys: " + ", ".join(table.foreign_keys), ""]
    else:
        # Stated rather than omitted: the brief asked for strong foreign keys,
        # so a table without any is a finding, not a blank space.
        out += ["Foreign keys: none.", ""]
    if table.indexes:
        out += ["Indexes: " + ", ".join(table.indexes), ""]
    return out


def render(db_path: Path, tables: list[Table]) -> str:
    """The whole document, as markdown."""
    documented = sum(1 for t in tables if t.name in TABLES)
    with_fks = sum(1 for t in tables if t.foreign_keys)
    lines = [
        "# AGENTS.md - the Open DJ state database",
        "",
        "GENERATED by `python -m apps.database.agents_md`. Do not hand-edit: "
        "every table, column, key and count below was read out of the database "
        "file sitting beside this document, so editing this file only makes it "
        "wrong. The one-line table descriptions come from "
        "`apps/database/descriptions.py`; that is the file to edit.",
        "",
        f"- database: `{db_path.name}`",
        f"- tables: {len(tables)} ({documented} described, "
        f"{len(tables) - documented} undocumented)",
        f"- tables carrying at least one foreign key: {with_fks} of {len(tables)}",
        "",
        "## How to read this",
        "",
        "This is a LOCAL-FIRST database. It is the single store for everything "
        "that is not an audio file: library, playlists, analysis, curation, "
        "sessions, settings and sign-in. Audio never lives here, only paths to "
        "it (`track_locations`).",
        "",
        "Two rules worth knowing before writing a query:",
        "",
        "1. `tracks` is the anchor. A track is a musical work, NOT a file and "
        "NOT a vendor's id. Files hang off `track_locations`, vendor ids off "
        "`track_vendor_ids`, and per-adapter opinions off `track_fields`. This "
        "is why the same song imported from rekordbox and from djay is one row "
        "here and not two.",
        "2. Never quote a coverage figure against a denominator that includes "
        "tracks with no resolvable audio. Name the denominator. See "
        "`docs/library-availability.md`.",
        "",
        "## Tables",
        "",
    ]
    for table in tables:
        lines += _render_table(table)
    lines += [
        "## CloudSync database requirements",
        "",
        "```",
        BRIEF,
        "```",
        "",
        "Where the database lives on each machine is answered in "
        "`specs/cloudsync-spec.md` (decision D1): the OS application-support "
        "directory, overridable with `MDT_DATA_DIR`.",
        "",
    ]
    return "\n".join(lines)


# ----- cli ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db", type=Path, default=Path("data/state/state.db"),
        help="database to document (default: data/state/state.db)",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="output path (default: AGENTS.md beside the database)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="exit non-zero if the file on disk differs from what would be "
             "written, and change nothing",
    )
    args = parser.parse_args(argv)

    tables = read_schema(args.db)
    out_path = args.out or args.db.parent / "AGENTS.md"
    content = render(args.db, tables)

    if args.check:
        current = out_path.read_text() if out_path.is_file() else ""
        if current == content:
            print(f"[OK] {out_path} is current ({len(tables)} tables)")
            return 0
        print(
            f"[ERROR] {out_path} is stale or missing. The database has moved "
            f"on from the document beside it. Regenerate: "
            f"python -m apps.database.agents_md --db {args.db}",
            file=sys.stderr,
        )
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content)
    undocumented = [t.name for t in tables if t.name not in TABLES]
    print(f"[OK] wrote {out_path} ({len(tables)} tables)")
    if undocumented:
        print(
            f"[WARN] {len(undocumented)} table(s) have no description and "
            f"render as UNDOCUMENTED: {', '.join(undocumented)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
