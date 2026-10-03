"""The measured subject for :mod:`scripts.sync_drift_lint`.

Separated from the checks so the two questions stay apart: this module answers
WHAT WAS MEASURED, and the linter answers WHAT IS WRONG WITH IT. Getting the
subject wrong is the more dangerous of the two mistakes, because a check over
the wrong database reports zero violations and reads exactly like a clean tree.

THE SUBJECT IS A PRODUCTION-SHAPED state.db, NOT THE LADDER ALONE. EIGHT
authorities write DDL into that one file; the ladder is one of them. Measured
Wed 9 Sep 2026 on current main: the ladder alone builds 22 tables, a
provisioned database holds 46, so a subject built from the ladder would be
blind to half of itself.

The list was re-derived twice, and each pass found an authority the previous
one had missed, which is the whole argument for :data:`DDL_SOURCE_FILES`
below. The Tue 1 Sep 2026 list missed ``apply_pairing_capture_migrations``,
the SECOND ladder inside apps/shared/pairings/schema_sql.py, which
apps/webui/server/routes/pairing_capture.py runs on the daemon's writable
state.db the first time a capture is POSTed. The re-derivation that found it
worked by reading every module under apps/ containing a ``CREATE TABLE`` --
a PYTHON-ONLY lens, and so structurally unable to see the EIGHTH authority:
apps/launcher/src-tauri/src/state.rs, which runs ``CREATE TABLE IF NOT
EXISTS launcher_meta`` against the same data/state/state.db on every launcher
start. Both omissions were silent in the same way and D-04 and D-08 fired on
their tables the moment each authority was added.

The lens is now a declaration rather than a method: ``DDL_SOURCE_FILES`` in
scripts/sync_drift_rules.py names every file in the tree that declares a
``CREATE TABLE`` in ANY language, with one line saying whether it writes
state.db, and tests/quality/test_sync_drift_authorities.py compares that
declaration against a fresh scan of the tracked tree -- the WHOLE tree, in
every language AND every directory, because a lens scoped to apps/ and
scripts/ would hide the next authority exactly the way ``*.py`` hid the last.
A new DDL writer fails by NAME there, in the direction that can actually
catch an omission, rather than waiting for someone to re-derive the list a
third time.

The inventory a state table must appear in is SPLIT across two modules:
``apps/shared/state/schema.py`` names what the ladder and the authorities it
knows about create (ALL_KNOWN_TABLES), and ``apps/database``'s docs modules
name every table the generated AGENTS.md describes. The two OVERLAP heavily
(tests/database/test_agents_md_generator.py pins the docs side to the schema
tuples plus the consolidated engine's own domains), so D-08 checks their
UNION: a table is declared if either module names it.

FACTS COME FROM A DATABASE, NEVER FROM SOURCE TEXT. Every ladder here is
really built and then read back through ``PRAGMA table_info`` and
``sqlite_master``. A builder that parsed the DDL strings would be one more
declaration to keep in agreement, which is the bug class the linter exists to
catch.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from apps.analysis import queue_stale as analysis_queue_stale
from apps.analysis import queue_store as analysis_queue_store
from apps.analysis import store as analysis_store
from apps.database import generate_agents_md
from apps.dedup import schema as dedup_schema
from apps.engine_core.store import schema as engine_schema
from apps.launcher.scripts import bootstrap_db as launcher_bootstrap
from apps.shared import fingerprints
from apps.shared.pairings import schema_sql as pairings_sql
from apps.shared.play_orders import schema as play_orders_schema
from apps.shared.playlist_sets import schema as playlist_sets_schema
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.spotify import state_aux as spotify_aux
from apps.sync_hub import engine_identity_map
from apps.webui.server import pairings_sqlite

#: Name of the production-shaped, migrated state DB inside :class:`Scan`.
STATE_LADDER: str = "shared_state"

#: Repo root, so a non-importable authority can be read from source.
REPO_ROOT: Path = Path(__file__).resolve().parents[1]


# ----- measured facts --------------------------------------------------------


@dataclass(frozen=True)
class TableFacts:
    """One live table, read out of a real database."""

    name: str
    columns: tuple[str, ...]
    #: column -> its declared DEFAULT expression, verbatim, or None.
    defaults: dict[str, str | None]

    def has(self, *columns: str) -> bool:
        return set(columns) <= set(self.columns)


@dataclass(frozen=True)
class Scan:
    """Every fact the checks read, measured once.

    ``ladders`` maps a ladder name to the tables it builds. ``documented`` is
    the state DB seen through the AGENTS.md generator's own introspection, so
    D-04 inherits its structural exclusions (fts5 shadow tables are sqlite's
    opaque index storage, not application columns).
    """

    ladders: dict[str, dict[str, TableFacts]]
    documented: list[generate_agents_md.TableInfo]
    #: Virtual tables in the state DB, measured from ``sqlite_master``. The
    #: floors need these to derive which tables the docs introspection is
    #: ENTITLED to drop, using the generator's own structural detector rather
    #: than a hardcoded name.
    virtual_tables: frozenset[str]

    @property
    def state(self) -> dict[str, TableFacts]:
        return self.ladders[STATE_LADDER]


def introspect_tables(conn: sqlite3.Connection) -> dict[str, TableFacts]:
    """Every non-internal table in ``conn``, with its columns and defaults."""
    names = [
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not str(row[0]).startswith("sqlite_")
    ]
    facts: dict[str, TableFacts] = {}
    for name in sorted(names):
        rows = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        if not rows:
            raise RuntimeError(
                f"PRAGMA table_info({name}) returned no rows for a table "
                f"sqlite_master lists. The introspection is broken, not the schema."
            )
        facts[name] = TableFacts(
            name=name,
            columns=tuple(str(row[1]) for row in rows),
            defaults={str(row[1]): (None if row[4] is None else str(row[4])) for row in rows},
        )
    return facts


# ----- the state DB, as production provisions it -----------------------------


@dataclass(frozen=True)
class Authority:
    """One schema authority that writes DDL into ``state.db``.

    Named rather than inlined so a reader can see the whole provisioning set
    at once, and so :func:`build_state_db` can say which one raised.
    """

    name: str
    provision: Callable[[sqlite3.Connection, Path], None]


def _apply_state_ladder(conn: sqlite3.Connection, _path: Path) -> None:
    """``open_rw``'s schema work, called directly rather than through it.

    The pair below IS what apps/shared/state/db.py open_rw does to a fresh
    file, plus an assertion that the ladder reached its terminal version; the
    rest of open_rw is path resolution and pragmas this scan supplies itself.

    RE-CHECKED Fri 11 Sep 2026: as of this date
    apps.database.regenerate_agents_md_if_writable IS wired in open_rw (after
    migrations + backfill). This ladder still calls apply_migrations and
    backfill_local_machine_id directly ON PURPOSE: routing through open_rw
    would let a docs gap abort the scan before D-04 can report the very
    defect it exists to name. The direct call is the smallest thing that
    provisions the ladder without that side effect.
    """
    version = state_schema.apply_migrations(conn)
    if version != state_schema.SCHEMA_VERSION:
        raise RuntimeError(
            f"a fresh migration reached v{version}, not "
            f"v{state_schema.SCHEMA_VERSION}. The scan subject is wrong, so "
            f"every check would be measuring the wrong database."
        )
    sync_stamp.backfill_local_machine_id(conn)


# Rust escapes that survive into a SQL literal. Anything else after a
# backslash is passed through as itself, which is what Rust does for `\'`.
_RUST_ESCAPES: dict[str, str] = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
_RUST_LINE_CONTINUATION = re.compile(r"\\\n[ \t]*")
_RUST_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)
_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\b", re.IGNORECASE)

#: The launcher's Rust module that opens state.db and creates a table in it.
RUST_STATE_AUTHORITY: Path = REPO_ROOT / "apps/launcher/src-tauri/src/state.rs"


def _unescape_rust(literal: str) -> str:
    return re.sub(r"\\(.)", lambda m: _RUST_ESCAPES.get(m.group(1), m.group(1)), literal)


def rust_ddl_statements(source: Path) -> list[str]:
    """Every ``CREATE TABLE`` literal a Rust module hands to sqlite.

    An authority this scan cannot IMPORT is still an authority. The launcher
    runs ``CREATE TABLE IF NOT EXISTS launcher_meta`` against
    ``<repo>/data/state/state.db`` -- ``get_db_path`` prefers that file
    whenever it exists, and ``commands::hotkey`` reaches it on every start
    through ``claim_first_run_notification`` -- so the table is really there
    in the live database of anyone who has opened the app, and every
    Python-only derivation of the authority list was structurally blind to
    it.

    The DDL is READ from the source and EXECUTED, so sqlite parses it and
    this module does not: the facts still come from a database. What is
    parsed here is only the Rust string literal wrapping it.

    Finding nothing is a hard error, not an empty list. Zero statements is
    exactly what a reformatted or moved source looks like, and an authority
    that silently contributes no tables is the failure mode this whole file
    exists to prevent.
    """
    text = _RUST_LINE_CONTINUATION.sub("", source.read_text(encoding="utf-8"))
    statements = [
        _unescape_rust(match.group(1))
        for match in _RUST_STRING_LITERAL.finditer(text)
        if _CREATE_TABLE.search(match.group(1))
    ]
    if not statements:
        raise RuntimeError(
            f"found no CREATE TABLE literal in {source}, which is declared a "
            "schema authority. Either it stopped creating tables (remove it "
            "from STATE_AUTHORITIES and DDL_SOURCE_FILES) or this extraction "
            "broke; an empty contribution would leave its tables unmeasured "
            "while the scan still read clean."
        )
    return statements


def _apply_rust_launcher_meta(conn: sqlite3.Connection, _path: Path) -> None:
    """Run the launcher's own DDL, verbatim, on the shared connection."""
    for statement in rust_ddl_statements(RUST_STATE_AUTHORITY):
        conn.executescript(statement)


