"""Every check in scripts/sync_drift_lint.py must be able to FAIL.

The linter guards omissions, and an omission raises nothing. That makes a
green run indistinguishable from a linter pointed at the wrong thing unless
each check has been SHOWN to fire on the defect it names
(``.claude/rules/verification.md``). So every check here gets two tests: an
INJECTED defect that must be reported exactly, and a negative control on the
real tree that must report zero. A checker that flagged everything would pass
the first and fail the second; one that flagged nothing would do the reverse.

The allowlist gets the same treatment. Its entries are asserted to name
triples that ARE present in the live scan carrying a CURRENT_TIMESTAMP
default, so a stale entry (the debt got fixed, the entry did not get deleted)
fails as loudly as a new violation. Without that, D-05's zero could mean "the
subject disappeared" rather than "the subject is clean".

The run AS A WHOLE -- its floors, its wiring, and the gate that reads it --
is attacked in test_sync_drift_floors.py beside this file, and the SOURCE-TEXT
scan behind NAIVE_DEFAULT_SOURCES in test_sync_drift_sources.py.

Regression lines:
  - if a synced-looking table is not in SYNC_TABLES and D-01 stays silent then broken
  - if hub_changelog gains deleted_at and D-01 stays silent then broken
  - if D-01 reports the machines registry, which the protocol does handle, then broken
  - if SYNC_TABLES names a table no authority builds and D-02 stays silent then broken
  - if a registered table loses deleted_at and D-02 stays silent then broken
  - if D-02 demands LWW columns of the machines registry then broken
  - if a MIGRATIONS step is appended without a SCHEMA_VERSION bump and D-03
    stays silent then broken
  - if a live column has no column_docs entry and D-04 stays silent then broken
  - if any ladder declares DEFAULT CURRENT_TIMESTAMP off the allowlist and D-05
    stays silent then broken
  - if a quoted literal merely containing CURRENT_TIMESTAMP makes D-05 fire then broken
  - if a DEFAULT wrapping CURRENT_TIMESTAMP in a UTC strftime makes D-05 fire
    then broken: that is the remedy the rule asks for
  - if a DEFAULT that reformats the stamp WITHOUT a zone designator stops D-05
    firing then broken
  - if a DEFAULT naming CURRENT_TIMESTAMP that sqlite cannot evaluate returns a
    verdict instead of raising then broken
  - if the allowlist names a triple that no longer exists then the debt list has
    gone stale, so broken
  - if any check reports a violation against the real tree then broken
"""

from __future__ import annotations

import copy
import dataclasses
import sqlite3

import pytest

from apps.database import column_docs as docs
from apps.database import generate_agents_md
from apps.shared.state import schema as state_schema
from apps.sync_hub import protocol_common
from scripts import sync_drift_lint as lint
from scripts import sync_drift_subject as subject

TRIO: tuple[str, ...] = protocol_common.SYNC_COLUMNS
PAIR: tuple[str, ...] = (protocol_common.UPDATED_AT, protocol_common.ORIGIN_DEVICE_ID)


# ----- fixtures ---------------------------------------------------------------


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


def _with_columns(table: subject.TableFacts, *columns: str) -> subject.TableFacts:
    return dataclasses.replace(
        table,
        columns=(*table.columns, *columns),
        defaults={**table.defaults, **dict.fromkeys(columns)},
    )


# ----- D-01 unregistered_synced_table -----------------------------------------


def test_d01_fires_on_a_synced_table_nobody_registered(scan: lint.Scan) -> None:
    """The schema -> registry direction: the whole reason this file exists."""
    scan.state["gig_notes"] = _facts("gig_notes", ("gig_id", "body", *TRIO))

    found = lint.check_unregistered_synced_table(scan)

    assert _rules(found) == [("unregistered_synced_table", "gig_notes")]


