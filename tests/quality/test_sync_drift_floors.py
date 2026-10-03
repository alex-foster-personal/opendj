"""The whole drift run must be unable to report clean while measuring nothing.

Companion to test_sync_drift_lint.py, which proves each individual check can
FAIL. These tests attack the three ways a run can be green for the wrong
reason (``.claude/rules/verification.md``):

  - THE SUBJECT. A scan that skipped an authority, a ladder, or the docs
    introspection must abort rather than return. Each floor is asserted to
    name the SET it expected rather than a count with slack, because a count
    with slack passes while a declared table has gone missing.
  - THE WIRING. A check deleted from CHECKS leaves its function behind, which
    is what ``assert_checks_wired`` looks for. The count of completed checks
    cannot see this: counting loop iterations always agrees with itself.
  - THE GATE. Deleting the function too satisfies the wiring check, so
    quality_gate's HARD_ZERO is the second, independent declaration: a
    hard-zero key that is never emitted is never compared.

Regression lines:
  - if a declared table is missing from the subject and the run reports PASS
    then broken
  - if a ladder is missing, or built zero tables, and the run reports PASS
    then broken
  - if the docs introspection sees nothing, or misses one live table, and the
    run reports PASS then broken
  - if a check is unwired from CHECKS and the run reports the remaining checks
    as complete then broken
  - if a hard-zero drift metric stops being emitted and the gate still passes
    then broken
  - if a schema authority fails and build_state_db returns a partial subject
    then broken
"""

from __future__ import annotations

import copy
import dataclasses
import re
from pathlib import Path

import pytest

from apps.database import generate_agents_md
from apps.shared.state import schema as state_schema
from scripts import quality_gate
from scripts import sync_drift_lint as lint
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


# ----- D-09 the floors, the wiring, and the run as a whole ---------------------


def test_d09_aborts_when_a_declared_table_is_missing_from_the_subject(
    scan: lint.Scan,
) -> None:
    """The floor names the SET, not a count.

    A count with slack passes while a declared table has gone missing, which
    is the shape of error this whole file is about: dropping one table of
    eighteen leaves seventeen, and seventeen clears a floor of seventeen.
    """
    del scan.state["events"]

    with pytest.raises(SystemExit, match="broken scan, not a clean tree"):
        lint.run(scan)


def test_d09_aborts_when_a_ladder_was_not_built(scan: lint.Scan) -> None:
    del scan.ladders["engine_core_cache"]

    with pytest.raises(SystemExit, match="cannot have been found clean"):
        lint.run(scan)


def test_d09_aborts_when_a_ladder_built_nothing(scan: lint.Scan) -> None:
    """An empty ladder makes D-05 report zero over nothing, and makes its
    allowlist entries suppress findings that were never made."""
    scan.ladders["dedup"] = {}

    with pytest.raises(SystemExit, match="built zero tables"):
        lint.run(scan)


def test_d09_aborts_when_the_docs_introspection_saw_nothing(scan: lint.Scan) -> None:
    """D-04's entire subject, floored. Without this it can measure nothing and
    the run still prints the same control line."""
    scan.documented.clear()

    with pytest.raises(SystemExit, match="docs introspection did not see"):
        lint.run(scan)


def test_d09_aborts_when_the_docs_introspection_misses_one_table(
    scan: lint.Scan,
) -> None:
    """The floor is a SET comparison, so one missing table is enough.

    A floor derived from the ladder's own TABLES tuple would pass here: every
    ladder table is still documented, and the table that vanished belongs to a
    sibling authority. That is the shape of gap this whole file is about.
    """
    scan.documented[:] = [t for t in scan.documented if t.name != "spotify_playlist_links"]

    with pytest.raises(SystemExit, match=re.escape("['spotify_playlist_links']")):
        lint.run(scan)


def test_d09_tolerates_exactly_the_fts5_shadows_and_nothing_else(
    clean_scan: lint.Scan,
) -> None:
    """The control for the floor above: it must not fire on a clean tree.

    The generator drops fts5 shadow tables by construction, so the floor
    subtracts exactly those names. Asserting the difference IS the shadow set
    keeps the exception from quietly widening.
    """
    shadows = {
        name
        for name in clean_scan.state
        if generate_agents_md._is_fts5_shadow_table(name, set(clean_scan.virtual_tables))
    }
    missed = set(clean_scan.state) - {t.name for t in clean_scan.documented}

    assert missed == shadows
    assert shadows, "the shadow set is derived, so an empty one means the derivation broke"


