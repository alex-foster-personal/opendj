"""D-09: refuse to report a clean scan that did not really measure anything.

Split out of :mod:`scripts.sync_drift_lint` so the two questions stay apart,
the way the subject and the policy already are. The checks answer WHAT IS
WRONG; this module answers WAS THERE ANYTHING TO BE WRONG ABOUT. That second
question is the one a green run cannot answer for itself: zero violations is
what a clean tree returns, and it is equally what an unbuilt subject, an
unwired check and a scan pointed at the wrong database return
(.claude/rules/verification.md).

Every floor here derives from a DECLARATION rather than a remembered number,
and each names the SET it expected rather than a count. A count with slack
passes while a declared table has gone missing, which is the shape of error
the whole tool is about.
"""

from __future__ import annotations

from types import ModuleType

from apps.database import generate_agents_md
from apps.shared.state import schema as state_schema
from scripts.sync_drift_rules import CFG
from scripts.sync_drift_subject import OTHER_LADDERS, STATE_LADDER, Scan

__all__ = ["assert_checks_wired", "assert_measurable"]


# ----- D-09: the floors ------------------------------------------------------


def assert_checks_wired(module: ModuleType) -> None:
    """Every check ``module`` defines must be dispatched, and match a rule.

    Reads ``module.CHECKS`` and ``module.RULES`` rather than importing them,
    so it compares that module's OWN wiring -- which is also what lets a test
    unwire a check and watch this fire.

    This is the arm that makes ``checks_run`` mean something. Counting
    iterations of the dispatch loop cannot fail -- it prints "7 of 7" after a
    check is unwired, which reads as complete. Comparing the DEFINED checks
    against the DISPATCHED ones can fail, and deleting a pair from CHECKS and
    RULES together (which their parity test cannot see, because both shrink)
    leaves the function behind and trips this.
    """
    defined = {
        name
        for name, value in vars(module).items()
        if name.startswith("check_")
        and callable(value)
        and getattr(value, "__module__", None) == module.__name__
    }
    dispatched = {check.__name__ for _, check in module.CHECKS}
    unwired = sorted(defined - dispatched)
    if unwired:
        raise SystemExit(
            f"[sync-drift] ABORT: {unwired} defined but not in CHECKS, so it "
            "never ran. An unwired check reports zero violations, which is "
            "indistinguishable from a clean tree."
        )
    if sorted(rule for rule, _ in module.CHECKS) != sorted(module.RULES):
        raise SystemExit(
            "[sync-drift] ABORT: CHECKS and RULES disagree "
            f"({sorted(rule for rule, _ in module.CHECKS)} vs {sorted(module.RULES)}). A rule "
            "with no check reports a permanent, meaningless zero."
        )


def assert_measurable(scan: Scan) -> None:
    """Refuse a scan too small, or too narrow, to have measured anything.

    Every floor derives from a declaration rather than a remembered number,
    and each names the SET it expected rather than a count: a count with slack
    passes while a declared table has gone missing, which is the shape of
    error this whole file is about.
    """
    declared = set(state_schema.ALL_KNOWN_TABLES)
    if len(declared) < CFG.MIN_DECLARED_TABLES:
        raise SystemExit(
            f"[sync-drift] ABORT: apps.shared.state.schema declares only "
            f"{len(declared)} table(s) in ALL_KNOWN_TABLES. The floor derives "
            "from that declaration, so an emptied declaration would set a "
            "floor of zero and a scan over nothing would read clean."
        )
    absent = sorted(declared - set(scan.state))
    if absent:
        raise SystemExit(
            f"[sync-drift] ABORT: the scan subject is missing {absent}, which "
            "apps.shared.state.schema.ALL_KNOWN_TABLES declares a provisioned "
            "state.db holds. This is a broken scan, not a clean tree."
        )
    # D-04's subject is the state DB seen through the AGENTS.md generator,
    # which structurally excludes fts5 shadow tables (sqlite's own opaque
    # index storage). Those are the ONLY tables it may drop, so this stays a
    # set comparison rather than a count with slack -- but the set is derived
    # by the GENERATOR'S OWN detector over the virtual tables this scan
    # measured, not by a name prefix. A `tracks_fts_` prefix pinned the one
    # fts5 table that existed the day it was written: adding a second
    # full-text index (an ordinary change here) put its five shadows in
    # `unseen` and aborted all eight checks, accusing the scan of being
    # broken when the scan was right and the floor was stale.
    shadows = {
        name
        for name in scan.state
        if generate_agents_md._is_fts5_shadow_table(name, set(scan.virtual_tables))
    }
    unseen = sorted(set(scan.state) - shadows - {t.name for t in scan.documented})
    if unseen:
        raise SystemExit(
            f"[sync-drift] ABORT: the docs introspection did not see {unseen}, "
            "so D-04 measured less than the scan built. A docs check over a "
            "subject it cannot see reports zero."
        )
    expected_ladders = set(OTHER_LADDERS) | {STATE_LADDER}
    if set(scan.ladders) != expected_ladders:
        raise SystemExit(
            f"[sync-drift] ABORT: measured ladders {sorted(scan.ladders)}, "
            f"expected {sorted(expected_ladders)}. A ladder that was not built "
            "cannot have been found clean."
        )
    empty = sorted(name for name, tables in scan.ladders.items() if not tables)
    if empty:
        raise SystemExit(
            f"[sync-drift] ABORT: ladder(s) {empty} built zero tables. D-05 "
            "would report zero naive defaults over nothing at all, and its "
            "allowlist entries would suppress findings that were never made."
        )
