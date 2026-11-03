"""Migration 25 -> 26: library organization foundation (Library Organization PRD s6).

Adds the storage every organization feature builds on, in ONE wire bump (v8),
so folders, sections, tags and works can ship separately without each forcing
the fleet to upgrade again:

* ``playlists`` gains ``parent_id``, ``kind``, ``stage``, ``set_status`` and
  ``sort_index``. ``kind`` admits ``set`` and ``set_status`` exists now because
  widening a synced CHECK later is itself a wire change.
* Synced tables: ``playlist_section``, ``tag_type``, ``tag_meta``,
  ``tag_synonym``, ``work``, ``work_member``, ``work_dismissal``, ``work_pref``.
* ``undo_journal`` is machine-local: undo is per session, never synced.

No new table has a REFERENCES clause and ``parent_id`` has none either: a
synced child can reach a peer before its parent inside one table's apply, and
that is the FK 409 wedge ``sync_set.py`` describes. The service layer
(:mod:`apps.shared.state.library_org`) enforces the same rules. For the same
reason "one primary work per member" is a service rule, not the partial UNIQUE
index the PRD sketched: two machines each choosing a primary would otherwise
collide on apply with no natural key to resolve against.

Every statement is idempotent, and the step first creates ``pair_transitions``
when it is missing. A data dir that ran main while the reverted #7886 was on it
(06:43Z to 08:16Z Fri 30 Oct 2026) is stamped v25 with these library tables
and WITHOUT transition memory, so it skipped main's v25. This step closes that
gap (#7961) and leaves the library tables it already has as they are: the DDL
is byte-identical to #7886's. ``ALTER TABLE ... ADD COLUMN`` has no IF NOT
EXISTS form, so :func:`apps.shared.state.schema.apply_migrations` skips one
whose column is already there.
"""
from __future__ import annotations

from .migrations_v21 import EXPECTED_TRIGGERS as _V21_TRIGGERS
from .migrations_v21 import WRITE_TOKEN_TABLE, trigger_name, trigger_sql
from .migrations_v25 import _V25 as _TRANSITION_MEMORY_V25

_SYNC_COLUMNS = (
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "origin_device_id TEXT, deleted_at TEXT"
)

#: Fixed stamp on seeded rows, so every machine seeds byte-identical rows and
#: the sync digest agrees without one row crossing the wire.
_SEED_STAMP = "2026-10-30T00:00:00.000000+00:00"

#: Tables v26 seeds rows into. Every v26 machine writes these rows itself.
SEEDED_TABLES: frozenset[str] = frozenset({"tag_type"})


def is_untouched_seed(table: str, row: dict[str, object]) -> bool:
    """A seeded row nobody has edited. Sync leaves it out: every peer has it.

    An edit restamps the row with this machine's id and a new ``updated_at``,
    so it then syncs like any other row.
    """
    return (
        table in SEEDED_TABLES
        and row.get("updated_at") == _SEED_STAMP
        and row.get("origin_device_id") is None
    )


#: The digested tables v26 adds, on top of v21's frozen list.
V26_WRITE_TOKEN_TABLES: tuple[str, ...] = (
    "playlist_section",
    "tag_type",
    "tag_meta",
    "tag_synonym",
    "work",
    "work_member",
    "work_dismissal",
    "work_pref",
)

_V26_TRIGGERS: dict[str, str] = {
    trigger_name(table, event): trigger_sql(table, event)
    for table in V26_WRITE_TOKEN_TABLES
    for event in ("INSERT", "UPDATE", "DELETE")
}

#: Every write-token trigger the digest gate requires at the current schema.
EXPECTED_TRIGGERS: dict[str, str] = {**_V21_TRIGGERS, **_V26_TRIGGERS}