def test_d09_and_d08_survive_a_second_full_text_index(scan: lint.Scan) -> None:
    """Adding another fts5 table must not abort all eight checks.

    The floor used to derive its shadow set from a ``tracks_fts_`` name
    prefix, pinning the one virtual table that existed the day it was
    written. The generator excludes shadows STRUCTURALLY and says in its own
    docstring that a future fts5 table needs no code change, so the floor
    disagreed with the thing it was checking: a second full-text index -- an
    ordinary change on this repo -- put five shadow tables in `unseen` and
    aborted the run with a message accusing the SCAN of being broken, when
    the scan was right and the floor was stale.

    Asserted on the PRESENCE of the good thing rather than the absence of the
    abort: the whole run is driven and ``checks_run`` is read back, because
    ``assert_measurable`` returning quietly is also what a floor that stopped
    measuring returns. The new index is then reported BY NAME as undeclared,
    which is the actionable half -- declare it and its shadows in
    ALL_KNOWN_TABLES, exactly as tracks_fts already is -- where the stale
    floor SystemExited before check one and blamed the scan.

    NAMED FOR BOTH RULES because it can fail for both.
    scripts/sync_drift_mutation_control.py blinds one check at a time and
    asserts the failures land in that check's own tests, keyed on the rule
    token in the test NAME; under the d09-only name it had, blinding D-08
    made this go red and the harness aborted with "measuring the wrong
    subject" instead of proving the eight mutations. Caught Wed 9 Sep 2026 by
    running the harness, which no pytest run does.
    """
    lyrics = {
        "lyrics_fts": _facts("lyrics_fts", ("line",)),
        **{
            f"lyrics_fts{suffix}": _facts(f"lyrics_fts{suffix}", ("id", "block"))
            for suffix in ("_config", "_content", "_data", "_docsize", "_idx")
        },
    }
    scan.state.update(lyrics)
    scan.documented.append(
        generate_agents_md.TableInfo(name="lyrics_fts", columns=(), foreign_keys=())
    )
    widened = dataclasses.replace(
        scan, virtual_tables=scan.virtual_tables | {"lyrics_fts"}
    )

    result = lint.run(widened)

    assert result.checks_run == len(lint.CHECKS) == 9, (
        "all nine checks must have RUN. A floor that aborts raises SystemExit "
        "before the first one, and no Result is produced at all."
    )
    undeclared = {v.subject for v in result.violations if v.rule == "undeclared_state_table"}
    assert "lyrics_fts" in undeclared, (
        "the run reached D-08 and named the new index, which is the report a "
        "developer can act on."
    )


def test_d09_still_aborts_on_a_shadow_shaped_name_with_no_virtual_table(
    scan: lint.Scan,
) -> None:
    """The control: the exclusion is the VIRTUAL TABLE, not the suffix.

    A table merely named like a shadow, with no fts5 parent in the database,
    is an ordinary undocumented table and must still abort. Without this, the
    widening above could have been done by matching suffixes and would have
    let any ``*_data`` table through unmeasured.
    """
    scan.state["lyrics_fts_data"] = _facts("lyrics_fts_data", ("id", "block"))

    with pytest.raises(SystemExit, match=re.escape("['lyrics_fts_data']")):
        lint.assert_measurable(scan)


def test_d09_floor_tracks_the_declaration_not_a_pinned_number() -> None:
    """The floor is derived, so adding a table moves it instead of rotting."""
    assert len(state_schema.ALL_KNOWN_TABLES) >= lint.CFG.MIN_DECLARED_TABLES


