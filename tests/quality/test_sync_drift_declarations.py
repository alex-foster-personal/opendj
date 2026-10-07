"""D-06, D-07 and D-08 must each be able to FAIL: the DECLARATION checks.

Split out of test_sync_drift_lint.py (the 600-line file-size gate), which
keeps the checks that read the database's SHAPE. The three here compare two
DECLARATIONS instead -- a mirrored version constant, a recorded migration
fingerprint, a table inventory -- and each gets the same treatment as its
siblings: an INJECTED defect that must be reported exactly, and a negative
control on the real tree that must report zero.

The dated MIRROR_VERSION_DEBT list gets the extra treatment the naive-default
allowlist gets next door. Its entries are asserted to name the mirror that is
actually live, with the source still ahead of it, so a paid-off debt fails as
loudly as a new drift rather than sitting there absorbing the next one.

The ORDINARY MIGRATION is run for real rather than simulated with a patched
constant. D-06 is a HARD-ZERO gate carrying a dated exemption, and the thing
that has to be proved about it is a SILENCE -- that appending a step and
bumping SCHEMA_VERSION in lockstep does not detonate it on an author who
touched nothing D-06 is about. A silence over a subject that was never built
is indistinguishable from a check that is simply off, so the step is really
applied by the real apply_migrations to a real sqlite file, D-04 and D-08 are
asserted to SEE the new table, and D-06 is then shown to still fire in that
same world once the mirror moves off the invariant the entry describes.

Whether the AUTHORITY LIST behind all of this is complete is the other half,
and it lives next door in test_sync_drift_authorities.py.

Regression lines:
  - if the mirror moves off the value MIRROR_VERSION_DEBT names
    and D-06 stays silent then broken
  - if a MIRROR_VERSION_DEBT entry names a mirror that is no longer live then
    the debt list has gone stale, so broken
  - if a debt entry carries no unblock order then the exemption is permanent,
    so broken
  - if an appended step and a lockstep SCHEMA_VERSION bump make D-06 or D-03
    fire then the gate detonates on routine work, so broken
  - if that same appended step is not visible to D-04 and D-08 then the
    subject was never built and the silences above measured nothing, so broken
  - if the mirror moves off the invariant in that same world and D-06 stays
    silent then broken
  - if a shipped migration step's statements change and D-07 stays silent
    then broken
  - if a step is appended with no fingerprint recorded and D-07 stays silent
    then broken
  - if an authority creates a table no inventory declares and D-08 stays
    silent then broken
"""

from __future__ import annotations

import copy

import pytest

from apps.engine_core.store import schema as engine_schema
from apps.shared.state import schema as state_schema
from scripts import sync_drift_lint as lint
from scripts import sync_drift_rules as rules
from scripts import sync_drift_subject as subject


@pytest.fixture(scope="module")
def clean_scan() -> lint.Scan:
    """The real tree, measured once. Pure data, so tests may copy and mutate."""
    with lint.measured_scan() as scan:
        return copy.deepcopy(scan)


@pytest.fixture
def scan(clean_scan: lint.Scan) -> lint.Scan:
    """A private copy of the real scan, safe to inject defects into."""
    return copy.deepcopy(clean_scan)


def _facts(name: str, columns: tuple[str, ...], **defaults: str) -> subject.TableFacts:
    return subject.TableFacts(
        name=name,
        columns=columns,
        defaults={column: defaults.get(column) for column in columns},
    )


def _rules(violations: list[lint.Violation]) -> list[tuple[str, str]]:
    return sorted((v.rule, v.subject) for v in violations)


# ----- D-06 mirror_version_mismatch -------------------------------------------