LIBRARY_ORG_DDL: list[str] = [
    "ALTER TABLE playlists ADD COLUMN parent_id TEXT",
    "ALTER TABLE playlists ADD COLUMN kind TEXT NOT NULL DEFAULT 'playlist' "
    "CHECK (kind IN ('folder', 'playlist', 'set'))",
    "ALTER TABLE playlists ADD COLUMN stage TEXT "
    "CHECK (stage IN ('inbox', 'sorting', 'ready', 'archive'))",
    "ALTER TABLE playlists ADD COLUMN set_status TEXT "
    "CHECK (set_status IN ('draft', 'ready', 'performed', 'background'))",
    "ALTER TABLE playlists ADD COLUMN sort_index INTEGER",
    "CREATE INDEX idx_playlists_parent ON playlists(parent_id) "
    "WHERE parent_id IS NOT NULL",
    f"""
    CREATE TABLE playlist_section (
        id TEXT PRIMARY KEY,
        owner_kind TEXT NOT NULL CHECK (owner_kind IN ('playlist', 'set')),
        owner_id TEXT NOT NULL,
        before_position INTEGER NOT NULL CHECK (before_position >= 0),
        title TEXT NOT NULL,
        color TEXT,
        {_SYNC_COLUMNS}
    )
    """,
    "CREATE INDEX idx_playlist_section_owner ON playlist_section(owner_kind, owner_id)",
    f"""
    CREATE TABLE tag_type (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        singular TEXT NOT NULL,
        show_as_column INTEGER NOT NULL DEFAULT 0 CHECK (show_as_column IN (0, 1)),
        show_in_tags INTEGER NOT NULL DEFAULT 1 CHECK (show_in_tags IN (0, 1)),
        column_width INTEGER,
        sort_index INTEGER NOT NULL DEFAULT 0,
        {_SYNC_COLUMNS}
    )
    """,
    *(
        "INSERT INTO tag_type(id, label, singular, show_as_column, show_in_tags, "
        "sort_index, created_at, updated_at) VALUES "
        f"('{tag_id}', '{label}', '{singular}', {column}, {in_tags}, {order}, "
        f"'{_SEED_STAMP}', '{_SEED_STAMP}')"
        for tag_id, label, singular, column, in_tags, order in (
            ("general", "Tags", "Tag", 0, 1, 0),
            ("instrument", "Instruments", "Instrument", 1, 0, 1),
            ("mood", "Moods", "Mood", 0, 1, 2),
        )
    ),
    f"""
    CREATE TABLE tag_meta (
        name_norm TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'general',
        color TEXT,
        source_category TEXT,
        {_SYNC_COLUMNS}
    )
    """,
    f"""
    CREATE TABLE tag_synonym (
        term_norm TEXT NOT NULL,
        tag_name_norm TEXT NOT NULL,
        {_SYNC_COLUMNS},
        PRIMARY KEY (term_norm, tag_name_norm)
    )
    """,
    f"""
    CREATE TABLE work (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('classical', 'song')),
        title TEXT NOT NULL,
        composer TEXT,
        catalogue TEXT,
        movement TEXT,
        aliases TEXT NOT NULL DEFAULT '[]',
        musicbrainz_work_id TEXT,
        iswc TEXT,
        {_SYNC_COLUMNS}
    )
    """,
    f"""
    CREATE TABLE work_member (
        work_id TEXT NOT NULL,
        member_kind TEXT NOT NULL CHECK (member_kind IN ('track', 'edit')),
        member_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN
            ('original', 'recording', 'remix', 'cover', 'edit', 'live', 'mashup')),
        is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
        sort_index INTEGER NOT NULL DEFAULT 0,
        {_SYNC_COLUMNS},
        PRIMARY KEY (work_id, member_kind, member_id)
    )
    """,
    "CREATE INDEX idx_work_member_member ON work_member(member_kind, member_id)",
    f"""
    CREATE TABLE work_dismissal (
        a_member TEXT NOT NULL,
        b_member TEXT NOT NULL,
        work_id TEXT,
        {_SYNC_COLUMNS},
        PRIMARY KEY (a_member, b_member)
    )
    """,
    f"""
    CREATE TABLE work_pref (
        work_id TEXT PRIMARY KEY,
        top_member_id TEXT NOT NULL,
        {_SYNC_COLUMNS}
    )
    """,
    """
    CREATE TABLE undo_journal (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        seq INTEGER NOT NULL,
        op TEXT NOT NULL,
        before TEXT NOT NULL,
        after TEXT NOT NULL,
        undone_at TEXT
    )
    """,
    "CREATE INDEX idx_undo_journal_session ON undo_journal(session_id, seq)",
    *(
        f"INSERT INTO {WRITE_TOKEN_TABLE}(table_name, token) VALUES ('{table}', randomblob(16))"
        for table in V26_WRITE_TOKEN_TABLES
    ),
    *_V26_TRIGGERS.values(),
]
"""The library organization DDL as #7886 shipped it, before :func:`_idempotent`."""


def _idempotent(statement: str) -> str:
    """The same statement, safe to re-run. SQLite stores DDL without IF NOT EXISTS."""
    return (
        statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
        .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        .replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ")
        .replace("INSERT INTO ", "INSERT OR IGNORE INTO ")
    )


_V26: list[str] = [
    *(_idempotent(statement) for statement in _TRANSITION_MEMORY_V25),
    *(_idempotent(statement) for statement in LIBRARY_ORG_DDL),
]

__all__ = [
    "EXPECTED_TRIGGERS",
    "LIBRARY_ORG_DDL",
    "SEEDED_TABLES",
    "V26_WRITE_TOKEN_TABLES",
    "_V26",
    "is_untouched_seed",
]
