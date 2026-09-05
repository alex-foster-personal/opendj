"""Source-unique fields: probe + totality + structure, and NO agreement rate.

Regression lines:

- if a single-source verdict omits `basis` or reports `cross_source` then broken
- if a single-source verdict carries an agreement rate then broken
- if a single-source pass uses a bare `suite_status` of 'passed' then broken
- if a time series with overlapping spans is passed then broken
- if a time series with a zero-length span is passed then broken
- if a span that would round away to zero milliseconds is not flagged then broken
- if the naive-vs-boundary millisecond conversion difference is not reported then broken
- if a value outside the declared scale is passed then broken
- if a negative first-segment start is treated as corruption rather than a note
  then broken
- if the real MIK series does not pass the structure audit then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.equivalence.config import CFG, SINGLE_SOURCE_FIELDS, SourceField
from apps.equivalence.probe import probe_field
from apps.equivalence.single_source import (
    BASIS_SINGLE,
    SUITE_PASSED_SINGLE,
    audit_series,
    decide_single_source,
    probe_single_source,
)
from apps.equivalence.sources import MikEnergySegment, MikRow, read_mik_energy_segments

SEGMENTS_SPEC = next(
    s for s in SINGLE_SOURCE_FIELDS if s.field_name == "energy_segments"
)
CLIPPING_SPEC = next(
    s for s in SINGLE_SOURCE_FIELDS if s.field_name == "clipped_peak_count"
)


def _seg(pk: int, start: float, length: float, energy: float = 5.0):
    return MikEnergySegment(song_pk=pk, start_s=start, length_s=length, energy=energy)


def _contiguous(pk: int, n: int, *, step: float = 60.0):
    return [_seg(pk, i * step, step, float(i % 10 + 1)) for i in range(n)]


def _mik_rows(count: int, value_fn) -> list[MikRow]:
    return [
        MikRow(
            pk=i,
            path=f"/music/{i}.mp3",
            title=f"t{i}",
            artist="a",
            key_camelot="8A",
            energy=6.0,
            tempo=124.0,
            volume=-13.0,
            rating=0,
            key_confidence=0.9,
            analysed_span_s=300.0,
            clipped_peak_count=value_fn(i),
        )
        for i in range(count)
    ]


# ------------------------------------------------------------ the contract


def test_single_source_verdict_declares_a_weaker_basis_and_no_rate():
    rows = _mik_rows(CFG.min_comparable + 50, lambda i: i % 900)
    verdict = decide_single_source(
        CLIPPING_SPEC, probe_single_source(rows, CLIPPING_SPEC)
    )
    entry = verdict.entry("2026-07-28T00:00:00+00:00")

    assert verdict.status == "passed"
    assert verdict.suite_status == SUITE_PASSED_SINGLE != "passed"
    assert entry["basis"] == BASIS_SINGLE == "single_source"
    assert entry["agreement"] is None
    assert entry["agreement_omitted_because"]
    assert entry["normaliser"], "a pass must name HOW, per the consumer contract"
    assert "WEAKER" in entry["basis_meaning"]


def test_single_source_verdict_is_readable_by_the_real_gate(tmp_path: Path):
    from apps.equivalence.verdict import build_document, write_document
    from apps.shared.equivalence import EquivalenceGate, verdict_path

    rows = _mik_rows(CFG.min_comparable + 50, lambda i: i % 900)
    verdict = decide_single_source(
        CLIPPING_SPEC, probe_single_source(rows, CLIPPING_SPEC)
    )
    document = build_document(
        [],
        match_report={
            "mik_rows": 0,
            "rekordbox_rows": 0,
            "by_tier": {},
            "tier_confidence": {},
            "matched_mik_rows": 0,
            "distinct_rekordbox_rows": 0,
            "unmatched_mik_rows": 0,
            "note": "n/a",
        },
        known_answers=None,
        single_source=[verdict],
    )
    write_document(document, verdict_path(tmp_path))
    gate = EquivalenceGate.load(tmp_path)
    assert gate.may_write("clipped_peak_count") is True
    assert gate.verdict("clipped_peak_count").status == "passed"


def test_too_few_values_is_untested():
    rows = _mik_rows(CFG.min_comparable - 1, lambda i: i % 900)
    verdict = decide_single_source(
        CLIPPING_SPEC, probe_single_source(rows, CLIPPING_SPEC)
    )
    assert verdict.status == "untested"


def test_a_constant_count_column_is_untested():
    rows = _mik_rows(CFG.min_comparable + 50, lambda i: 0)
    verdict = decide_single_source(
        CLIPPING_SPEC, probe_single_source(rows, CLIPPING_SPEC)
    )
    assert verdict.status == "untested"
    assert any("ZERO VARIANCE" in r for r in verdict.reasons), verdict.reasons


# -------------------------------------------------- time-series structure


def test_a_clean_contiguous_series_is_sound():
    structure = audit_series(_contiguous(1, 10), value_range=(1.0, 10.0))
    assert structure.sound
    assert structure.overlapping_tracks == 0
    assert structure.gap_tracks == 0
    assert structure.non_positive_length_segments == 0


def test_overlapping_spans_are_a_violation():
    segments = [_seg(1, 0.0, 60.0), _seg(1, 30.0, 60.0)]
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert not structure.sound
    assert any("overlapping" in v for v in structure.violations)


def test_a_zero_length_span_is_a_violation():
    segments = [_seg(1, 0.0, 60.0), _seg(1, 60.0, 0.0)]
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert not structure.sound
    assert any("length <= 0" in v for v in structure.violations)


def test_a_span_shorter_than_half_a_millisecond_would_vanish():
    segments = [_seg(1, 0.0, 0.0004)]
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert structure.vanishing_segments_after_ms_rounding == 1
    assert not structure.sound
    assert any("VANISH" in v for v in structure.violations)


def test_values_outside_the_declared_scale_are_a_violation():
    """DJ.Studio's energyLevelNr reaches 17 on what looks like a 1-10 column."""
    segments = [_seg(1, 0.0, 60.0, energy=17.0)]
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert not structure.sound
    assert any("outside the declared" in v for v in structure.violations)