def _apply_pairing_capture(conn: sqlite3.Connection, _path: Path) -> None:
    """Named rather than a lambda: it returns a version this scan discards,
    and a lambda body cannot say so without failing the type check."""
    pairings_sql.apply_pairing_capture_migrations(conn)


def _apply_play_orders(conn: sqlite3.Connection, _path: Path) -> None:
    """Named for the same reason as :func:`_apply_pairing_capture`."""
    play_orders_schema.apply_play_order_migrations(conn)


def _apply_playlist_sets(conn: sqlite3.Connection, _path: Path) -> None:
    playlist_sets_schema.apply_playlist_set_migrations(conn)


def _apply_launcher(conn: sqlite3.Connection, path: Path) -> None:
    """The launcher's additive fts5 + frecency DDL, applied by path.

    ``apply_launcher_migration`` opens its own connection, so the caller's is
    committed first. Both are autocommit, and the launcher's backfill no-ops
    because a freshly migrated DB has no tracks.
    """
    conn.commit()
    launcher_bootstrap.apply_launcher_migration(path)


STATE_AUTHORITIES: tuple[Authority, ...] = (
    Authority("apps/shared/state/schema.py", _apply_state_ladder),
    # Module-private because the module exposes no public ensure_*; this is
    # the entry point production uses (apps/analysis/store.py, on the shared
    # connection) and the one tests/engine_core/test_store_schema.py drives.
    Authority(
        "apps/analysis/store.py",
        lambda conn, _path: analysis_store._ensure_analysis_tables(conn),
    ),
    # The backfill queue, additive on the same state.db. Production reaches
    # it from two places -- the analysis_backfill routes and apps.analysis
    # .queue_cli -- and both call this one function, so one authority covers
    # both call sites.
    Authority(
        "apps/analysis/queue_store.py",
        lambda conn, _path: analysis_queue_store.ensure_queue_tables(conn),
    ),
    # analysis_stale is a SEPARATE FILE'S table, and ensure_queue_tables above
    # already runs its DDL. Listed anyway, and run directly, because the
    # declaration this list is compared against reads files: a file calling
    # itself an authority that nothing here provisions is exactly the hole
    # test_every_state_authority_is_declared_as_one exists to refuse. Both
    # statements are IF NOT EXISTS, so running it twice provisions once.
    Authority(
        "apps/analysis/queue_stale.py",
        lambda conn, _path: analysis_queue_stale.ensure_stale_table(conn),
    ),
    Authority(
        "apps/shared/pairings/schema_sql.py",
        lambda conn, _path: pairings_sql.ensure_phase08_tables(conn),
    ),
    # The SECOND entry point in that same module, and a separate authority:
    # one module, two ladders, and listing only the first is how three tables
    # stayed invisible. apps/webui/server/routes/pairing_capture.py runs it on
    # the daemon's WRITABLE state.db, because PairingCaptureRepo defaults to
    # ensure_schema=True, so the first POST to a capture route creates
    # pairing_sync_snapshots, pairing_alignments and
    # pairing_capture_schema_meta in the live file.
    Authority(
        "apps/shared/pairings/schema_sql.py::apply_pairing_capture_migrations",
        _apply_pairing_capture,
    ),
    Authority(
        "apps/webui/server/pairings_sqlite.py",
        lambda conn, _path: pairings_sqlite.ensure_http_pairings_table(conn),
    ),
    Authority("apps/shared/play_orders/schema.py", _apply_play_orders),
    Authority("apps/shared/playlist_sets/schema.py", _apply_playlist_sets),
    Authority(
        "apps/spotify/state_aux.py",
        lambda conn, _path: spotify_aux.ensure_aux_tables(conn),
    ),
    Authority("apps/launcher/scripts/bootstrap_db.py", _apply_launcher),
    # NOT PYTHON, and that is the point. See rust_ddl_statements above: the
    # desktop launcher creates launcher_meta in the same state.db on every
    # start, and no derivation restricted to *.py could ever have seen it.
    Authority("apps/launcher/src-tauri/src/state.rs", _apply_rust_launcher_meta),
    # apps/sync_hub/client.py runs this against the same shared connection on
    # every sync round (prepare_spoke_identity), to hold identity-collapse
    # remaps across batched hub_apply calls. Not in the sync set itself.
    Authority(
        "apps/sync_hub/engine_identity_map.py",
        lambda conn, _path: engine_identity_map.ensure_identity_remap_table(conn),
    ),
)
"""Every writer of DDL into ``state.db``, in the order production runs them.

The first is the migration ladder; the rest run additively on the same file.
A new authority belongs here the day it is written, or every check silently
stops covering the tables it creates.

WHICH FILES ARE AND ARE NOT AUTHORITIES IS DECLARED, NOT NARRATED.
``DDL_SOURCE_FILES`` in scripts/sync_drift_rules.py holds every file in the
tracked tree that declares a ``CREATE TABLE``, in any language, each with one
line saying whether it writes state.db and why. A test compares that
declaration against a fresh scan, so a new DDL writer of any kind fails by
name rather than waiting to be noticed. The prose that used to live here
listed the exclusions instead, and it went wrong in both available
directions: it omitted a whole language, and it recorded a re-check for
``SetStore(`` -- a name that appears nowhere in this repository except in the
sentence you are reading, the class being ``SetsState`` -- so the zero it read
as evidence could never have been anything else
(.claude/rules/verification.md). Re-derived with the real name, and recorded
as the COMMAND rather than as its answer, so the next reader re-measures
instead of inheriting a number: ``git grep -n 'SetsState(' -- apps`` names
every construction site, and each takes either no argument or an explicit
``db_path``, both of which resolve to apps.sets.paths.SETS_DB. Only
tests/sets/test_state.py hands it a state.db backend, so apps/sets/state.py is
genuinely not a state.db authority today. The conclusion survived; the
evidence for it did not."""


