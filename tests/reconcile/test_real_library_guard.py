"""The real-library precondition in tests/reconcile/conftest.py.

Single-line intent, one assertion block each:
- if a library at the current schema does not pass the gate, every
  real-library test in this package skips on a machine that HAS a library --
  broken.
- if a library at an older schema passes the gate, it reaches the tests and
  dies on a column its migration never added, which reads as a broken test
  rather than a stale fixture -- broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from tests.reconcile.conftest import _require_current_schema


def test_a_library_at_the_current_schema_passes_the_gate() -> None:
    # The presence case, asserted first: a control that only ever checked the
    # raising branch would pass even if the gate rejected everything.
    _require_current_schema(Path("/nonexistent/state.db"), state_schema.SCHEMA_VERSION)


@pytest.mark.parametrize("version", [None, 5, state_schema.SCHEMA_VERSION - 1])
def test_a_library_at_another_schema_fails_loudly_naming_both_versions(
    version: int | None,
) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        _require_current_schema(Path("/tmp/stale/state.db"), version)

    message = str(excinfo.value)
    # Naming BOTH numbers is the point: "incompatible" alone does not tell a
    # reader whether the library or the code is the stale one.
    assert f"v{version}" in message
    assert f"v{state_schema.SCHEMA_VERSION}" in message
    assert "not a skip" in message
