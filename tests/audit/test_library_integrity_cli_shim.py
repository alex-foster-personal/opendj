"""`apps.audit.library_integrity` is now a CLI + re-export over the shared guard.

The guard moved to `apps.shared.library_integrity` to invert the `sync -> audit`
dependency that PR #431 introduced (`apps.sync.playlist_apply` gates live writes
on it, and `apps.audit` already imported `apps.sync` in three places, so the two
packages formed a cycle). Two things must survive that move, and neither is
covered by the guard's own tests:

  [if] the documented operator CLI `python -m apps.audit.library_integrity`
       stops resolving [then] .planning/ROADMAP.md and the usb-import-export
       skill both point at a dead command ⛔️ it still parses --help
  [if] `from apps.audit.library_integrity import assert_healthy` starts
       returning a SECOND copy of the guard [then] a threshold or gate fix
       lands in one place and silently misses the other ⛔️ the names are the
       identical objects
  [if] apps.audit re-acquires an import edge back into the shared guard's old
       home [then] the cycle returns ⛔️ apps.shared imports no sibling package

-Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.audit import library_integrity as audit_gate
from apps.shared import library_integrity as shared_gate


@pytest.mark.parametrize(
    "name",
    [
        "DEFAULT_THRESHOLD",
        "IntegrityReport",
        "LibraryIntegrityError",
        "TrackLike",
        "assert_healthy",
        "check_integrity",
        "live_report",
        "rehome_path",
    ],
)
def test_audit_reexports_the_identical_shared_object(name: str) -> None:
    assert getattr(audit_gate, name) is getattr(shared_gate, name)


def test_gate_surface_is_reachable_from_the_historic_import_path() -> None:
    """The four names apps.sync.playlist_apply depends on, via the old path."""
    from apps.audit.library_integrity import (  # noqa: F401
        DEFAULT_THRESHOLD,
        LibraryIntegrityError,
        assert_healthy,
        live_report,
    )


def test_operator_cli_still_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    """`python -m apps.audit.library_integrity --help` works (no live DB read)."""
    with pytest.raises(SystemExit) as exit_info:
        audit_gate.main(["--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "python -m apps.audit.library_integrity" in help_text
    assert "--strict" in help_text
    assert "--threshold" in help_text


def test_shared_guard_imports_no_sibling_app_package() -> None:
    """The whole point of the move: apps.shared stays the leaf.

    Mirrors the `shared-is-the-stable-core` contract in .importlinter at unit
    speed, so a bad import fails here before the arch gate runs.
    """
    assert shared_gate.__file__ is not None
    source = Path(shared_gate.__file__).read_text(encoding="utf-8")
    siblings = ("apps.audit", "apps.sync", "apps.webui", "apps.reconcile")
    offenders = [s for s in siblings if f"import {s}" in source or f"from {s}" in source]
    assert offenders == [], f"apps.shared.library_integrity reaches into {offenders}"