def test_d06_fires_when_the_mirror_moves_off_the_value_written_down(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the MIRROR re-arms the check, debt entry sitting there or not.

    The live tree carries a dated entry keyed on mirror 7. Moving the mirror
    to a value nobody wrote down must report: if the exemption were keyed on
    the rule rather than on the mirror, this would come back empty and the
    check would be off for good.
    """
    monkeypatch.setattr(engine_schema, "LEGACY_SHARED_STATE_VERSION", 3)
    assert engine_schema.LEGACY_SHARED_STATE_VERSION not in lint.MIRROR_VERSION_DEBT

    found = lint.check_mirror_version_mismatch(scan)

    assert _rules(found) == [("mirror_version_mismatch", "apps/engine_core/store/schema.py")]


def test_d06_fires_when_the_mirror_gets_ahead_of_the_source(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry excuses a mirror that is BEHIND, never one that is ahead.

    Without the direction test, an entry keyed on the mirror value would also
    swallow the opposite defect -- a mirror claiming a version the shared
    ladder has not reached -- which is a different and worse claim.
    """
    monkeypatch.setattr(state_schema, "SCHEMA_VERSION", 6)
    assert engine_schema.LEGACY_SHARED_STATE_VERSION in lint.MIRROR_VERSION_DEBT
    assert engine_schema.LEGACY_SHARED_STATE_VERSION > state_schema.SCHEMA_VERSION

    found = lint.check_mirror_version_mismatch(scan)

    assert _rules(found) == [("mirror_version_mismatch", "apps/engine_core/store/schema.py")]


#: A real migration step, in the shape the next one will actually take: DDL
#: that sqlite parses, no sync columns (so D-01 has nothing to say), and no
#: DEFAULT CURRENT_TIMESTAMP (so D-05 has nothing to say). What is left is
#: exactly the version bookkeeping D-03, D-06 and D-07 are about.
_NEXT_STEP: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS gig_notes (
        gig_id     TEXT PRIMARY KEY,
        note       TEXT NOT NULL,
        written_at TEXT
    )
    """,
]


def _the_next_ordinary_migration(monkeypatch: pytest.MonkeyPatch) -> lint.Scan:
    """Append :data:`_NEXT_STEP`, bump SCHEMA_VERSION in lockstep, and BUILD.

    Not a monkeypatched constant standing in for a migration: the appended
    step is run by the real ``apply_migrations`` against a real sqlite file,
    and the scan handed back is what the checks would measure on the day
    somebody lands it. That matters because the two things being proved here
    are a SILENCE (D-06 must not fire), and a silence over an unbuilt subject
    is indistinguishable from a broken check.

    ``_apply_state_ladder`` refuses to hand back a database whose ladder did
    not reach SCHEMA_VERSION, so the lockstep half of "append and bump in
    lockstep" is enforced by the build rather than asserted afterwards: get
    it wrong and this helper raises instead of returning a subject.
    """
    appended = [*state_schema.MIGRATIONS, _NEXT_STEP]
    monkeypatch.setattr(state_schema, "MIGRATIONS", appended)
    monkeypatch.setattr(state_schema, "SCHEMA_VERSION", len(appended))
    with lint.measured_scan() as scan:
        return copy.deepcopy(scan)


def test_d04_and_d08_see_the_appended_step_so_the_d06_silence_is_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for every silence below: the new table is really there.

    D-04 and D-08 are the two checks that SHOULD have something to say about
    a brand new undocumented, undeclared table, and they say it. Their firing
    is what makes the neighboring zeros measurements rather than the sound
    of a scan that never saw the change -- and it is also the honest shape of
    the routine migration: the author picks up docs and inventory work they
    can act on, and does NOT pick up schema work on a dormant consolidation
    target they never touched.
    """
    scan = _the_next_ordinary_migration(monkeypatch)

    assert len(state_schema.MIGRATIONS) == state_schema.SCHEMA_VERSION
    assert scan.state["gig_notes"].columns == ("gig_id", "note", "written_at")
    assert ("undeclared_state_table", "gig_notes") in _rules(
        lint.check_undeclared_state_table(scan)
    )
    assert ("undocumented_table_or_column", "gig_notes") in _rules(
        lint.check_undocumented_table_or_column(scan)
    )


def test_d06_and_d03_do_not_detonate_on_the_next_ordinary_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this keying exists to prevent, run for real.

    Appending a migration and bumping SCHEMA_VERSION in lockstep is an
    ORDINARY change that touches nothing D-06 is about. Under the old
    (mirror, source) keying it moved the pair off the dated entry and fired a
    hard-zero gate at the author, demanding they teach a dormant
    consolidation target three views they had never heard of. The debt is
    unchanged by that bump: the mirror is still stuck at the same value, for
    the same written reason, one step further behind.

    D-03 is asserted alongside it because the two failures look identical
    from the outside -- a red hard-zero gate on a routine change -- and only
    one of them would ever be the author's to fix.
    """
    scan = _the_next_ordinary_migration(monkeypatch)

    assert lint.check_mirror_version_mismatch(scan) == []
    assert lint.check_version_ladder_mismatch(scan) == []


def test_d07_asks_only_for_the_new_step_fingerprint_on_that_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one thing the routine migration DOES owe, and nothing more.

    Split from the silences next door so each test carries the rule token of
    every check that can make it fail: scripts/sync_drift_mutation_control.py
    blinds one check at a time and asserts the failures land in that check's
    own tests, so a test asserting on D-07 under a D-06 name would abort the
    harness rather than prove anything.
    """
    scan = _the_next_ordinary_migration(monkeypatch)

    found = lint.check_migration_step_changed(scan)

    # The version is READ, not written down: this file used to say v9, and the
    # day v9 actually shipped the assertion started describing v10 while still
    # naming v9. That is the same value-rot D-06 was re-keyed to avoid, one
    # level up, in this gate's own tests.
    assert _rules(found) == [
        (
            "migration_step_changed",
            f"apps/shared/state/schema.py v{state_schema.SCHEMA_VERSION}",
        )
    ]
    assert "has no fingerprint" in found[0].detail, (
        "D-07's one ask of a new step is that its fingerprint be recorded. "
        "Any other complaint here would be the gate inventing work."
    )


def test_d03_d06_and_d07_are_all_silent_once_the_new_step_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end state of the routine change: all three version checks silent.

    D-07's single ask is answered the way its own message says to answer it,
    and nothing else is outstanding on the version axis. If re-keying D-06
    had merely moved the detonation somewhere else, this is where it would
    show.
    """
    scan = _the_next_ordinary_migration(monkeypatch)
    monkeypatch.setattr(
        lint,
        "SHIPPED_MIGRATIONS",
        {
            **lint.SHIPPED_MIGRATIONS,
            # The APPENDED step's own version, read off the patched ladder.
            # A literal 9 here recorded the fingerprint against the step
            # BELOW the new one once v9 shipped, so D-07 reported both a
            # mismatch on v9 and a missing fingerprint on v10 -- and the
            # silence this test exists to prove became noise.
            state_schema.SCHEMA_VERSION: lint.migration_fingerprint(_NEXT_STEP),
        },
    )

    assert lint.check_version_ladder_mismatch(scan) == []
    assert lint.check_mirror_version_mismatch(scan) == []
    assert lint.check_migration_step_changed(scan) == []


@pytest.mark.parametrize("side", ["behind-and-unexcused", "ahead"])
def test_d06_is_still_live_inside_that_same_ordinary_migration(
    monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    """The silence above is the keying, not a dead check.

    Same appended-migration world, same debt entry sitting there, and the
    mirror moved off the invariant the entry describes: one value BEHIND the
    source but written down nowhere, one AHEAD of it, which no entry may ever
    excuse. Both report. Without this, the three zeros above would read the
    same whether D-06 works or was accidentally switched off by the re-keying.

    Both mirror values are DERIVED from the ladder rather than parametrized as
    literals. ``[3, 10]`` was the original pair, and 10 stopped being "ahead"
    the moment v9 shipped -- the test would then have passed for the wrong
    reason, on a mirror sitting exactly ON the source.
    """
    scan = _the_next_ordinary_migration(monkeypatch)
    mirror = 3 if side == "behind-and-unexcused" else state_schema.SCHEMA_VERSION + 1
    # The control on the parameter itself: each side really is the side it
    # claims, against the SCHEMA_VERSION this world actually has.
    if side == "ahead":
        assert mirror > state_schema.SCHEMA_VERSION
    else:
        assert mirror < state_schema.SCHEMA_VERSION
        assert mirror not in rules.MIRROR_VERSION_DEBT
    monkeypatch.setattr(engine_schema, "LEGACY_SHARED_STATE_VERSION", mirror)

    found = lint.check_mirror_version_mismatch(scan)

    assert _rules(found) == [("mirror_version_mismatch", "apps/engine_core/store/schema.py")]
    assert f"LEGACY_SHARED_STATE_VERSION is {mirror}" in found[0].detail
    assert f"SCHEMA_VERSION is {state_schema.SCHEMA_VERSION}" in found[0].detail


def test_d06_goes_quiet_only_for_the_mirror_value_written_down(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the two firing tests above.

    Same injected mirror, this time with its value added to the debt list:
    silent. That is what proves those tests fire because the mirror is
    unlisted, not because the check is broken in some other way.
    """
    monkeypatch.setattr(engine_schema, "LEGACY_SHARED_STATE_VERSION", 3)
    monkeypatch.setattr(
        lint, "MIRROR_VERSION_DEBT", {**lint.MIRROR_VERSION_DEBT, 3: "test entry"}
    )

    assert lint.check_mirror_version_mismatch(scan) == []


@pytest.mark.parametrize("mirror", sorted(lint.MIRROR_VERSION_DEBT))
def test_d06_debt_entries_still_name_a_live_and_still_behind_mirror(mirror: int) -> None:
    """The debt list may only shrink, and it may not lie.

    Without this, D-06's zero is ambiguous: it reads the same whether the
    explained mismatch is still there or was fixed and the entry left behind
    to swallow the NEXT one. Both halves are asserted -- the entry names the
    mirror that is actually live, and the source is still ahead of it, so the
    debt it describes is still unpaid.
    """
    live = engine_schema.LEGACY_SHARED_STATE_VERSION

    assert mirror == live, (
        f"MIRROR_VERSION_DEBT names mirror {mirror}, but the live mirror is "
        f"{live}. Either the debt was paid (delete the entry) or the mirror "
        "moved and the entry no longer describes anything (delete it, and let "
        "D-06 report the new state)."
    )
    assert mirror < state_schema.SCHEMA_VERSION, (
        f"MIRROR_VERSION_DEBT excuses mirror {mirror} for being BEHIND the "
        f"shared ladder, but SCHEMA_VERSION is {state_schema.SCHEMA_VERSION}. "
        "The entry no longer describes the situation it was written for."
    )


def test_d06_debt_entry_states_its_unblock_order() -> None:
    """An exemption without an unblock order is a permanent exemption.

    The reason this pair is carried rather than fixed is that bumping the
    mirror alone would assert the consolidated ladder is caught up to v8 when
    it builds none of v8's three views, so the entry has to say what has to
    happen first.
    """
    for reason in lint.MIRROR_VERSION_DEBT.values():
        assert "UNBLOCK ORDER" in reason
        assert "view" in reason


def test_d06_remediation_names_the_order_that_makes_the_bump_safe() -> None:
    """Bumping the mirror alone makes the new step silently never run.

    ``_stamp_legacy_counters`` writes schema_meta rows up to the mirrored
    version, so stamping a version whose DDL the consolidated ladder does not
    create short-circuits apply_migrations. Advice that omits the order mints
    the omission this file exists to catch.
    """
    remediation = lint.RULES["mirror_version_mismatch"]

    assert "FIRST" in remediation
    assert "test_object_names_match_" in remediation
    assert "short-circuit" in remediation


def test_d06_clean_against_the_real_tree(clean_scan: lint.Scan) -> None:
    assert lint.check_mirror_version_mismatch(clean_scan) == []


# ----- D-07 migration_step_changed --------------------------------------------


def test_d07_fires_when_a_shipped_step_is_edited_in_place(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent peer divergence: same reported version, different columns."""
    edited = [list(step) for step in state_schema.MIGRATIONS]
    edited[0].append("ALTER TABLE tracks ADD COLUMN mood TEXT")
    monkeypatch.setattr(state_schema, "MIGRATIONS", edited)

    found = lint.check_migration_step_changed(scan)

    assert _rules(found) == [("migration_step_changed", "apps/shared/state/schema.py v1")]
    assert lint.SHIPPED_MIGRATIONS[1] in found[0].detail


def test_d07_fires_when_a_new_step_records_no_fingerprint(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    added = [*state_schema.MIGRATIONS, ["CREATE TABLE IF NOT EXISTS gig_notes (x TEXT)"]]
    monkeypatch.setattr(state_schema, "MIGRATIONS", added)
    monkeypatch.setattr(state_schema, "SCHEMA_VERSION", len(added))

    found = lint.check_migration_step_changed(scan)

    version = f"apps/shared/state/schema.py v{len(added)}"
    assert _rules(found) == [("migration_step_changed", version)]
    assert "has no fingerprint" in found[0].detail


def test_d07_fires_when_the_record_outlives_the_step(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_schema, "MIGRATIONS", list(state_schema.MIGRATIONS)[:-1])

    found = lint.check_migration_step_changed(scan)

    version = f"apps/shared/state/schema.py v{len(state_schema.MIGRATIONS) + 1}"
    assert (("migration_step_changed", version)) in _rules(found)


def test_d07_fingerprint_pins_statements_not_formatting() -> None:
    """Reindenting a DDL string changes no column, so it must not fire.

    A fingerprint that tripped on whitespace would be re-recorded on reflex,
    and a record that gets re-recorded on reflex protects nothing.
    """
    plain = ["CREATE TABLE t (a TEXT, b TEXT)"]
    reindented = ["\n    CREATE TABLE t (\n        a TEXT,\n        b TEXT\n    )\n"]
    altered = ["CREATE TABLE t (a TEXT, b INTEGER)"]

    assert lint.migration_fingerprint(plain) == lint.migration_fingerprint(reindented)
    assert lint.migration_fingerprint(plain) != lint.migration_fingerprint(altered)


def test_d07_records_every_shipped_step_and_no_others(clean_scan: lint.Scan) -> None:
    assert sorted(lint.SHIPPED_MIGRATIONS) == list(range(1, len(state_schema.MIGRATIONS) + 1))
    assert lint.check_migration_step_changed(clean_scan) == []


# ----- D-08 undeclared_state_table --------------------------------------------


def test_d08_fires_on_a_table_the_inventory_does_not_declare(scan: lint.Scan) -> None:
    """The staleness that actually happened, in the direction that catches it."""
    scan.state["gig_notes"] = _facts("gig_notes", ("gig_id",))

    found = lint.check_undeclared_state_table(scan)

    assert _rules(found) == [("undeclared_state_table", "gig_notes")]


def test_d08_goes_quiet_once_the_inventory_declares_it(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan.state["gig_notes"] = _facts("gig_notes", ("gig_id",))
    monkeypatch.setattr(
        state_schema,
        "ALL_KNOWN_TABLES",
        state_schema.ALL_KNOWN_TABLES | {"gig_notes"},
    )

    assert lint.check_undeclared_state_table(scan) == []


def test_d08_clean_against_the_real_tree(clean_scan: lint.Scan) -> None:
    assert lint.check_undeclared_state_table(clean_scan) == []