def test_d01_goes_quiet_once_the_table_is_registered(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the check reads the registry rather than a list baked into it."""
    scan.state["gig_notes"] = _facts("gig_notes", ("gig_id", "body", *TRIO))
    monkeypatch.setattr(
        protocol_common,
        "SYNC_TABLES",
        (*protocol_common.SYNC_TABLES, protocol_common.TableSpec("gig_notes", ("gig_id",))),
    )

    assert lint.check_unregistered_synced_table(scan) == []


def test_d01_changelog_exemption_lapses_when_its_reason_does(scan: lint.Scan) -> None:
    """The exemption is conditional on the absence of deleted_at, not on the name.

    A name-only exemption would silently absorb exactly this change, which is
    the failure mode the linter exists to catch, one level up.
    """
    before = scan.state["hub_changelog"]
    assert protocol_common.DELETED_AT not in before.columns, (
        "the exemption's stated reason is that a log entry cannot be "
        "tombstoned; if hub_changelog has deleted_at the reason is wrong"
    )
    scan.state["hub_changelog"] = _with_columns(before, protocol_common.DELETED_AT)

    found = lint.check_unregistered_synced_table(scan)

    assert _rules(found) == [("unregistered_synced_table", "hub_changelog")]


def test_d01_does_not_report_the_machine_registry_the_protocol_handles(
    scan: lint.Scan,
) -> None:
    """``machines`` is in the sync set, just without LWW columns.

    ``pk_columns`` and ``table_columns`` accept it and raise for anything
    else, so reporting it would be a false positive on a table that IS
    handled. Today it carries no sync columns, so a check that had simply
    forgotten it would read clean by luck; the columns are added here so the
    exemption is the only thing that can keep it quiet.
    """
    assert protocol_common.REGISTRY_TABLE in lint.handled_tables()
    registry = scan.state[protocol_common.REGISTRY_TABLE]
    assert not registry.has(*PAIR), "if machines gains LWW columns, revisit this exemption"
    scan.state[protocol_common.REGISTRY_TABLE] = _with_columns(registry, *PAIR)

    assert lint.check_unregistered_synced_table(scan) == []


def test_d01_still_fires_on_an_unhandled_table_with_the_same_columns(
    scan: lint.Scan,
) -> None:
    """The control for the exemption above: same injection, unhandled name."""
    other = scan.state["adapters"]
    assert other.name not in lint.handled_tables()
    scan.state["adapters"] = _with_columns(other, *PAIR)

    assert _rules(lint.check_unregistered_synced_table(scan)) == [
        ("unregistered_synced_table", "adapters")
    ]


def test_d01_clean_against_the_real_tree(clean_scan: lint.Scan) -> None:
    assert lint.check_unregistered_synced_table(clean_scan) == []


# ----- D-02 registered_table_missing ------------------------------------------


def test_d02_fires_on_a_registered_table_no_authority_builds(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        protocol_common,
        "SYNC_TABLES",
        (*protocol_common.SYNC_TABLES, protocol_common.TableSpec("ghost_table", ("id",))),
    )

    found = lint.check_registered_table_missing(scan)

    assert _rules(found) == [("registered_table_missing", "ghost_table")]
    assert "no schema authority creates it" in found[0].detail


def test_d02_fires_when_a_registered_table_loses_a_sync_column(scan: lint.Scan) -> None:
    before = scan.state["tracks"]
    kept = tuple(c for c in before.columns if c != protocol_common.DELETED_AT)
    scan.state["tracks"] = dataclasses.replace(
        before,
        columns=kept,
        defaults={c: before.defaults[c] for c in kept},
    )

    found = lint.check_registered_table_missing(scan)

    assert _rules(found) == [("registered_table_missing", "tracks")]
    assert protocol_common.DELETED_AT in found[0].detail


def test_d02_does_not_demand_lww_columns_of_the_machine_registry(
    clean_scan: lint.Scan,
) -> None:
    """The other half of the split: handled, but not row-level LWW.

    Folding REGISTRY_TABLE into the LWW set to quiet D-01 would make D-02
    demand a trio the registry does not use, which is a false report in the
    opposite direction.
    """
    assert protocol_common.REGISTRY_TABLE not in lint.lww_tables()
    registry = clean_scan.state[protocol_common.REGISTRY_TABLE]
    assert not registry.has(*TRIO), "the premise of this test is that it has no trio"

    assert lint.check_registered_table_missing(clean_scan) == []


def test_d02_clean_against_the_real_tree(clean_scan: lint.Scan) -> None:
    assert lint.check_registered_table_missing(clean_scan) == []


# ----- D-03 version_ladder_mismatch -------------------------------------------


def test_d03_fires_on_a_step_appended_without_a_bump(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forgotten bump: the appended step never runs and nothing complains."""
    monkeypatch.setattr(
        state_schema,
        "MIGRATIONS",
        [*state_schema.MIGRATIONS, ["CREATE TABLE IF NOT EXISTS never_created (x TEXT)"]],
    )

    found = lint.check_version_ladder_mismatch(scan)

    assert _rules(found) == [("version_ladder_mismatch", "apps/shared/state/schema.py")]
    assert "the last step never runs" in found[0].detail


def test_d03_fires_on_a_bump_without_a_step(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_schema, "SCHEMA_VERSION", state_schema.SCHEMA_VERSION + 1)

    found = lint.check_version_ladder_mismatch(scan)

    assert _rules(found) == [("version_ladder_mismatch", "apps/shared/state/schema.py")]
    assert "a step is missing" in found[0].detail


def test_d03_clean_against_the_real_tree(clean_scan: lint.Scan) -> None:
    assert lint.check_version_ladder_mismatch(clean_scan) == []


# ----- D-04 undocumented_table_or_column --------------------------------------


def test_d04_fires_on_an_undocumented_table(scan: lint.Scan) -> None:
    scan.documented.append(
        generate_agents_md.TableInfo(
            name="gig_notes",
            columns=(
                generate_agents_md.ColumnInfo(
                    name="gig_id", type="TEXT", not_null=True, default=None, primary_key=True
                ),
            ),
            foreign_keys=(),
        )
    )

    found = lint.check_undocumented_table_or_column(scan)

    assert _rules(found) == [("undocumented_table_or_column", "gig_notes")]


def test_d04_fires_on_an_undocumented_column(
    scan: lint.Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    thinned = {
        table: ({k: v for k, v in columns.items() if k != "isrc"} if table == "tracks" else columns)
        for table, columns in docs.COLUMN_DOCS.items()
    }
    monkeypatch.setattr(docs, "COLUMN_DOCS", thinned)

    found = lint.check_undocumented_table_or_column(scan)

    assert _rules(found) == [("undocumented_table_or_column", "tracks.isrc")]


def test_d04_covers_the_tables_the_sibling_authorities_add(clean_scan: lint.Scan) -> None:
    """The subject must reach past the migration ladder.

    Measured Tue 1 Sep 2026: building only the ladder gave D-04 18 tables of
    the 26 a provisioned state.db documents, and one of the eight it could not
    see (spotify_playlist_links) had no docs entry at all.
    """
    documented = {table.name for table in clean_scan.documented}
    assert {"pairings", "smartlists", "play_orders", "spotify_playlist_links"} <= documented

    assert lint.check_undocumented_table_or_column(clean_scan) == []


# ----- D-05 naive_stamp_default -----------------------------------------------


def test_d05_fires_on_a_ladder_that_mints_a_naive_stamp(scan: lint.Scan) -> None:
    """Today's P0 class, one level earlier: the DDL default that writes it."""
    scan.ladders["fake_ladder"] = {
        "gig_notes": _facts(
            "gig_notes", ("gig_id", "created_at"), created_at="CURRENT_TIMESTAMP"
        )
    }

    found = lint.check_naive_stamp_default(scan)

    assert _rules(found) == [("naive_stamp_default", "fake_ladder:gig_notes.created_at")]


def test_d05_allowlist_covers_one_triple_not_a_whole_table(scan: lint.Scan) -> None:
    """A second naive default on an allowlisted TABLE still fires.

    The allowlist is keyed on (ladder, table, column). Keyed on the table it
    would quietly absorb every future naive default added beside a listed one.
    """
    before = scan.ladders["engine_core_durable"]["duplicate_clusters"]
    assert ("engine_core_durable", "duplicate_clusters", "created_at") in (
        lint.NAIVE_DEFAULT_ALLOWLIST
    )
    scan.ladders["engine_core_durable"]["duplicate_clusters"] = dataclasses.replace(
        before,
        columns=(*before.columns, "reviewed_at"),
        defaults={**before.defaults, "reviewed_at": "CURRENT_TIMESTAMP"},
    )

    found = lint.check_naive_stamp_default(scan)

    assert _rules(found) == [
        ("naive_stamp_default", "engine_core_durable:duplicate_clusters.reviewed_at")
    ]


@pytest.mark.parametrize(
    "default",
    [
        "'pending'",
        "'see CURRENT_TIMESTAMP notes'",
        "\"CURRENT_TIMESTAMP\"",
        "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        # The correct fix for a naive default, WRAPPING the token rather than
        # avoiding it: a sortable ISO-8601 UTC stamp. Token matching reported
        # this, so the check rejected the remedy for the thing it was
        # complaining about, with no escape hatch short of editing the linter.
        "(strftime('%Y-%m-%dT%H:%M:%SZ', CURRENT_TIMESTAMP))",
        "NULL",
        "0",
    ],
)
def test_d05_ignores_a_default_that_does_not_evaluate_the_token(
    scan: lint.Scan, default: str
) -> None:
    """A hard-zero gate whose only escape hatch is editing the linter gets
    switched off, so the match has to be the CALL and not the substring."""
    scan.ladders["fake_ladder"] = {
        "gig_notes": _facts("gig_notes", ("gig_id", "state"), state=default)
    }

    assert lint.check_naive_stamp_default(scan) == []


@pytest.mark.parametrize(
    "default",
    [
        "CURRENT_TIMESTAMP",
        "current_timestamp",
        "(CURRENT_TIMESTAMP)",
        # Reformatted, but still not placeable on the UTC line: no zone
        # designator. The narrowing is on the VALUE minted, not on whether
        # strftime appears, so a half-done fix is still reported.
        "(strftime('%Y-%m-%d %H:%M:%S', CURRENT_TIMESTAMP))",
        "(strftime('%Y-%m-%dT%H:%M:%S', CURRENT_TIMESTAMP))",
    ],
)
def test_d05_still_fires_on_the_real_call(scan: lint.Scan, default: str) -> None:
    """The control for the narrowing above: the defect itself still reports."""
    scan.ladders["fake_ladder"] = {
        "gig_notes": _facts("gig_notes", ("gig_id", "made_at"), made_at=default)
    }

    assert _rules(lint.check_naive_stamp_default(scan)) == [
        ("naive_stamp_default", "fake_ladder:gig_notes.made_at")
    ]


def test_d05_refuses_a_verdict_on_a_default_it_cannot_evaluate(scan: lint.Scan) -> None:
    """A failed measurement must not render as a result, in either colour.

    D-05 now decides by EVALUATING the default and inspecting the value. An
    expression naming CURRENT_TIMESTAMP that sqlite will not evaluate is
    exactly what this rule is about and exactly what it did not measure, so
    it raises rather than returning clean (or guessing dirty).
    """
    scan.ladders["fake_ladder"] = {
        "gig_notes": _facts(
            "gig_notes", ("gig_id", "made_at"), made_at="no_such_fn(CURRENT_TIMESTAMP)"
        )
    }

    with pytest.raises(RuntimeError, match="could not evaluate"):
        lint.check_naive_stamp_default(scan)


def test_d05_clean_against_the_real_tree(clean_scan: lint.Scan) -> None:
    assert lint.check_naive_stamp_default(clean_scan) == []


@pytest.mark.parametrize("triple", sorted(lint.NAIVE_DEFAULT_ALLOWLIST))
def test_d05_allowlist_entries_still_name_a_real_defect(
    clean_scan: lint.Scan, triple: tuple[str, str, str]
) -> None:
    """The debt list may only shrink, and it may not lie.

    Without this, D-05's zero is ambiguous: it reads the same whether the
    allowlisted subject is still there and still defective, or has been
    deleted and the entry left behind. This asserts the subject EXISTS and
    still carries the default, so the entry is suppressing a real finding
    rather than nothing at all.
    """
    ladder, table, column = triple
    assert ladder in clean_scan.ladders, f"allowlist names ladder {ladder!r}, which was not built"
    assert table in clean_scan.ladders[ladder], (
        f"allowlist names {ladder}:{table}, which the ladder no longer builds. "
        "Delete the entry."
    )
    default = clean_scan.ladders[ladder][table].defaults.get(column)
    probe = sqlite3.connect(":memory:")
    assert default is not None and lint.mints_naive_stamp(probe, default), (
        f"allowlist names {ladder}:{table}.{column}, which no longer defaults "
        "to CURRENT_TIMESTAMP. The debt is paid; delete the entry."
    )


def test_d05_allowlist_names_the_live_originals_not_only_the_dormant_copies() -> None:
    """A debt list covering only the dormant twins would read as complete.

    The four engine_core entries are copies; the executing originals live in
    apps/dedup/schema.py and apps.shared.fingerprints, and both are scanned.
    """
    ladders = {ladder for ladder, _, _ in lint.NAIVE_DEFAULT_ALLOWLIST}

    assert {"dedup", "fingerprints"} <= ladders <= set(subject.OTHER_LADDERS)