def build_state_db(path: Path) -> sqlite3.Connection:
    """Provision a state DB at ``path`` the way production does, and open it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    for authority in STATE_AUTHORITIES:
        try:
            authority.provision(conn, path)
        except Exception as exc:
            raise RuntimeError(
                f"schema authority {authority.name} failed to provision the "
                f"scan subject: {exc!r}. A partially built subject would read "
                f"as a clean tree."
            ) from exc
    return conn


# ----- ladders that are not state.db -----------------------------------------


def _build_engine_core_durable(_tmp: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    engine_schema.apply_migrations(conn)
    return conn


def _build_engine_core_cache(_tmp: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    engine_schema.apply_cache_migrations(conn)
    return conn


def _build_dedup(tmp: Path) -> sqlite3.Connection:
    """The LIVE dedup schema, run today by apps/tags and apps/dedup."""
    return dedup_schema.ensure_schema(tmp / "dedup" / "phase7.sqlite")


def _build_fingerprint_cache(tmp: Path) -> sqlite3.Connection:
    """The LIVE chromaprint cache schema, built by its own constructor."""
    path = tmp / "cache" / "fingerprints.sqlite"
    fingerprints.FingerprintCache(path)
    return sqlite3.connect(str(path))


OTHER_LADDERS: dict[str, Callable[[Path], sqlite3.Connection]] = {
    # Dormant: the consolidation target, not wired up yet.
    "engine_core_durable": _build_engine_core_durable,
    "engine_core_cache": _build_engine_core_cache,
    # LIVE, and outside state.db. Present because the naive-stamp defaults
    # these two mint are the originals that engine_core copied verbatim; a
    # debt list naming only the dormant copies would read as the complete
    # inventory while the executing half went unmeasured.
    "dedup": _build_dedup,
    "fingerprints": _build_fingerprint_cache,
}


def build_other_ladder(name: str, tmp: Path) -> sqlite3.Connection:
    """Build one non-state ladder so its DDL can be introspected."""
    builder = OTHER_LADDERS.get(name)
    if builder is None:
        raise RuntimeError(f"no builder for ladder {name!r}")
    return builder(tmp)


@contextmanager
def measured_scan() -> Iterator[Scan]:
    """Build every ladder, read the facts, and tear the databases down."""
    with tempfile.TemporaryDirectory(prefix="sync-drift-lint-") as tmp:
        root = Path(tmp)
        state = build_state_db(root / "state" / "state.db")
        try:
            ladders = {STATE_LADDER: introspect_tables(state)}
            documented = generate_agents_md.introspect(state)
            virtual = frozenset(generate_agents_md._virtual_table_names(state))
            for name in OTHER_LADDERS:
                other = build_other_ladder(name, root)
                try:
                    ladders[name] = introspect_tables(other)
                finally:
                    other.close()
            yield Scan(ladders=ladders, documented=documented, virtual_tables=virtual)
        finally:
            state.close()


__all__ = [
    "OTHER_LADDERS",
    "REPO_ROOT",
    "RUST_STATE_AUTHORITY",
    "STATE_AUTHORITIES",
    "STATE_LADDER",
    "Authority",
    "Scan",
    "TableFacts",
    "build_other_ladder",
    "build_state_db",
    "introspect_tables",
    "measured_scan",
    "rust_ddl_statements",
]
