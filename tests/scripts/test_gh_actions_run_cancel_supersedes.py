"""In-repo automation must route cancels through ``gh_actions_run_cancel`` (X25).

Regression lines:
  - if ci_fast_cancel posts a bare gh cancel instead of the helper then broken
  - if ci_trunk_tip_only posts a bare gh cancel instead of the helper then broken
"""

from __future__ import annotations

import inspect

from scripts import ci_fast_cancel, ci_trunk_tip_only


def test_ci_fast_cancel_imports_the_shared_cancel_helper() -> None:
    """Supersedes bare ``gh run cancel`` for the fast-tier fail-fast step."""
    wrapper = inspect.getsource(ci_fast_cancel._cancel_run)
    assert "_cancel_run_logged" in wrapper
    main_source = inspect.getsource(ci_fast_cancel.main)
    assert "gh run cancel" not in main_source.replace("`gh run cancel`", "")
    assert "_cancel_run(" in main_source


def test_ci_trunk_tip_only_routes_through_logged_cancel() -> None:
    """Supersedes bare ``gh api .../cancel`` for the trunk-tip sweeper."""
    source = inspect.getsource(ci_trunk_tip_only._cancel_run)
    assert "_cancel_run_logged" in source
