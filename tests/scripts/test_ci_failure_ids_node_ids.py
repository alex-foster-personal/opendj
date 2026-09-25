"""Node-id boundaries in ``scripts.ci_failure_ids``: where a pytest identity ENDS.

Split out of tests/scripts/test_ci_failure_ids.py, which is at the 600-line limit.

A node id holds whitespace only inside its `[parameters]`, so the identity ends at the
first whitespace at bracket depth zero. Ending it at the first whitespace instead, as this
did until Thu 24 Sep 2026, mis-parsed 378 of the ledger's 14,911 ids, and the fast tier's
copy of the same pattern DROPPED 375 of them outright: a leg failing only on those
reported no failing identity at all, which reads as an infra death rather than a red test.

Regression lines:
  - if a node id with whitespace in its parameters is cut at that whitespace then broken
  - if a ` - ` inside the parameters ends the identity then broken
  - if a parameter quoting a whole `FAILED tests/...` line yields a second identity then broken
  - if an id whose bracket never closes swallows the rest of the line then broken

[if] a ledger id round-trips through this parser unchanged [then] pass, [else stop].
"""

from __future__ import annotations

import pytest

from scripts.ci_failure_ids import failed_identities

pytestmark = pytest.mark.requirement("OPS-16")

TS = "2026-09-16T09:12:01.1234567Z "


def test_a_parametrized_id_holding_whitespace_survives_its_reason_text():
    """if a node id with a space in its parameters is cut at that space then the same test
    reads as two identities and the main-red subtraction stops matching it

    378 of the ledger's 14,911 ids hold whitespace. Ending the identity at the first space
    truncated every one, and where the line carried no ` - ` to anchor on, the fast tier's
    copy of this pattern dropped the identity ALTOGETHER, so a red leg reported no failing
    identity at all.
    """
    log = (
        f"{TS}FAILED tests/agentic/test_p.py::test_q[No errors appear anywhere.] - assert False\n"
        f"{TS}ERROR tests/agentic/test_p.py::test_r[a b c]\n"
    )
    assert failed_identities(log) == {
        "FAILED tests/agentic/test_p.py::test_q[No errors appear anywhere.]",
        "ERROR tests/agentic/test_p.py::test_r[a b c]",
    }


def test_a_dash_inside_the_parameters_is_not_the_reason_separator():
    """if ` - ` inside a parameter ends the identity then a real ledger id is cut in half"""
    identity = (
        "tests/mik/test_mikdb_read.py::test_strip_energy_prefix"
        "[1979 - Remaster-1979 - Remaster]"
    )
    log = f"{TS}FAILED {identity} - AssertionError: assert 'a' == 'b'\n"
    assert failed_identities(log) == {f"FAILED {identity}"}


def test_a_node_id_quoting_a_failure_line_is_one_identity_not_two():
    """if a parameter holding its own `FAILED tests/...` yields a second identity then a
    test named after a log line invents a failure that never ran"""
    identity = (
        "tests/scripts/test_ci_failure_ids.py::test_an_ordinary_test_failure_is_not_read"
        "_as_something_beyond_it[FAILED tests/test_a.py::test_b - AssertionError]"
    )
    assert failed_identities(f"{TS}FAILED {identity}\n") == {f"FAILED {identity}"}


def test_an_unbalanced_bracket_falls_back_to_the_whitespace_bounded_id():
    """if an id whose bracket never closes swallows the rest of the line then one malformed
    line poisons the identity set; the old whitespace bound is the safe fallback"""
    log = f"{TS}FAILED tests/a/test_x.py::test_one[oops - AssertionError: assert 0 == 1\n"
    assert failed_identities(log) == {"FAILED tests/a/test_x.py::test_one[oops"}
