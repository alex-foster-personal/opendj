"""CLI-level coverage for the equivalence gate's publish timing (PR #383 review).

The canonical verdict file feeds ``apps.shared.equivalence.EquivalenceGate`` on
the NEXT MIK load. Two failure modes were possible before this suite existed:

1. A run that later fails its own known-answer check (exit 3) had ALREADY
   replaced the gate file with the unproven verdicts from this run.
2. ``run --fields X`` only computes verdicts for ``X``, and a wholesale write
   erased every OTHER field's already-proven verdict from the gate.

Both are exercised end to end through ``apps.equivalence.__main__.main`` with
``read_rekordbox``/``read_mik``/``read_mik_energy_segments``/
``run_known_answers`` monkeypatched, so no real rekordbox or MIK database is
needed -- empty source rows are enough to reach the write-gating logic
without tripping any mapping-bug cluster.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.equivalence import __main__ as cli
from apps.equivalence.compare import Agreement
from apps.equivalence.config import SINGLE_SOURCE_FIELDS
from apps.equivalence.probe import Probe
from apps.equivalence.verdict import PASSED, SUITE_PASSED, PairVerdict, build_document


def _probe(**kw) -> Probe:
    base: dict[str, Any] = dict(
        source="rekordbox",
        locator="djmdContent.BPM",
        kind="bpm",
        declared_unit="centi_bpm",
        rows=200,
        present=200,
        null=0,
        distinct=50,
        numeric=True,
    )
    base.update(kw)
    return Probe(**base)


def _passing_bpm_verdict() -> PairVerdict:
    agreement = Agreement(
        pairs=200,
        comparable=200,
        agree=200,
        disagree=0,
        left_missing=0,
        right_missing=0,
        left_unmapped=0,
        right_unmapped=0,
        tolerance=1.0,
        tolerance_note="1.0 BPM",
    )
    return PairVerdict(
        field_name="bpm",
        status=PASSED,
        suite_status=SUITE_PASSED,
        reasons=["200/200 agree (100.0%)"],
        left=_probe(),
        right=_probe(source="mik", locator="ZSONG.ZTEMPO", declared_unit="bpm"),
        agreement=agreement,
        clusters=[],
        mismatches=[],
        normaliser="bpm:centi_bpm->bpm | bpm:bpm->bpm",
    )


class _FakeKnownReport:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def as_dict(self) -> dict:
        return self._payload


def _patch_empty_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "read_rekordbox", lambda _path: [])
    monkeypatch.setattr(cli, "read_mik", lambda _path: [])
    monkeypatch.setattr(cli, "read_mik_energy_segments", lambda _path: [])


def _patch_known_answers(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> None:
    payload = {
        "fixture": "fake-fixture.json",
        "tracks": 0,
        "passed": 0,
        "failed": 0 if ok else 1,
        "pending_human_verification": 0,
        "unresolved_tracks": 0,
        "unresolved_checks": 0,
        "ok": ok,
        "results": [],
        "note": "fake report for CLI publish-timing tests",
    }
    monkeypatch.setattr(
        cli, "run_known_answers", lambda *a, **k: _FakeKnownReport(payload)
    )


def test_a_corrupt_source_exits_source_unreadable_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that exists but is corrupt/locked/schema-drifted raises
    ``sqlite3.DatabaseError`` (or its ``OperationalError`` subclass), not
    ``FileNotFoundError`` (P2 review, PR #383). Before the fix, ``main`` only
    caught ``FileNotFoundError`` here, so this class of failure fell through
    to an unhandled traceback and Python's default exit code 1 -- masking the
    documented ``EXIT_SOURCE_UNREADABLE`` (4) contract callers rely on to
    distinguish an environment/source problem from an unspecified crash."""

    def _raise_corrupt(_path):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(cli, "read_rekordbox", _raise_corrupt)
    monkeypatch.setattr(cli, "read_mik", lambda _path: [])

    exit_code = cli.main(["run", "--data-dir", str(tmp_path), "--json"])

    assert exit_code == cli.EXIT_SOURCE_UNREADABLE


def test_gate_not_written_when_known_answers_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    _patch_known_answers(monkeypatch, ok=False)

    exit_code = cli.main(["run", "--data-dir", str(tmp_path), "--json"])

    assert exit_code == cli.EXIT_KNOWN_ANSWER_FAILED
    assert not (tmp_path / "state" / "equivalence-verdicts.json").exists()


def test_gate_written_when_run_is_trustworthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    _patch_known_answers(monkeypatch, ok=True)

    exit_code = cli.main(["run", "--data-dir", str(tmp_path), "--json"])

    assert exit_code == cli.EXIT_OK
    verdicts_path = tmp_path / "state" / "equivalence-verdicts.json"
    assert verdicts_path.exists()
    written = json.loads(verdicts_path.read_text(encoding="utf-8"))
    assert set(written["fields"]) == {p.field_name for p in cli.FIELD_PAIRS} | {
        s.field_name for s in SINGLE_SOURCE_FIELDS
    }


def test_partial_fields_run_merges_onto_existing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--fields key`` run must not demote ``bpm``'s prior proven verdict."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    verdicts_path = state_dir / "equivalence-verdicts.json"

    prior = build_document(
        [_passing_bpm_verdict()],
        match_report={},
        known_answers={"ok": True},
        single_source=[],
    )
    verdicts_path.write_text(json.dumps(prior, indent=2), encoding="utf-8")

    _patch_empty_sources(monkeypatch)
    _patch_known_answers(monkeypatch, ok=True)

    exit_code = cli.main(
        ["run", "--data-dir", str(tmp_path), "--json", "--fields", "key"]
    )

    assert exit_code == cli.EXIT_OK
    written = json.loads(verdicts_path.read_text(encoding="utf-8"))
    assert written["fields"]["bpm"]["status"] == "passed"  # untouched by this run
    assert "key" in written["fields"]  # freshly recomputed by this run


def test_merge_with_existing_gate_ignores_a_corrupt_prior_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "equivalence-verdicts.json"
    path.write_text("{not json", encoding="utf-8")
    document = {"fields": {"key": {"status": "passed"}}}

    merged = cli._merge_with_existing_gate(document, path)

    assert merged is document
    assert merged["fields"] == {"key": {"status": "passed"}}


def test_merge_with_existing_gate_no_prior_file_is_a_no_op(tmp_path: Path) -> None:
    document = {"fields": {"key": {"status": "passed"}}}

    merged = cli._merge_with_existing_gate(document, tmp_path / "nope.json")

    assert merged is document
