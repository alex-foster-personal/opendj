"""Support modules and the scopes a global helper CARRIES.

Split out of test_ci_plan.py, which crossed the file size ceiling. A collected test module is
not the whole input: a helper beside it can be the only carrier of a cross-scope import, and
a helper imported by every suite drags its scopes into every run. Both are ways a plan can
look narrow while resting on an edge nobody derived.

`_config` and `_changed` are duplicated from test_ci_plan.py deliberately, for the reason
given in test_ci_plan_inputs.py.

[if] a helper's carried scopes are dropped from a plan [then] fail here, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_plan import read_config
from scripts.ci_plan_carry import helper_carried_scopes
from tests.scripts.ci_plan_helpers_fixtures import _changed, _config

__all__ = ["_changed", "_config"]

pytestmark = pytest.mark.requirement("OPS-16")


def test_the_committed_helper_carried_list_is_exactly_the_derived_one() -> None:
    """A global pytest helper is not a test and does not run; it is imported by suites across
    the tree, so a change to a scope it imports has to run everything. The anti-rot guard for
    that, recomputed from the tree rather than trusting the committed list: a new import in
    `tests/conftest.py` fails this test instead of silently leaving suites unrun.

    An EQUALITY, in both directions. The earlier form asked only whether every carried scope
    was covered, and a mutation that shrank the carried set passed it (round 5); a list that
    carries a scope no helper reaches is a constant, not a derivation, and is refused too.
    Named by scope rather than written as path triggers since round 9, because ownership is
    longest-prefix and a prefix trigger on `apps/webui/` would swallow `apps/webui/frontend/`.
    """
    config = read_config()
    carried = helper_carried_scopes(config, Path("."))
    assert carried, "no helper-carried scope found; the derivation measured nothing"
    assert tuple(sorted(carried)) == tuple(sorted(config.helper_carried)), (
        "ci/test-scopes.yml `helper_carried` no longer matches what the global helpers reach"
    )