def test_d09_aborts_when_a_check_is_defined_but_never_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arm that makes checks_run mean anything.

    Counting completed checks cannot fail: it reports "7 of 7" after a check
    is unwired. Comparing the DEFINED checks against the DISPATCHED ones can,
    and this is the mutation that a CHECKS/RULES parity test cannot see,
    because both shrink together.
    """
    monkeypatch.setattr(lint, "CHECKS", lint.CHECKS[:-1])
    monkeypatch.setattr(
        lint, "RULES", {k: v for k, v in lint.RULES.items() if k != lint.CHECKS[-1][0]}
    )

    with pytest.raises(SystemExit, match="defined but not in CHECKS"):
        lint.assert_checks_wired()


def test_d09_aborts_when_a_rule_has_no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lint, "RULES", {**lint.RULES, "orphan_rule": "no check emits this"})

    with pytest.raises(SystemExit, match="CHECKS and RULES disagree"):
        lint.assert_checks_wired()


def test_the_gate_refuses_a_drift_rule_that_stopped_being_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second floor, in the other file, for the mutation the first cannot see.

    Deleting the check function AND its CHECKS/RULES entries satisfies
    assert_checks_wired, and a HARD_ZERO key that is never emitted is never
    compared -- so the gate would pass by having stopped asking.
    """
    victim = "naive_stamp_default"
    monkeypatch.delattr(lint, f"check_{victim}")
    monkeypatch.setattr(lint, "CHECKS", tuple(c for c in lint.CHECKS if c[0] != victim))
    monkeypatch.setattr(lint, "RULES", {k: v for k, v in lint.RULES.items() if k != victim})
    assert f"sync_drift.{victim}" in quality_gate.HARD_ZERO

    with pytest.raises(RuntimeError, match="emitted no metric"):
        quality_gate._eval_sync_drift()


def test_every_declared_rule_has_a_check_and_every_check_a_rule() -> None:
    """A rule with no check reports a permanent, meaningless zero."""
    assert sorted(rule for rule, _ in lint.CHECKS) == sorted(lint.RULES)


def test_every_hard_zero_drift_key_names_a_declared_rule() -> None:
    """The gate's list and the linter's list are the two declarations that
    keep each other honest, so neither may name a rule the other lacks."""
    gated = {k.removeprefix("sync_drift.") for k in quality_gate.HARD_ZERO
             if k.startswith("sync_drift.")}

    assert gated == set(lint.RULES)


def test_the_real_tree_is_clean_and_the_control_says_it_was_measured(
    clean_scan: lint.Scan,
) -> None:
    result = lint.run(clean_scan)

    assert result.violations == ()
    assert result.checks_run == len(lint.CHECKS)
    assert result.ladders_scanned == len(subject.OTHER_LADDERS) + 1
    assert set(state_schema.ALL_KNOWN_TABLES) <= set(clean_scan.state)
    assert result.counts() == dict.fromkeys(lint.RULES, 0)


def test_main_exits_zero_against_the_real_tree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert lint.main([]) == 0
    assert "[sync-drift] PASS" in capsys.readouterr().out


def test_list_facts_names_every_ladder(capsys: pytest.CaptureFixture[str]) -> None:
    assert lint.main(["--list-facts"]) == 0
    out = capsys.readouterr().out
    for ladder in (subject.STATE_LADDER, *subject.OTHER_LADDERS):
        assert ladder in out


# ----- the subject ------------------------------------------------------------


def test_scan_builds_the_ladders_it_declares(tmp_path: Path) -> None:
    """A ladder builder that silently no-ops would leave D-05 measuring nothing."""
    for name in subject.OTHER_LADDERS:
        conn = subject.build_other_ladder(name, tmp_path)
        try:
            tables = subject.introspect_tables(conn)
        finally:
            conn.close()
        assert tables, f"ladder {name!r} built zero tables"


