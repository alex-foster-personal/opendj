"""sync_drift_lint.py -- catch the sync-schema mistakes that are silent forever.

Every failure below is an OMISSION, and an omission raises nothing. Forget to
bump ``SCHEMA_VERSION`` and ``apply_migrations`` short-circuits, so the new DDL
never runs and no test notices. Forget to add a table to ``SYNC_TABLES`` and it
simply never syncs: no error, no digest entry, no divergence report, because
nothing ever looks at it. The only way to see an omission is to compare two
declarations that should agree and find that they do not.

DIRECTION MATTERS. Every consistency check already in this repo runs registry
-> schema: it walks a declared list and asks whether the database has it.
Nothing ran schema -> registry, and that is the direction that catches a table
which EXISTS, carries the sync columns, and was never registered. Check D-01
is that direction, and it is the highest-value check in this file.

WHAT IS MEASURED lives in :mod:`scripts.sync_drift_subject`: a
production-shaped ``state.db`` built by all EIGHT of its schema authorities
(46 tables, where the migration ladder alone gives 22), plus the four other
ladders, every one really built and read back through ``PRAGMA table_info``.
Read that module before trusting a zero here, because a check over the wrong
subject reports zero and reads like a clean tree. Two of those eight were
found by re-deriving the list rather than carrying it over, and D-04 and D-08
fired on their tables the moment each was added -- the eighth being the one
written in Rust, which every Python-only derivation had been structurally
unable to see. The list is now DECLARED, in
scripts/sync_drift_rules.py DDL_SOURCE_FILES, and compared against a fresh
scan of the tracked tree in every language AND every directory: a lens
scoped to apps/ and scripts/ would hide the next one the same way ``*.py``
hid the last.

Requirements (status key: -> out-of-scope, ? todo, OK done, RUN done+ran,
REG done+ran+regression-tested):

REG  D-01 Flag a live table carrying the sync columns that no registry lists.
          The schema -> registry direction; the missing one.
          [if a synced-looking table is added and not registered then the run
           exits 1]
          [if it is added to SYNC_TABLES then the run exits 0]
          [if a changelog gains deleted_at then its exemption stops applying
           and the run exits 1]
REG  D-02 Flag a table registered for row-level LWW that the state DB does not
          have, or has without the full sync trio. The registry -> schema
          direction.
          [if SYNC_TABLES names a table no authority creates then exits 1]
          [if a registered table lacks deleted_at then exits 1]
REG  D-03 Flag SCHEMA_VERSION disagreeing with len(MIGRATIONS). A ladder step
          appended without a bump never runs and never complains.
          [if a step is appended and the version is not bumped then exits 1]
          [if both move together then exits 0]
REG  D-04 Flag a live table or column with no entry in table_docs.py /
          column_docs.py. Deliberately duplicates the guard on the DB-open
          path, which only fires when migrations ADVANCE and is therefore
          one-shot. This one runs every time and names each gap.
          [if a column is added without a docs entry then exits 1]
REG  D-05 Flag DEFAULT CURRENT_TIMESTAMP on any column of any ladder. SQLite
          writes a stamp without a UTC offset -- naive, unorderable, and it
          quarantines the row out of CloudSync forever. Minting that BY DDL
          DEFAULT is the P0 class at the schema level.
          [if a ladder declares that default then exits 1]
          [if the column defaults to NULL, a literal, or a UTC strftime call
           then exits 0]
REG  D-06 Flag engine_core's LEGACY_SHARED_STATE_VERSION disagreeing with the
          shared-state SCHEMA_VERSION it mirrors. Exempts only a stuck mirror
          value written down in MIRROR_VERSION_DEBT, and only while the
          source is ahead of it, so moving the mirror re-arms it.
          [if the shared ladder is bumped and the mirror is not then exits 1]
          [if the mirror moves off the value the debt entry names then exits 1]
          [if the mirror gets AHEAD of the source then exits 1]
          [if a step is appended and SCHEMA_VERSION bumped in lockstep, with
           the mirror untouched, then exits 0 -- an ordinary migration must
           not inherit schema work on a dormant consolidation target]
          [if a debt entry names a mirror that is no longer live then its test
           fails, so the list cannot go stale in silence]
REG  D-07 Flag a SHIPPED migration step whose content changed. The wire digest
          hashes each synced table's COLUMN LIST, and the peer handshake pins
          only SCHEMA_VERSION, so editing a step that already ran on a real
          database diverges two peers permanently while both report the same
          version -- and the resulting SyncDigestMismatch is the alarm ADR 04
          reserves for a merge bug, so the schema edit arrives wearing a
          disguise.
          [if a recorded step's statements change then exits 1]
          [if a step is appended without recording its fingerprint then exits 1]
REG  D-08 Flag a table a schema authority creates in state.db that neither
          inventory declares (apps/shared/state/schema.py's tuples, or
          apps/database's table docs). D-01's direction one layer out. It
          reads zero today; what it changed is the tripwire test in
          tests/shared/state, which hand-copied three of the authorities and so
          never compared the rest of their tables to anything.
          [if an authority creates an undeclared table then exits 1]
REG  D-09 Refuse to report a clean scan that measured nothing, or that ran
          fewer checks than are declared. Lives in
          scripts/sync_drift_floors.py. Zero is both a value and an error
          signature (.claude/rules/verification.md), so every floor here is
          derived from a declaration, never remembered.
          [if a check is unwired from CHECKS then the run aborts]
          [if the state DB lacks a table ALL_KNOWN_TABLES declares then the run
           aborts rather than printing PASS]
          [if the docs introspection misses one live table then the run
           aborts, fts5 shadow tables excepted]
REG  D-12 Flag a synced row shape that moved without a WIRE_VERSION bump. The
          handshake gates on WIRE_VERSION (apps/sync_hub/wire_version.py), so
          a same-wire peer on another schema must present the same rows.
          [if a ladder step adds a synced column and no bump then exits 1]
          [if it only touches a machine-local table then exits 0]

->   D-10 Run the checks against a DEPLOYED database, not only a freshly
          provisioned one.
->   D-11 Give a deliberately machine-local table an auditable escape
          hatch from D-01.
          Both are OPEN and both are the maintainer's call; the measurements behind
          them, and why neither was half-done here, are in
          scripts/sync_drift_rules.py.

Usage:
    python -m scripts.sync_drift_lint              # run every check
    python -m scripts.sync_drift_lint --list-facts # show what was measured
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass

from apps.database import column_docs as docs
from apps.engine_core.store import schema as engine_schema
from apps.shared.state import schema as state_schema
from apps.sync_hub import protocol_common, wire_version
from scripts.sync_drift_floors import assert_checks_wired as _assert_checks_wired
from scripts.sync_drift_floors import assert_measurable
from scripts.sync_drift_rules import (
    CFG,
    MIRROR_VERSION_DEBT,
    NAIVE_DEFAULT_ALLOWLIST,
    RULES,
    SHIPPED_MIGRATIONS,
)
from scripts.sync_drift_subject import Scan, measured_scan

# ----- violations ------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    rule: str
    subject: str
    detail: str

    def render(self) -> str:
        return f"{self.rule}: {self.subject}: {self.detail}"


def lww_tables() -> frozenset[str]:
    """Tables the protocol syncs ROW BY ROW, so each needs the full trio.

    ``MEMBERSHIP_TABLE`` counts: it is registered, just at whole-playlist
    granularity rather than row-level LWW (ADR 04 c5), and it is in
    ``DIGEST_TABLES`` carrying the trio like the rest.
    """
    return frozenset(
        [spec.name for spec in protocol_common.SYNC_TABLES] + [protocol_common.MEMBERSHIP_TABLE]
    )


def handled_tables() -> frozenset[str]:
    """Every table SOME registry in ``protocol_common`` handles.

    Wider than :func:`lww_tables` by exactly ``REGISTRY_TABLE`` (``machines``),
    which the protocol transports but does not digest or stamp: ``pk_columns``
    and ``table_columns`` accept it and raise ``'<x>' is not in the sync set``
    for anything else, so it IS in the sync set, just without LWW columns.
    Splitting the two sets is what lets D-01 stop reporting a table that is
    handled without D-02 demanding a trio the registry does not use. Folding
    them together in either direction produces a false report, and the fastest
    way to get a gate switched off is a false report.
    """
    return lww_tables() | {protocol_common.REGISTRY_TABLE}


# ----- checks ----------------------------------------------------------------


def check_unregistered_synced_table(scan: Scan) -> list[Violation]:
    """D-01. Schema -> registry: a synced-looking table nobody registered."""
    handled = handled_tables()
    out: list[Violation] = []
    for table in scan.state.values():
        if not table.has(protocol_common.UPDATED_AT, protocol_common.ORIGIN_DEVICE_ID):
            continue
        if table.name in handled:
            continue
        if table.name in CFG.CHANGELOG_EXEMPT and not table.has(protocol_common.DELETED_AT):
            continue
        out.append(
            Violation(
                "unregistered_synced_table",
                table.name,
                f"carries {protocol_common.UPDATED_AT} + "
                f"{protocol_common.ORIGIN_DEVICE_ID} but no registry in "
                f"apps/sync_hub/protocol_common.py lists it",
            )
        )
    return out


def check_registered_table_missing(scan: Scan) -> list[Violation]:
    """D-02. Registry -> schema: a registered table the authorities do not build."""
    out: list[Violation] = []
    for name in sorted(lww_tables()):
        table = scan.state.get(name)
        if table is None:
            out.append(
                Violation(
                    "registered_table_missing",
                    name,
                    "registered for sync but no schema authority creates it",
                )
            )
            continue
        missing = [c for c in protocol_common.SYNC_COLUMNS if c not in table.columns]
        if missing:
            out.append(
                Violation(
                    "registered_table_missing",
                    name,
                    f"registered for sync but missing {', '.join(missing)}",
                )
            )
    return out


def check_version_ladder_mismatch(_scan: Scan) -> list[Violation]:
    """D-03. A migration step appended without a version bump never runs.

    Takes the scan it does not read, because :data:`CHECKS` dispatches every
    check through one signature. Comparing two declarations needs no database.
    """
    declared = state_schema.SCHEMA_VERSION
    steps = len(state_schema.MIGRATIONS)
    if declared == steps:
        return []
    return [
        Violation(
            "version_ladder_mismatch",
            "apps/shared/state/schema.py",
            f"SCHEMA_VERSION is {declared} but MIGRATIONS holds {steps} step(s); "
            f"{'the last step never runs' if steps > declared else 'a step is missing'}",
        )
    ]


def check_undocumented_table_or_column(scan: Scan) -> list[Violation]:
    """D-04. A live table or column with no curated description."""
    out: list[Violation] = []
    for table in scan.documented:
        if table.name not in docs.TABLE_DOCS:
            out.append(
                Violation(
                    "undocumented_table_or_column",
                    table.name,
                    "whole table has no entry in apps/database/table_docs.py",
                )
            )
            continue
        known = docs.COLUMN_DOCS.get(table.name, {})
        out.extend(
            Violation(
                "undocumented_table_or_column",
                f"{table.name}.{column.name}",
                "column has no entry in apps/database/column_docs.py",
            )
            for column in table.columns
            if column.name not in known
        )
    return out


_QUOTED_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")
_TIMESTAMP_TOKEN = re.compile(r"\bCURRENT_TIMESTAMP\b", re.IGNORECASE)
_ISO_UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def mints_naive_stamp(conn: sqlite3.Connection, default: str) -> bool:
    """True when a DEFAULT expression MINTS a stamp CloudSync cannot place.

    MEASURED, NOT PATTERN-MATCHED. The expression is evaluated in sqlite and
    the VALUE it produces is inspected for the good thing: a sortable
    ISO-8601 UTC stamp with an explicit zone. Token matching answered a
    different question and got it wrong in the developer-hostile direction --
    ``DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', CURRENT_TIMESTAMP))`` mints
    exactly the stamp this rule asks for and was reported as a violation, so
    the check rejected the correct fix for the thing it was complaining
    about, with no escape hatch short of editing the linter.

    The token prefilter stays, and is load-bearing rather than an
    optimization: it is what limits the value test to the expressions this
    rule is ABOUT. Without it a ``DEFAULT 0`` produces an int, fails the ISO
    test, and gets reported as a naive stamp. Quoted literals are stripped
    first, so a default that merely CONTAINS the word is a string rather than
    a call.

    An expression that cannot be evaluated RAISES. A default this check could
    not measure must not be rendered as either verdict.
    """
    if not _TIMESTAMP_TOKEN.search(_QUOTED_LITERAL.sub("", default)):
        return False
    try:
        value = conn.execute(f"SELECT {default}").fetchone()[0]
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"could not evaluate the DEFAULT expression {default!r}: {exc}. "
            "It names CURRENT_TIMESTAMP, so it is exactly what D-05 is about, "
            "and reporting it either way would be a verdict over a "
            "measurement that did not happen."
        ) from exc
    return not (isinstance(value, str) and _ISO_UTC_STAMP.match(value))


def check_naive_stamp_default(scan: Scan) -> list[Violation]:
    """D-05. DEFAULT CURRENT_TIMESTAMP mints an unorderable naive stamp."""
    out: list[Violation] = []
    probe = sqlite3.connect(":memory:")
    for ladder in sorted(scan.ladders):
        for table in scan.ladders[ladder].values():
            for column, default in sorted(table.defaults.items()):
                if default is None or not mints_naive_stamp(probe, default):
                    continue
                if (ladder, table.name, column) in NAIVE_DEFAULT_ALLOWLIST:
                    continue
                out.append(
                    Violation(
                        "naive_stamp_default",
                        f"{ladder}:{table.name}.{column}",
                        f"DEFAULT {default}",
                    )
                )
    return out


def check_mirror_version_mismatch(_scan: Scan) -> list[Violation]:
    """D-06. engine_core's mirror of the shared-state terminal version.

    Scan-free for the same reason as D-03: two declarations, no database.

    :data:`MIRROR_VERSION_DEBT` exempts a STUCK MIRROR that has been
    explained in writing, never the rule. It is keyed on the mirror's exact
    value, and the exemption applies only while the mirror is BEHIND: move
    the mirror and the key stops matching, so the next drift fires even
    though a dated entry is sitting right there.

    Keyed on the mirror rather than on the ``(mirror, source)`` pair, which
    is what it was, because the pair form detonated on the next unrelated
    change. The debt is that the consolidated ladder has not caught up with
    the shared ladder; bumping SCHEMA_VERSION for some new migration does not
    create a second, different debt, it leaves the same one unpaid one step
    further behind. Under pair-keying the very next routine migration -- step
    appended, version bumped in lockstep, nothing else wrong -- inherited a
    blocking demand to do schema work on a dormant consolidation target it
    never touched, against a hard-zero gate. The two realistic exits from
    that are doing someone else's schema work or re-keying the allowlist, and
    the second is the reflex that makes gates decorative.

    What is given up is small and named: a mirror stuck at the SAME value for
    a DIFFERENT reason no longer re-arms the check. What is kept is the part
    that matters, that any movement of the mirror itself does, and a
    parametrized test asserts each entry still names the LIVE mirror with the
    source still ahead of it, so a paid-off debt fails as loudly as a new one.
    """
    mirror = engine_schema.LEGACY_SHARED_STATE_VERSION
    source = state_schema.SCHEMA_VERSION
    if mirror == source or (source > mirror and mirror in MIRROR_VERSION_DEBT):
        return []
    return [
        Violation(
            "mirror_version_mismatch",
            "apps/engine_core/store/schema.py",
            f"LEGACY_SHARED_STATE_VERSION is {mirror} but "
            f"apps/shared/state/schema.py SCHEMA_VERSION is {source}",
        )
    ]


_LAYOUT = re.compile(r"\s*([(),])\s*|\s+")


def migration_fingerprint(step: list[str]) -> str:
    """Digest of one migration step, blind to layout and nothing else.

    Reindenting a triple-quoted DDL string changes no column, and a
    fingerprint that tripped on it would be re-recorded on reflex, which
    protects nothing. So whitespace runs collapse and the space around
    brackets and commas is dropped. The one thing this cannot see is
    whitespace INSIDE a quoted literal, which by construction cannot change a
    table's column list -- the only thing the wire digest hashes.
    """
    body = "\n".join(
        _LAYOUT.sub(lambda m: m.group(1) or " ", statement).strip() for statement in step
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def check_migration_step_changed(_scan: Scan) -> list[Violation]:
    """D-07. A shipped migration step is immutable once it has run anywhere."""
    out: list[Violation] = []
    for version, step in enumerate(state_schema.MIGRATIONS, start=1):
        measured = migration_fingerprint(step)
        recorded = SHIPPED_MIGRATIONS.get(version)
        if recorded is None:
            out.append(
                Violation(
                    "migration_step_changed",
                    f"apps/shared/state/schema.py v{version}",
                    f"has no fingerprint in SHIPPED_MIGRATIONS; record "
                    f"{version}: {measured!r} once the step is final",
                )
            )
        elif recorded != measured:
            out.append(
                Violation(
                    "migration_step_changed",
                    f"apps/shared/state/schema.py v{version}",
                    f"recorded {recorded}, measured {measured}",
                )
            )
    out.extend(
        Violation(
            "migration_step_changed",
            f"apps/shared/state/schema.py v{version}",
            "is recorded in SHIPPED_MIGRATIONS but the ladder no longer has that step",
        )
        for version in sorted(SHIPPED_MIGRATIONS)
        if version > len(state_schema.MIGRATIONS)
    )
    return out


def declared_state_tables() -> frozenset[str]:
    """Every table a provisioned ``state.db`` is declared to hold, anywhere.

    The declaration is SPLIT in two, deliberately: ``ALL_KNOWN_TABLES`` covers
    what apps/shared/state/schema.py and the authorities it names create,
    while the sibling apps writing to the same connection are self-declaring
    through apps/database's docs modules. Their UNION is what "declared"
    means.

    The two OVERLAP heavily, and an earlier version of this docstring said
    the opposite -- that tests/database/test_agents_md_generator.py asserts
    they are disjoint. That test asserts EQUALITY
    (``set(TABLE_DOCS) == documentable``, where ``documentable`` already
    contains ALL_KNOWN_TABLES minus the fts5 shadows), which FORCES overlap.
    Measured Wed 9 Sep 2026: 38 names in ALL_KNOWN_TABLES, 41 in TABLE_DOCS,
    33 in both. The practical consequence is the one D-08's remediation now
    states: declaring a new table in one inventory alone leaves that equality
    unsatisfied, so a table needs BOTH a declaration and docs.
    """
    return frozenset(state_schema.ALL_KNOWN_TABLES) | frozenset(docs.TABLE_DOCS)


def check_undeclared_state_table(scan: Scan) -> list[Violation]:
    """D-08. Schema -> declaration: a state table no inventory names.

    D-01's direction one layer out, measured against
    :data:`~scripts.sync_drift_subject.STATE_AUTHORITIES` rather than a
    hand-copied list. The hand-copied list is how the gap stayed invisible:
    the tripwire test in tests/shared/state provisioned three of the eight
    authorities, so the tables the other five create were never compared to
    any inventory at all.
    """
    declared = declared_state_tables()
    return [
        Violation(
            "undeclared_state_table",
            name,
            "created in state.db by a schema authority but named neither in "
            "apps/shared/state/schema.py ALL_KNOWN_TABLES nor in "
            "apps/database's table docs",
        )
        for name in sorted(set(scan.state) - declared)
    ]


def check_wire_shape_changed(_scan: Scan) -> list[Violation]:
    """D-12. Its subject is a fresh run of the shared ladder, which builds
    every synced table; the Scan carries no PRAGMA detail beyond columns."""
    drift = wire_version.fresh_ladder_drift()
    return [] if drift is None else [
        Violation("wire_shape_changed", "apps/sync_hub/wire_version.py", drift)
    ]


CHECKS: tuple[tuple[str, Callable[[Scan], list[Violation]]], ...] = (
    ("unregistered_synced_table", check_unregistered_synced_table),
    ("registered_table_missing", check_registered_table_missing),
    ("version_ladder_mismatch", check_version_ladder_mismatch),
    ("undocumented_table_or_column", check_undocumented_table_or_column),
    ("naive_stamp_default", check_naive_stamp_default),
    ("mirror_version_mismatch", check_mirror_version_mismatch),
    ("migration_step_changed", check_migration_step_changed),
    ("undeclared_state_table", check_undeclared_state_table),
    ("wire_shape_changed", check_wire_shape_changed),
)


def assert_checks_wired() -> None:
    """D-09's wiring arm, over THIS module's CHECKS and RULES.

    A thin binding rather than a re-export: the floor has to read the
    dispatch table of the module that owns it, and reading it here is what
    lets a test unwire a check in this namespace and watch the floor fire.
    """
    _assert_checks_wired(sys.modules[__name__])


@dataclass(frozen=True)
class Result:
    """What one scan measured. ``checks_run`` is the control, not a score."""

    violations: tuple[Violation, ...]
    checks_run: int
    tables_scanned: int
    ladders_scanned: int

    def counts(self) -> dict[str, int]:
        """One count per DECLARED rule, so a rule that found nothing still
        reports a zero rather than vanishing from the readout."""
        return {r: sum(1 for v in self.violations if v.rule == r) for r in sorted(RULES)}


def run(scan: Scan) -> Result:
    """Run every declared check over ``scan``, refusing an unmeasured one."""
    assert_checks_wired()
    assert_measurable(scan)
    violations: list[Violation] = []
    for rule, check in CHECKS:
        found = check(scan)
        wrong = [v for v in found if v.rule != rule]
        if wrong:
            raise RuntimeError(
                f"check {rule!r} emitted a violation labelled {wrong[0].rule!r}; "
                "the readout would attribute it to the wrong gate"
            )
        violations.extend(found)
    return Result(
        violations=tuple(violations),
        checks_run=len(CHECKS),
        tables_scanned=len(scan.state),
        ladders_scanned=len(scan.ladders),
    )


# ----- main ------------------------------------------------------------------


def _print_facts(scan: Scan) -> None:
    for ladder in sorted(scan.ladders):
        tables = scan.ladders[ladder]
        print(f"{ladder}: {len(tables)} table(s)")
        for name, facts in sorted(tables.items()):
            print(f"    {name} ({len(facts.columns)} columns)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list-facts", action="store_true",
                        help="print every table measured, per ladder, and exit")
    args = parser.parse_args(argv)

    with measured_scan() as scan:
        if args.list_facts:
            _print_facts(scan)
            return 0
        result = run(scan)

    for violation in sorted(result.violations, key=lambda v: (v.rule, v.subject)):
        print(violation.render())
        print(f"    {RULES[violation.rule]}")

    control = (
        f"{result.checks_run}/{len(CHECKS)} checks over "
        f"{result.tables_scanned} state tables in {result.ladders_scanned} ladders"
    )
    if result.violations:
        print(f"\n[sync-drift] FAIL: {len(result.violations)} violation(s); {control}.")
        return 1
    print(f"[sync-drift] PASS: 0 violations; {control}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
