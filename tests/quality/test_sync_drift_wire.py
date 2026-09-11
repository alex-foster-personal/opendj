"""D-12 wire_shape_changed must be able to FAIL, and must stay quiet otherwise.

The check's subject is a fresh run of the shared-state ladder, so the
defects are injected there: a ladder step appended the way a real migration
would be (MIGRATIONS and SCHEMA_VERSION moved together).

Regression lines:
  - if a ladder step adds a synced column with no wire bump and D-12 is silent then broken
  - if a ladder step touching only a machine-local table makes D-12 fire then broken
  - if D-12 reports a violation against the real tree then broken

[if] D-12 misses a synced column added without a bump [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.state import schema as state_schema
from scripts import sync_drift_lint as lint

pytestmark = pytest.mark.requirement("CAT-04")


def _append_step(monkeypatch: pytest.MonkeyPatch, statement: str) -> None:
    monkeypatch.setattr(state_schema, "MIGRATIONS", [*state_schema.MIGRATIONS, [statement]])
    monkeypatch.setattr(state_schema, "SCHEMA_VERSION", state_schema.SCHEMA_VERSION + 1)


def test_d12_fires_on_a_synced_column_added_without_a_wire_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If D-12 misses a synced column added by a new ladder step then broken."""
    _append_step(monkeypatch, "ALTER TABLE playlists ADD COLUMN wire_probe TEXT")

    found = lint.check_wire_shape_changed(None)  # type: ignore[arg-type]

    assert [v.rule for v in found] == ["wire_shape_changed"]
    assert "wire_probe" in found[0].detail


def test_d12_ignores_a_machine_local_ladder_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a local-only ladder step makes D-12 demand a wire bump then broken."""
    _append_step(monkeypatch, "CREATE TABLE wire_probe_local (id INTEGER PRIMARY KEY)")

    assert lint.check_wire_shape_changed(None) == []  # type: ignore[arg-type]


def test_d12_clean_against_the_real_tree() -> None:
    """If D-12 reports drift on the tree as committed then broken."""
    assert lint.check_wire_shape_changed(None) == []  # type: ignore[arg-type]
