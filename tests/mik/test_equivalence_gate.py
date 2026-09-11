"""Equivalence-gate tests: fail-closed in every direction.

[if] a verdict is absent, untested, or bound elsewhere [then] gate refuses/degrades, [else stop].
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.equivalence import (
    EquivalenceGate,
    EquivalenceGateError,
    verdict_path,
)

pytestmark = pytest.mark.requirement("META-01")


def test_absent_file_means_everything_is_untested(data_dir: Path) -> None:
    gate = EquivalenceGate.load(data_dir)
    assert gate.file_present is False
    for field in ("key", "energy", "bpm", "loudness", "energy_segments"):
        assert gate.verdict(field).status == "untested"
        assert gate.may_write(field) is False


def test_passed_unlocks_only_that_field(data_dir: Path, write_verdicts) -> None:
    write_verdicts({"energy": "passed", "bpm": "failed"})
    gate = EquivalenceGate.load(data_dir)
    assert gate.may_write("energy") is True
    assert gate.may_write("bpm") is False
    assert gate.may_write("loudness") is False  # no entry at all


def test_override_allows_and_names_the_field(
    data_dir: Path, write_verdicts, caplog
) -> None:
    write_verdicts({"bpm": "failed"})
    gate = EquivalenceGate.load(data_dir, allow_unverified=True)
    with caplog.at_level("WARNING"):
        assert gate.may_write("bpm") is True
        assert gate.may_write("bpm") is True  # warns once, not twice
    warnings = [r for r in caplog.records if "EQUIVALENCE OVERRIDE" in r.message]
    assert len(warnings) == 1
    assert "bpm" in warnings[0].getMessage()


def test_unparseable_file_raises_never_degrades_to_allow(data_dir: Path) -> None:
    verdict_path(data_dir).write_text("{not json", encoding="utf-8")
    with pytest.raises(EquivalenceGateError, match="unreadable"):
        EquivalenceGate.load(data_dir)


def test_entry_without_status_raises(data_dir: Path) -> None:
    verdict_path(data_dir).write_text(
        json.dumps({"energy": {"normaliser": "x"}}), encoding="utf-8"
    )
    with pytest.raises(EquivalenceGateError, match="no 'status' key"):
        EquivalenceGate.load(data_dir)


def test_unknown_status_raises(data_dir: Path) -> None:
    verdict_path(data_dir).write_text(
        json.dumps({"energy": {"status": "probably-fine"}}), encoding="utf-8"
    )
    with pytest.raises(EquivalenceGateError, match="expected one of"):
        EquivalenceGate.load(data_dir)


def test_passed_without_a_normaliser_raises(data_dir: Path) -> None:
    """SKILL 4b: record the test, not just the result."""
    verdict_path(data_dir).write_text(
        json.dumps({"energy": {"status": "passed", "normaliser": None}}),
        encoding="utf-8",
    )
    with pytest.raises(EquivalenceGateError, match="names no 'normaliser'"):
        EquivalenceGate.load(data_dir)


def test_wrapped_fields_key_is_accepted(data_dir: Path) -> None:
    verdict_path(data_dir).write_text(
        json.dumps(
            {
                "meta": {"tool": "unit-b"},
                "fields": {
                    "energy": {"status": "passed", "normaliser": "mik_1_10"}
                },
            }
        ),
        encoding="utf-8",
    )
    gate = EquivalenceGate.load(data_dir)
    assert gate.may_write("energy") is True
    assert gate.may_write("meta") is False


def test_non_object_top_level_raises(data_dir: Path) -> None:
    verdict_path(data_dir).write_text(json.dumps(["energy"]), encoding="utf-8")
    with pytest.raises(EquivalenceGateError, match="expected a JSON object"):
        EquivalenceGate.load(data_dir)


class TestSingleSourceBasis:
    """The producer's shipped form: status 'passed' + mandatory basis.

    Agreed with apps/equivalence Tue 28 Jul 2026. A source-unique field (MIK's
    energy series, MIK's clipped-peak count) has no cross-source counterpart,
    so basis is the only thing that separates "cross-validated" from "probed
    one side and asserted totality".
    """

    def _write(self, data_dir: Path, entry: dict) -> None:
        verdict_path(data_dir).write_text(
            json.dumps({"fields": {"energy_segments": entry}}), encoding="utf-8"
        )

    def test_producer_form_is_writable_and_recorded_as_single_source(
        self, data_dir: Path
    ) -> None:
        self._write(
            data_dir,
            {
                "status": "passed",
                "basis": "single_source",
                "suite_status": "passed_single_source",
                "normaliser": "mik_seconds_to_ms",
                "checked_at": "2026-07-28T01:00:00+00:00",
                "verified_by": "apps.equivalence",
            },
        )
        gate = EquivalenceGate.load(data_dir)
        verdict = gate.verdict("energy_segments")
        assert gate.may_write("energy_segments") is True
        assert verdict.basis == "single_source"
        assert verdict.is_single_source is True
        assert verdict.verified_by == "apps.equivalence"

    def test_explicit_status_spelling_is_also_accepted(self, data_dir: Path) -> None:
        self._write(
            data_dir,
            {
                "status": "passed_single_source",
                "normaliser": "mik_seconds_to_ms",
                "checked_at": "2026-07-28T01:00:00+00:00",
            },
        )
        gate = EquivalenceGate.load(data_dir)
        assert gate.may_write("energy_segments") is True
        assert gate.verdict("energy_segments").basis == "single_source"

    def test_dropped_basis_falls_back_to_suite_status(self, data_dir: Path) -> None:
        """The hole this rung closes: a bare 'passed' must not read as cross."""
        self._write(
            data_dir,
            {
                "status": "passed",
                "suite_status": "passed_single_source",
                "normaliser": "mik_seconds_to_ms",
            },
        )
        gate = EquivalenceGate.load(data_dir)
        assert gate.verdict("energy_segments").basis == "single_source"

    def test_cross_source_pass_is_recorded_as_cross_source(
        self, data_dir: Path
    ) -> None:
        self._write(
            data_dir,
            {
                "status": "passed",
                "basis": "cross_source",
                "suite_status": "passed",
                "normaliser": "camelot_upper_nfc",
            },
        )
        verdict = EquivalenceGate.load(data_dir).verdict("energy_segments")
        assert verdict.basis == "cross_source"
        assert verdict.is_single_source is False

    def test_basis_contradicting_the_status_is_rejected(self, data_dir: Path) -> None:
        self._write(
            data_dir,
            {
                "status": "passed_single_source",
                "basis": "cross_source",
                "normaliser": "x",
            },
        )
        with pytest.raises(EquivalenceGateError, match="contradictory"):
            EquivalenceGate.load(data_dir)

    def test_basis_contradicting_the_suite_status_is_rejected(
        self, data_dir: Path
    ) -> None:
        self._write(
            data_dir,
            {
                "status": "passed",
                "basis": "cross_source",
                "suite_status": "passed_single_source",
                "normaliser": "x",
            },
        )
        with pytest.raises(EquivalenceGateError, match="contradictory"):
            EquivalenceGate.load(data_dir)

    def test_non_passing_verdict_keeps_its_pairing_but_verifies_nothing(
        self, data_dir: Path
    ) -> None:
        """The producer sends basis on untested entries too: it is the PAIRING
        kind, not the outcome. Evidence basis must still read 'unverified'."""
        self._write(
            data_dir,
            {
                "status": "untested",
                "basis": "cross_source",
                "suite_status": "inconclusive_suspect_cluster",
            },
        )
        verdict = EquivalenceGate.load(data_dir).verdict("energy_segments")
        assert verdict.pairing == "cross_source"
        assert verdict.basis == "unverified"
        assert verdict.is_single_source is False
        assert EquivalenceGate.load(data_dir).may_write("energy_segments") is False

    def test_passing_verdict_must_name_a_basis(self, data_dir: Path) -> None:
        self._write(
            data_dir,
            {"status": "passed", "basis": "unverified", "normaliser": "x"},
        )
        with pytest.raises(EquivalenceGateError, match="must say whether"):
            EquivalenceGate.load(data_dir)

    def test_unknown_basis_is_rejected(self, data_dir: Path) -> None:
        self._write(
            data_dir, {"status": "passed", "basis": "vibes", "normaliser": "x"}
        )
        with pytest.raises(EquivalenceGateError, match="expected one of"):
            EquivalenceGate.load(data_dir)

    def test_provenance_rows_carry_the_basis(self, data_dir: Path) -> None:
        self._write(
            data_dir,
            {
                "status": "passed",
                "basis": "single_source",
                "suite_status": "passed_single_source",
                "normaliser": "mik_seconds_to_ms",
                "checked_at": "2026-07-28T01:00:00+00:00",
                "verified_by": "apps.equivalence",
            },
        )
        rows = EquivalenceGate.load(data_dir).provenance_rows(
            ["energy_segments", "bpm"]
        )
        by_field = {row["field_name"]: row for row in rows}
        assert by_field["energy_segments"]["basis"] == "single_source"
        assert by_field["energy_segments"]["verified_by"] == "apps.equivalence"
        assert by_field["bpm"]["basis"] == "unverified"
        assert by_field["bpm"]["status"] == "untested"


def test_blocked_reason_explains_which_gate_fired(
    data_dir: Path, write_verdicts
) -> None:
    write_verdicts({"bpm": "failed"})
    gate = EquivalenceGate.load(data_dir)
    assert "FAILED" in gate.blocked_reason("bpm")
    assert "has not run" in gate.blocked_reason("loudness")
    with pytest.raises(ValueError, match="not blocked"):
        write_verdicts({"energy": "passed"})
        EquivalenceGate.load(data_dir).blocked_reason("energy")


# --------------------------------------------- source-bound verdicts (P1)


def _write_verdict_with_sources(
    data_dir: Path, *, mik_fingerprint: dict
) -> None:
    payload = {
        "meta": {"sources": {"mik": mik_fingerprint}},
        "fields": {
            "energy": {
                "status": "passed",
                "normaliser": "test_energy",
                "checked_at": "2026-07-28T00:00:00+00:00",
            }
        },
    }
    verdict_path(data_dir).write_text(json.dumps(payload), encoding="utf-8")


def test_load_with_no_sources_arg_skips_the_binding_check(
    data_dir: Path, tmp_path: Path
) -> None:
    """Omitting ``sources=`` (the default) must not require meta.sources at
    all -- callers that read no external source themselves have nothing to
    bind, and existing hand-written verdict fixtures never carry it."""
    mik_store = tmp_path / "real.mikdb"
    mik_store.write_bytes(b"real content")
    stat = mik_store.stat()
    _write_verdict_with_sources(
        data_dir,
        mik_fingerprint={
            "path": str(mik_store),
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        },
    )
    gate = EquivalenceGate.load(data_dir)
    assert gate.may_write("energy") is True


def test_load_rejects_a_verdict_bound_to_a_different_store(
    data_dir: Path, tmp_path: Path
) -> None:
    """P1 regression (PR #383 review, apps/shared/equivalence.py): a verdict
    computed against one MIK store must not authorize values read from a
    DIFFERENT store at load time, even if apps.equivalence was run at some
    point against SOME file -- the passing status proves nothing about a
    source it never probed."""
    verified_store = tmp_path / "verified.mikdb"
    verified_store.write_bytes(b"the store apps.equivalence actually probed")
    stat = verified_store.stat()
    _write_verdict_with_sources(
        data_dir,
        mik_fingerprint={
            "path": str(verified_store),
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        },
    )
    different_store = tmp_path / "different.mikdb"
    different_store.write_bytes(b"a totally different, unprobed store")

    with pytest.raises(EquivalenceGateError, match="different"):
        EquivalenceGate.load(data_dir, sources={"mik": different_store})


def test_load_rejects_an_upgraded_store_at_the_same_path(
    data_dir: Path, tmp_path: Path
) -> None:
    """Same requirement, the OTHER way it was described in review: 'the MIK
    store is upgraded' at the SAME path. Path equality must not be enough."""
    store = tmp_path / "same_path.mikdb"
    store.write_bytes(b"version probed by apps.equivalence")
    stat = store.stat()
    _write_verdict_with_sources(
        data_dir,
        mik_fingerprint={
            "path": str(store),
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        },
    )
    # The file at the SAME path is replaced in place (an upgrade), changing
    # its size and mtime without changing its path at all.
    store.write_bytes(b"a much longer replacement payload after the upgrade")

    with pytest.raises(EquivalenceGateError, match="different"):
        EquivalenceGate.load(data_dir, sources={"mik": store})


def test_load_allows_a_verdict_bound_to_the_same_unchanged_store(
    data_dir: Path, tmp_path: Path
) -> None:
    store = tmp_path / "same.mikdb"
    store.write_bytes(b"unchanged content")
    stat = store.stat()
    _write_verdict_with_sources(
        data_dir,
        mik_fingerprint={
            "path": str(store),
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        },
    )
    gate = EquivalenceGate.load(data_dir, sources={"mik": store})
    assert gate.may_write("energy") is True


def test_load_source_mismatch_is_overridable_and_fails_closed(
    data_dir: Path, tmp_path: Path, caplog
) -> None:
    """``allow_unverified`` degrades a source mismatch the same way it
    degrades an absent verdict file: every field becomes untested (writable
    only through the explicit override), never silently trusted."""
    verified_store = tmp_path / "verified.mikdb"
    verified_store.write_bytes(b"probed content")
    stat = verified_store.stat()
    _write_verdict_with_sources(
        data_dir,
        mik_fingerprint={
            "path": str(verified_store),
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        },
    )
    different_store = tmp_path / "different.mikdb"
    different_store.write_bytes(b"unprobed content")

    with caplog.at_level("WARNING"):
        gate = EquivalenceGate.load(
            data_dir, allow_unverified=True, sources={"mik": different_store}
        )
    assert gate.verdict("energy").status == "untested"
    assert gate.may_write("energy") is True  # only via the override
    assert any("different" in r.message for r in caplog.records)