def test_unknown_ladder_raises_rather_than_returning_empty(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no builder for ladder"):
        subject.build_other_ladder("not_a_ladder", tmp_path)


def test_every_state_authority_contributes_to_the_subject(clean_scan: lint.Scan) -> None:
    """Naming an authority is not the same as running it.

    Each entry is asserted to have put at least one table in the subject, so
    an authority that silently no-ops (a renamed entry point, a guard that
    returns early) cannot sit in the list looking measured.
    """
    per_authority = {
        "apps/shared/state/schema.py": "tracks",
        "apps/analysis/store.py": "analysis",
        # The backfill queue, additive on the same state.db, and its
        # staleness table. Two FILES rather than two entry points: the
        # declaration these are compared against reads files, so a
        # DDL-bearing file folded into its neighbour's line would be
        # classified by nobody.
        "apps/analysis/queue_store.py": "analysis_queue_item",
        "apps/analysis/queue_stale.py": "analysis_stale",
        "apps/shared/pairings/schema_sql.py": "pairings",
        # The same module's SECOND entry point, listed separately because it
        # is a separate ladder with its own version counter. Naming the module
        # once was how three tables stayed out of every inventory until
        # Wed 9 Sep 2026.
        "apps/shared/pairings/schema_sql.py::apply_pairing_capture_migrations": (
            "pairing_sync_snapshots"
        ),
        "apps/shared/play_orders/schema.py": "play_orders",
        # SET-05's private ladder, the same shape as play_orders just above.
        # Missing here until this fix even though it was already declared in
        # DDL_SOURCE_FILES and STATE_AUTHORITIES: the two production
        # declarations agreed with each other, so nothing caught this test's
        # own per_authority dict going stale the day playlist_sets was added.
        "apps/shared/playlist_sets/schema.py": "playlist_sets",
        "apps/spotify/state_aux.py": "spotify_playlist_links",
        "apps/launcher/scripts/bootstrap_db.py": "tracks_frecency",
        # The one authority that is NOT PYTHON. Every derivation of this list
        # had been done by reading *.py, so launcher_meta was created in the
        # live state.db on every launcher start while no inventory, no test
        # and no docs run had heard of it.
        "apps/launcher/src-tauri/src/state.rs": "launcher_meta",
        "apps/sync_hub/engine_identity_map.py": "sync_identity_remap",
        "apps/webui/server/pairings_sqlite.py": "http_pairings",
    }
    assert {a.name for a in subject.STATE_AUTHORITIES} == set(per_authority)
    for authority, table in per_authority.items():
        assert table in clean_scan.state, f"{authority} contributed nothing to the subject"


def test_a_failing_authority_aborts_rather_than_shrinking_the_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partially built subject reads as a clean tree, so it must raise."""

    def _boom(_conn: object, _path: Path) -> None:
        raise RuntimeError("simulated authority failure")

    broken = subject.STATE_AUTHORITIES[0].__class__("apps/broken.py", _boom)
    monkeypatch.setattr(
        "scripts.sync_drift_subject.STATE_AUTHORITIES",
        (*subject.STATE_AUTHORITIES, broken),
    )

    with pytest.raises(RuntimeError, match=re.escape("apps/broken.py failed to provision")):
        subject.build_state_db(tmp_path / "state" / "state.db")


# ----- the non-Python authority ----------------------------------------------


def test_the_rust_authority_contributes_its_real_ddl(clean_scan: lint.Scan) -> None:
    """Read from source, executed by sqlite, then read back out of the DB.

    The launcher is an authority this scan cannot import. Its DDL is
    extracted from the Rust literal and EXECUTED, so sqlite parses it and
    this tool does not, and the columns asserted here come from
    ``PRAGMA table_info`` on the result rather than from the extraction.
    """
    facts = clean_scan.state["launcher_meta"]

    assert facts.columns == ("key", "value")
    assert facts.defaults == {"key": None, "value": None}


def test_the_rust_extraction_aborts_rather_than_contributing_nothing(
    tmp_path: Path,
) -> None:
    """Zero statements is what a reformatted source looks like.

    An authority that silently contributes no tables leaves its tables
    unmeasured while every check still reports a clean scan, which is the
    exact failure this file exists to prevent. So finding nothing raises.
    """
    empty = tmp_path / "state.rs"
    empty.write_text("fn nothing() {}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="found no CREATE TABLE literal"):
        subject.rust_ddl_statements(empty)


def test_the_rust_extraction_finds_what_the_source_declares() -> None:
    """The control for the floor above: it must find something real.

    A guard that only proves an EMPTY source raises would pass just as well
    if the extractor were broken for every source.
    """
    statements = subject.rust_ddl_statements(subject.RUST_STATE_AUTHORITY)

    assert len(statements) == 1
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS launcher_meta")
    assert "\\n" not in statements[0], "the Rust escape sequences were not decoded"
