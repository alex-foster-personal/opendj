"""NATIVE-12: the refusal half of the Mixed In Key corpus guard.

``data/reference/mik/<date>/`` holds a captured Mixed In Key run. NATIVE-12 says
it is reference data only: anything taken from it into files, the odj library or
``state.db`` needs its OWN recorded decision first, and ``state.db`` has no such
decision today.

This module covers ``apps.mik.reference_guard`` itself: that the flag refuses a
module with no decision, admits one that has a decision for exactly the action
it asked for, and that a registry which cannot be read or is not backed by its
record document fails loudly rather than reading as an empty one. The static
half -- that no source in the checkout reaches this corpus unclassified -- is
``tests/mik/test_reference_import_guard_sweep.py``.

One line, in the repo's marked-scope form:
  - [if] a module has no import decision [then] the guard refuses it, [else stop].

Regression lines:
  - if the guard admits a module that is not in the registry then broken
  - if the guard refuses a module that IS in the registry then broken
  - if a recorded decision names a record document that does not exist then broken
  - if an unreadable registry is read as an empty one then broken
  - if an import decision is recorded without editing this suite then broken
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.mik import reference_guard
from tests.mik.guard_helpers import corpus_path, registry_entry, write_record, write_registry

pytestmark = pytest.mark.requirement("NATIVE-12")


def test_every_decision_is_backed_by_a_record_document(tmp_path: Path) -> None:
    """A decision with no document behind it is a name, not a decision."""
    registry = write_registry(
        tmp_path / "registry.json",
        [
            {
                "module": "apps/importer.py",
                "action": "import",
                "decision": "MIK-IMPORT-99",
                "record": "gone.md",
            }
        ],
    )
    with pytest.raises(reference_guard.MikImportDecisionUnrecorded) as excinfo:
        reference_guard.load_import_decisions(registry, tmp_path)
    assert "MIK-IMPORT-99" in str(excinfo.value)


def test_no_import_decision_is_recorded_today() -> None:
    """The snapshot NATIVE-12 pins: nothing from the corpus has been imported.

    Flipping this IS the deliberate act the requirement asks for. Do it only with
    a recorded decision, and update this assertion in the same commit.
    """
    assert reference_guard.load_import_decisions() == (), (
        "an import decision was recorded. That is allowed only with its own recorded "
        "decision document (NATIVE-12); name it in this test so the change cannot land "
        "silently."
    )



# ----------------------------------------------------------- the refusal itself


def test_guard_refuses_an_unregistered_module() -> None:
    """Nothing is registered, so the flag refuses every caller today."""
    with pytest.raises(reference_guard.MikReferenceImportRefused) as excinfo:
        reference_guard.require_mik_import_decision("apps/mik/load.py", action="import")
    message = str(excinfo.value)
    assert "apps/mik/load.py" in message
    assert "import_decisions.json" in message


def test_guard_admits_a_registered_module(tmp_path: Path) -> None:
    """Once a decision exists, the named module passes and an unnamed one does not."""
    write_record(tmp_path, "MIK-IMPORT-02")
    registry = write_registry(
        tmp_path / "registry.json", [registry_entry("apps/mik/load.py", "MIK-IMPORT-02")]
    )
    granted = reference_guard.require_mik_import_decision(
        "apps/mik/load.py", action="import", registry_path=registry, record_root=tmp_path
    )
    assert granted.decision == "MIK-IMPORT-02"
    with pytest.raises(reference_guard.MikReferenceImportRefused):
        reference_guard.require_mik_import_decision(
            "apps/mik/match.py", action="import", registry_path=registry, record_root=tmp_path
        )
    with pytest.raises(reference_guard.MikReferenceImportRefused) as excinfo:
        reference_guard.require_mik_import_decision(
            "apps/mik/load.py", action="read", registry_path=registry, record_root=tmp_path
        )
    assert "import" in str(excinfo.value)


def test_guard_rejects_an_unreadable_registry(tmp_path: Path) -> None:
    """A missing registry must fail loudly, never read as 'nothing is registered, carry on'."""
    with pytest.raises(FileNotFoundError):
        reference_guard.load_import_decisions(tmp_path / "absent.json")


def test_a_registry_without_the_decisions_key_is_refused(tmp_path: Path) -> None:
    """A lost key must not read as the empty registry NATIVE-12 pins."""
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"version": 1, "corpus_path": corpus_path()}), encoding="utf-8")
    with pytest.raises(reference_guard.MikImportDecisionUnrecorded) as excinfo:
        reference_guard.load_import_decisions(registry, tmp_path)
    assert "decisions" in str(excinfo.value)


def test_a_decision_for_another_action_does_not_grant_this_one(tmp_path: Path) -> None:
    """A read grant is refused for import, and the refusal says which grant it found."""
    write_record(tmp_path, "MIK-IMPORT-05")
    registry = write_registry(
        tmp_path / "registry.json",
        [registry_entry("apps/mik/load.py", "MIK-IMPORT-05", action="read")],
    )
    with pytest.raises(reference_guard.MikReferenceImportRefused) as excinfo:
        reference_guard.require_mik_import_decision(
            "apps/mik/load.py", action="import", registry_path=registry, record_root=tmp_path
        )
    assert "read" in str(excinfo.value)


def test_the_lookup_answers_by_module_and_action_not_by_order(tmp_path: Path) -> None:
    """Both actions resolve to their own decision, whichever order the file lists them in."""
    write_record(tmp_path, "MIK-IMPORT-06")
    entries = [
        registry_entry("apps/mik/load.py", "MIK-IMPORT-06", action="read"),
        registry_entry("apps/mik/load.py", "MIK-IMPORT-06", action="import"),
    ]
    for order, decisions in (("read first", entries), ("import first", list(reversed(entries)))):
        registry = write_registry(tmp_path / "registry.json", decisions)
        for action in ("read", "import"):
            granted = reference_guard.require_mik_import_decision(
                "apps/mik/load.py", action=action, registry_path=registry, record_root=tmp_path
            )
            assert granted.action == action, order


def test_two_decisions_for_one_action_are_refused(tmp_path: Path) -> None:
    """Two entries for one (module, action) leave the grant ambiguous, so refuse."""
    write_record(tmp_path, "MIK-IMPORT-07")
    registry = write_registry(
        tmp_path / "registry.json",
        [
            registry_entry("apps/mik/load.py", "MIK-IMPORT-07"),
            registry_entry("apps/mik/load.py", "MIK-IMPORT-07"),
        ],
    )
    with pytest.raises(reference_guard.MikImportDecisionUnrecorded):
        reference_guard.load_import_decisions(registry, tmp_path)


def test_a_decision_record_outside_the_tree_is_refused(tmp_path: Path) -> None:
    """An absolute path, or one climbing out with .., is not a document in the repository."""
    outside = tmp_path / "outside.md"
    outside.write_text("Decision MIK-IMPORT-08 records nothing reviewable.\n", "utf-8")
    inside = tmp_path / "tree"
    inside.mkdir()
    for record in ("/tmp/decision.md", "../outside.md", str(outside)):
        registry = write_registry(
            tmp_path / "registry.json",
            [
                {
                    "module": "apps/mik/load.py",
                    "action": "import",
                    "decision": "MIK-IMPORT-08",
                    "record": record,
                }
            ],
        )
        with pytest.raises(reference_guard.MikImportDecisionUnrecorded) as excinfo:
            reference_guard.load_import_decisions(registry, inside)
        assert "MIK-IMPORT-08" in str(excinfo.value)