def test_naive_millisecond_conversion_hazard_is_reported_not_fatal():
    """Boundaries at x.0005 s: rounding start and length apart overlaps by 1 ms.

    The source data is fine, so this must be a FINDING with an instruction, not
    a violation that blocks the field.
    """
    # round(60000.6) + round(30000.6) = 60001 + 30001 = 90002, but
    # round(90001.2) = 90001, so the naive conversion overlaps by 1 ms.
    segments = [_seg(1, 60.0006, 30.0006), _seg(1, 90.0012, 30.0)]
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert structure.overlapping_tracks == 0, "no overlap in seconds"
    assert structure.overlapping_tracks_naive_ms == 1
    assert structure.overlapping_tracks_boundary_ms == 0
    assert structure.sound, "a naive-conversion hazard must not block the field"
    assert any("CONVERSION HAZARD" in f for f in structure.findings)
    assert any("Convert BOUNDARIES" in f for f in structure.findings)


def test_a_negative_first_start_is_a_note_not_a_violation():
    segments = [_seg(1, -0.019488616669108, 60.0), _seg(1, 59.980511383330892, 60.0)]
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert structure.negative_start_segments == 1
    assert structure.sound
    assert any("start BEFORE 0 s" in f for f in structure.findings)


def test_a_gap_is_a_note_not_a_violation():
    segments = [_seg(1, 0.0, 30.0), _seg(1, 60.0, 30.0)]
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert structure.gap_tracks == 1
    assert structure.sound
    assert any("gap" in f for f in structure.findings)


def test_structure_violation_fails_the_verdict():
    segments = [_seg(1, 0.0, 60.0), _seg(1, 30.0, 60.0)]
    probe = probe_field(segments, SEGMENTS_SPEC.source)
    structure = audit_series(segments, value_range=(1.0, 10.0))
    verdict = decide_single_source(SEGMENTS_SPEC, probe, structure)
    assert verdict.status == "failed"
    assert verdict.suite_status == "failed_structure"


# ------------------------------------------------------ malformed rows


