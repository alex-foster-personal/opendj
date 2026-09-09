"""The measured subject for :mod:`scripts.sync_drift_lint`.

Separated from the checks so the two questions stay apart: this module answers
WHAT WAS MEASURED, and the linter answers WHAT IS WRONG WITH IT. Getting the
subject wrong is the more dangerous of the two mistakes, because a check over
the wrong database reports zero violations and reads exactly like a clean tree.

THE SUBJECT IS A PRODUCTION-SHAPED state.db, NOT THE LADDER ALONE. Seven
authorities write DDL into that one file; the ladder is one of them. Measured
Wed 9 Sep 2026 on current main: the ladder alone builds 22 tables, a
provisioned database holds 45, so a subject built from the ladder would be
blind to half of itself. Re-derived from scratch on this port rather than
carried over, and that re-derivation found a SEVENTH authority the Tue 1 Sep
2026 list did not have: ``apply_pairing_capture_migrations``, which
apps/webui/server/routes/pairing_capture.py runs on the daemon's writable
state.db the first time a capture is POSTed. Its three tables were declared
nowhere and documented nowhere, so both D-04 and D-08 fired on them the moment
the authority was added here.

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

import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from apps.analysis import store as analysis_store
from apps.database import generate_agents_md
from apps.dedup import schema as dedup_schema
from apps.engine_core.store import schema as engine_schema
from apps.launcher.scripts import bootstrap_db as launcher_bootstrap
from apps.shared import fingerprints
from apps.shared.pairings import schema_sql as pairings_sql
from apps.shared.play_orders import schema as play_orders_schema
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.spotify import state_aux as spotify_aux

#: Name of the production-shaped, migrated state DB inside :class:`Scan`.
STATE_LADDER: str = "shared_state"


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
    """``open_rw``'s schema work without its documentation side effect.

    The production entry point regenerates the state dir's AGENTS.md, and that
    generator RAISES on a docs gap. Building the scan through it would make
    D-04 structurally unable to report: a docs gap would abort the scan before
    any check ran, losing six other measurements to a defect one of them
    exists to name. The pair below is the same schema work, and the backfill
    no-ops on a fresh DB.
    """
    version = state_schema.apply_migrations(conn)
    if version != state_schema.SCHEMA_VERSION:
        raise RuntimeError(
            f"a fresh migration reached v{version}, not "
            f"v{state_schema.SCHEMA_VERSION}. The scan subject is wrong, so "
            f"every check would be measuring the wrong database."
        )
    sync_stamp.backfill_local_machine_id(conn)


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
        lambda conn, _path: pairings_sql.apply_pairing_capture_migrations(conn),
    ),
    Authority(
        "apps/shared/play_orders/schema.py",
        lambda conn, _path: play_orders_schema.apply_play_order_migrations(conn),
    ),
    Authority(
        "apps/spotify/state_aux.py",
        lambda conn, _path: spotify_aux.ensure_aux_tables(conn),
    ),
    Authority("apps/launcher/scripts/bootstrap_db.py", _apply_launcher),
)
"""Every writer of DDL into ``state.db``, in the order production runs them.

The first is the migration ladder; the rest run additively on the same file.
A new authority belongs here the day it is written, or every check silently
stops covering the tables it creates.

NOT here, deliberately: ``apps/sets/state.py``. Its ``SetStore`` defaults to
its OWN file (apps.sets.paths.SETS_DB) and only writes ``sets`` and
``set_events`` into a caller-supplied database through the ``backend=`` hook;
nothing under apps/ constructs one against state.db today (re-checked Wed 9
Sep 2026: ``SetStore(`` has no call site anywhere in the tree), so it is a
possible future state.db authority rather than a current one.
tests/engine_core/test_store_schema.py DOES drive its ``_ensure_schema``,
because the consolidation target has to cover that future. Both tables are
undocumented, so the day a caller appears, D-04 will say so.

Also NOT here, and each checked rather than assumed (Wed 9 Sep 2026, by
reading every module under apps/ that contains a CREATE TABLE):
apps/engine_core/jobs/store.py, apps/lyrics/search_index_schema.py,
apps/shared/hashing.py, apps/sync/fingerprint.py, apps/voice/settings.py and
apps/webui/server/search_index.py each own a SEPARATE sqlite file, and
search_index.py says so in its first line ("never mutate state.db's own
schema"); apps/webui/server/routes/copilot.py builds its projection in
``:memory:``; apps/launcher/scripts/latency_check.py builds a throwaway
benchmark file; and apps/analysis/selection.py's ``ensure_tables`` delegates
to apps/analysis/store.py rather than declaring DDL of its own."""


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
            for name in OTHER_LADDERS:
                other = build_other_ladder(name, root)
                try:
                    ladders[name] = introspect_tables(other)
                finally:
                    other.close()
            yield Scan(ladders=ladders, documented=documented)
        finally:
            state.close()


__all__ = [
    "OTHER_LADDERS",
    "STATE_AUTHORITIES",
    "STATE_LADDER",
    "Authority",
    "Scan",
    "TableFacts",
    "build_other_ladder",
    "build_state_db",
    "introspect_tables",
    "measured_scan",
]