def _make_mik_segment_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """A minimal MIK store with only the ``ZENERGYSEGMENT`` table."""
    import sqlite3

    db_path = tmp_path / "mik.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE ZENERGYSEGMENT "
        "(ZSONG INTEGER, ZSTARTTIME REAL, ZLENGTH REAL, ZENERGY REAL)"
    )
    conn.executemany(
        "INSERT INTO ZENERGYSEGMENT VALUES (?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()
    return db_path


def test_a_clean_segment_row_reads_normally(tmp_path: Path):
    db_path = _make_mik_segment_db(tmp_path, [(1, 0.0, 30.0, 5.0)])
    segments = read_mik_energy_segments(db_path)
    assert segments == [MikEnergySegment(song_pk=1, start_s=0.0, length_s=30.0, energy=5.0)]


@pytest.mark.parametrize(
    "row",
    [
        pytest.param((1, None, 30.0, 5.0), id="null_start"),
        pytest.param((1, 0.0, None, 5.0), id="null_length"),
        pytest.param((1, 0.0, 30.0, None), id="null_energy"),
    ],
)
def test_a_null_column_raises_instead_of_being_silently_dropped(
    tmp_path: Path, row: tuple
):
    """P1 regression (PR #383 review, apps/equivalence/sources.py:233): the
    production MIK reader (apps.mik.mikdb) rejects a ZENERGYSEGMENT row with
    a NULL start, length, or energy. Silently excluding the same row here
    let a single-source verdict pass over a truncated series while claiming
    the source structure was sound -- the row must instead surface as a
    structural failure."""
    from apps.equivalence.sources import MalformedEnergySegment

    db_path = _make_mik_segment_db(tmp_path, [row])
    with pytest.raises(MalformedEnergySegment):
        read_mik_energy_segments(db_path)


# ----------------------------------------------- against the real store


@pytest.mark.skipif(
    not CFG.mik_db.exists(), reason="MIK store not present on this machine"
)
def test_the_real_mik_series_is_structurally_sound():
    segments = read_mik_energy_segments(CFG.mik_db)
    structure = audit_series(segments, value_range=(1.0, 10.0))
    assert structure.segments > 50_000
    assert structure.sound, structure.violations
    # MEASURED Tue 28 Jul 2026: the source has zero overlaps in seconds, but a
    # naive millisecond conversion would corrupt half the library.
    assert structure.overlapping_tracks == 0
    assert structure.overlapping_tracks_boundary_ms == 0
    assert structure.overlapping_tracks_naive_ms > 1_000


@pytest.mark.skipif(
    not CFG.mik_db.exists(), reason="MIK store not present on this machine"
)
def test_the_real_clipping_column_passes_single_source():
    from apps.equivalence.sources import read_mik

    rows = read_mik(CFG.mik_db)
    verdict = decide_single_source(
        CLIPPING_SPEC, probe_single_source(rows, CLIPPING_SPEC)
    )
    assert verdict.status == "passed", verdict.reasons
    assert verdict.suite_status == SUITE_PASSED_SINGLE


def test_declared_energy_segments_spec_is_a_series_with_a_value_range():
    assert SEGMENTS_SPEC.shape == "time_series"
    assert SEGMENTS_SPEC.value_range == (1.0, 10.0)
    assert CLIPPING_SPEC.shape == "scalar"
    assert isinstance(SEGMENTS_SPEC.source, SourceField)


# --------------------------------------- the MIK-only energy SCALAR (task 2)
#
# Regression lines:
# - if the energy scalar is declared as a cross-source pair then broken
# - if the energy scalar does not carry basis single_source then broken
# - if a scalar outside its own series' range is passed then broken
# - if the scalar-vs-series derivability rate is not measured then broken
# - if a source-unique target is missing from the known-answer index then broken

ENERGY_SPEC = next(s for s in SINGLE_SOURCE_FIELDS if s.field_name == "energy")


def test_energy_scalar_is_declared_source_unique_not_a_pair():
    from apps.equivalence.config import FIELD_PAIRS

    assert ENERGY_SPEC.shape == "scalar"
    assert ENERGY_SPEC.companion_series_field == "energy_segments"
    assert "energy" not in {p.field_name for p in FIELD_PAIRS}, (
        "energy is MIK-only, exactly like the energy SERIES; declaring it as a "
        "pair with an absent left side can only ever yield untested_no_data"
    )


def test_scalar_inside_its_series_range_is_contained_and_not_derivable():
    from apps.equivalence.single_source import audit_scalar_against_series

    # Several tracks, and the scalar sits at a DIFFERENT place inside the
    # series range on each, so no single statistic explains it. One track alone
    # would let some hypothesis score 100% by accident.
    segments: list[MikEnergySegment] = []
    scalars: dict[int, float | None] = {}
    for pk, scalar in enumerate((6.0, 4.0, 8.0, 5.0, 7.0)):
        segments += [_seg(pk, 0.0, 60.0, 3.0), _seg(pk, 60.0, 60.0, 8.0)]
        scalars[pk] = scalar
    audit = audit_scalar_against_series(scalars, segments)
    assert audit.sound
    assert audit.scalar_above_series_max == 0
    assert audit.scalar_below_series_min == 0
    assert audit.best_hypothesis_rate < 0.99
    assert audit.as_dict()["derivable_from_series"] is False


def test_scalar_equal_to_the_series_max_everywhere_is_reported_redundant():
    from apps.equivalence.single_source import audit_scalar_against_series

    segments: list[MikEnergySegment] = []
    scalars: dict[int, float | None] = {}
    for pk in range(20):
        segments += [_seg(pk, 0.0, 60.0, 4.0), _seg(pk, 60.0, 60.0, 7.0)]
        scalars[pk] = 7.0
    audit = audit_scalar_against_series(scalars, segments)
    assert audit.best_hypothesis == "max"
    assert audit.best_hypothesis_rate == 1.0
    assert audit.as_dict()["derivable_from_series"] is True


def test_scalar_escaping_its_series_range_fails_as_a_mapping_bug():
    """The DJ.Studio trap in its scalar form: a POINTER stored where a value
    belongs escapes the series' own range, and no second source exists to
    contradict it."""
    from apps.equivalence.single_source import audit_scalar_against_series

    segments = [_seg(1, 0.0, 60.0, 3.0), _seg(1, 60.0, 60.0, 8.0)]
    audit = audit_scalar_against_series({1: 17.0}, segments)
    assert not audit.sound
    assert audit.scalar_above_series_max == 1

    rows = _mik_rows(CFG.min_comparable + 10, lambda i: i % 900)
    verdict = decide_single_source(
        ENERGY_SPEC, probe_single_source(rows, ENERGY_SPEC), series_consistency=audit
    )
    assert verdict.status == "failed"
    assert verdict.suite_status == "failed_mapping_bug"


def test_known_answer_index_covers_source_unique_targets():
    """A declaration moving between two config lists must not silently disable
    a fixture check. `mik.energy` did exactly that when energy left
    FIELD_PAIRS."""
    from apps.equivalence.known import _spec_index

    index = _spec_index()
    assert ("mik", "energy") in index
    assert index[("mik", "energy")].unit == "energy_1_10"


def test_a_check_that_never_ran_is_not_a_pass():
    from apps.equivalence.known import CheckResult, KnownAnswerReport

    stalled = CheckResult(
        track_id="t",
        target="mik.energy",
        expected_raw=5.0,
        actual_raw=None,
        expected_canonical=None,
        actual_canonical=None,
        verified_by="audit-table",
        status="unresolved",
        detail="no declared field for target 'mik.energy'",
    )
    report = KnownAnswerReport(
        fixture_path="x",
        tracks=1,
        passed=0,
        failed=0,
        pending=0,
        unresolved=0,
        results=[stalled],
    )
    assert report.unresolved_checks == 1
    assert report.ok is False


@pytest.mark.skipif(
    not CFG.mik_db.exists(), reason="MIK store not present on this machine"
)
def test_the_real_energy_scalar_passes_single_source_and_is_not_derivable():
    from apps.equivalence.single_source import audit_scalar_against_series
    from apps.equivalence.sources import read_mik

    rows = read_mik(CFG.mik_db)
    audit = audit_scalar_against_series(
        {r.pk: r.energy for r in rows}, read_mik_energy_segments(CFG.mik_db)
    )
    verdict = decide_single_source(
        ENERGY_SPEC, probe_single_source(rows, ENERGY_SPEC), series_consistency=audit
    )
    assert verdict.status == "passed", verdict.reasons
    assert verdict.suite_status == SUITE_PASSED_SINGLE
    assert verdict.basis == BASIS_SINGLE
    assert verdict.entry("2026-07-28T00:00:00+00:00")["agreement"] is None
    # MEASURED: contained on every track, and NOT a stored statistic of the
    # series, so the two columns are not redundant with each other.
    assert audit.scalar_above_series_max == 0
    assert audit.scalar_below_series_min == 0
    assert audit.best_hypothesis_rate < 0.9
    assert audit.scalar_without_series == 7
