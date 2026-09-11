"""Negative controls: inject a known mapping bug and assert the suite catches it.

A detector that has never been shown to fire is not evidence. Each test here
deliberately breaks one thing and asserts the specific verdict.

Regression lines:

- if rekordbox centi-BPM declared as plain 'bpm' is not flagged as a declared-unit
  scale mismatch then broken
- if a whole column shifted to its relative major is not reported as a MAPPING BUG
  then broken
- if every value doubled is not reported as a MAPPING BUG then broken
- if a wrong mapping confined to one notation is not reported as a MAPPING BUG
  then broken
- if a small scattered disagreement is reported as a MAPPING BUG rather than a
  suspect cluster then broken
- if a constant column (zero variance) yields a comparison verdict rather than
  UNTESTED then broken
- if an absent column yields anything other than UNTESTED then broken
- if an unknown loudness unit yields anything other than UNTESTED then broken
- if a clean identical pair does not PASS then broken
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.equivalence.compare import (
    MAPPING_BUG,
    SUSPECT,
    compare_pair,
    detect_offset_clusters,
)
from apps.equivalence.config import CFG, FieldPair, SourceField
from apps.equivalence.normalisers import Key
from apps.equivalence.probe import probe_field
from apps.equivalence.sources import MikRow, Pairing, RbRow
from apps.equivalence.verdict import (
    FAILED,
    PASSED,
    SUITE_INCONCLUSIVE,
    SUITE_MAPPING_BUG,
    SUITE_NO_DATA,
    SUITE_UNKNOWN_UNIT,
    UNTESTED,
    decide,
)

# 300 rows keeps every case above CFG.min_comparable without being slow.
N = 300


# Values VARY across rows on purpose: a synthetic column holding one value
# everywhere trips the zero-variance guard and never reaches the comparison,
# which is what an earlier version of this file did to itself.
def _camelot(idx: int) -> str:
    return f"{idx % 12 + 1}A"


def _centi_bpm(idx: int) -> int:
    return 12_000 + (idx % 25) * 10


def _rb(idx: int, **kw) -> RbRow:
    base: dict[str, Any] = dict(
        content_id=f"rb{idx}",
        path=f"/music/{idx}.mp3",
        file_name=f"{idx}.mp3",
        title=f"track {idx}",
        artist="artist",
        bpm_raw=_centi_bpm(idx),
        key_raw=_camelot(idx),
        length_raw=300 + idx % 60,
        rating=idx % 6,
        loudness=-13.0 - (idx % 7),
    )
    base.update(kw)
    return RbRow(**base)


def _mik(idx: int, **kw) -> MikRow:
    base: dict[str, Any] = dict(
        pk=idx,
        path=f"/music/{idx}.mp3",
        title=f"track {idx}",
        artist="artist",
        key_camelot=_camelot(idx),
        energy=float(idx % 9 + 1),
        tempo=_centi_bpm(idx) / 100.0,
        volume=-13.0 - (idx % 7),
        rating=0,
        key_confidence=0.9,
        analysed_span_s=300.0 + idx % 60,
    )
    base.update(kw)
    return MikRow(**base)


def _pairings(rb_kw_fn=lambda i: {}, mik_kw_fn=lambda i: {}) -> list[Pairing]:
    return [
        Pairing(
            tier="exact_path", left=_rb(i, **rb_kw_fn(i)), right=_mik(i, **mik_kw_fn(i))
        )
        for i in range(N)
    ]


def _spec(
    field_name: str,
    kind: str,
    left_unit: str,
    right_unit: str,
    left_attr: str,
    right_attr: str,
    tolerance: float = 0.0,
) -> FieldPair:
    return FieldPair(
        field_name=field_name,
        left=SourceField("rekordbox", f"rb.{left_attr}", kind, left_unit, left_attr),
        right=SourceField("mik", f"mik.{right_attr}", kind, right_unit, right_attr),
        tolerance=tolerance,
    )


def _run(spec: FieldPair, pairings: list[Pairing]):
    rb_rows = [p.left for p in pairings]
    mik_rows = [p.right for p in pairings]
    left = probe_field(rb_rows, spec.left)
    right = probe_field(mik_rows, spec.right)
    agreement, disagreements = compare_pair(pairings, spec)
    clusters = (
        detect_offset_clusters(
            disagreements,
            kind=spec.left.kind,
            comparable=agreement.comparable,
            form_counts=agreement.form_counts,
        )
        if left.comparable and right.comparable
        else []
    )
    return decide(spec, left, right, agreement, clusters), agreement, clusters


# ------------------------------------------------- the happy path first


def test_a_clean_identical_pair_passes():
    spec = _spec("bpm", "bpm", "centi_bpm", "bpm", "bpm_raw", "tempo", tolerance=1.0)
    verdict, agreement, clusters = _run(spec, _pairings())
    assert verdict.status == PASSED, verdict.reasons
    assert agreement.rate == 1.0
    assert clusters == []
    assert verdict.normaliser  # the contract requires a named normaliser


# ------------------------------------------- injected scale mismatches


def test_centi_bpm_declared_as_bpm_is_a_declared_unit_mismatch():
    spec = _spec("bpm", "bpm", "bpm", "bpm", "bpm_raw", "tempo", tolerance=1.0)
    verdict, _agreement, _clusters = _run(spec, _pairings())
    assert verdict.status == FAILED
    assert verdict.suite_status == SUITE_MAPPING_BUG
    assert any("DECLARED UNIT IMPLAUSIBLE" in r for r in verdict.reasons), (
        verdict.reasons
    )


def test_every_value_doubled_is_a_mapping_bug():
    spec = _spec("bpm", "bpm", "centi_bpm", "bpm", "bpm_raw", "tempo", tolerance=1.0)
    verdict, _agreement, clusters = _run(
        spec,
        _pairings(mik_kw_fn=lambda i: {"tempo": _centi_bpm(i) / 200.0}),
    )
    assert verdict.status == FAILED
    assert [c.classification for c in clusters] == [MAPPING_BUG]
    assert clusters[0].signature == "half_or_double"
    assert clusters[0].population_share == pytest.approx(1.0)


def test_whole_key_column_shifted_to_the_relative_is_a_mapping_bug():
    """The trap SKILL 4b calls out: looks 0% agreeing here, but in a mixed
    column it is the case that can look 95% right and be systematically wrong."""
    spec = _spec("key", "key", "camelot", "camelot", "key_raw", "key_camelot")
    verdict, _agreement, clusters = _run(
        spec,
        # Every row moved from nA to nB, i.e. every key replaced by its relative
        # major. A whole-column convention error.
        _pairings(rb_kw_fn=lambda i: {"key_raw": f"{i % 12 + 1}B"}),
    )
    assert verdict.status == FAILED
    assert clusters[0].signature == "relative_major_minor"
    assert clusters[0].classification == MAPPING_BUG
    assert "RELATIVE MAJOR/MINOR" in clusters[0].interpretation


def test_a_wrong_mapping_confined_to_one_notation_is_a_mapping_bug():
    """The realistic version: only the musical-notation rows are mis-mapped.

    Ten of 300 rows are musical notation and ALL of them are wrong, which is
    only 3.3% of the population -- far below the systematic threshold -- so it
    is the notation-subset coverage test that must catch it.
    """
    # Musical spelling of Camelot nB, i.e. the relative major of nA.
    relative_major = {
        1: "B",
        2: "F#",
        3: "Db",
        4: "Ab",
        5: "Eb",
        6: "Bb",
        7: "F",
        8: "C",
        9: "G",
        10: "D",
    }
    musical = set(range(10))

    def rb_kw(idx: int) -> dict:
        if idx not in musical:
            return {}
        return {"key_raw": relative_major[idx % 12 + 1]}

    spec = _spec(
        "key", "key", "mixed_camelot_musical", "camelot", "key_raw", "key_camelot"
    )
    verdict, agreement, clusters = _run(spec, _pairings(rb_kw_fn=rb_kw))
    assert verdict.status == FAILED, verdict.reasons
    assert clusters[0].classification == MAPPING_BUG
    assert clusters[0].subset_form == "rb=musical|mik=camelot"
    assert clusters[0].subset_coverage == pytest.approx(1.0)
    assert clusters[0].population_share < CFG.systematic_population_share
    assert agreement.form_counts["rb=musical|mik=camelot"] == len(musical)


def test_a_narrow_scattered_pattern_is_a_suspect_not_a_proven_bug():
    """3% of rows a fifth apart: a pattern, but not a unit or convention error.

    Calling this a mapping bug would cry wolf on ordinary analyser
    disagreement; calling it nothing would bless an unexplained pattern. It
    must land as INCONCLUSIVE with the cluster named for adjudication.
    """

    def mik_kw(idx: int) -> dict:
        # Rows 1..10 sit exactly one Camelot step below rekordbox; the rest agree.
        if 1 <= idx <= 10:
            return {"key_camelot": f"{idx}A"}
        return {}

    spec = _spec("key", "key", "camelot", "camelot", "key_raw", "key_camelot")
    verdict, _agreement, clusters = _run(spec, _pairings(mik_kw_fn=mik_kw))
    assert verdict.status == UNTESTED
    assert verdict.suite_status == SUITE_INCONCLUSIVE
    assert [c.classification for c in clusters] == [SUSPECT]
    assert clusters[0].signature == "camelot_hop_+1_fifth_up"
    assert verdict.mapping_bugs == []  # a suspect is NOT reported as proven


def test_diffuse_unclustered_disagreement_does_not_pass():
    """P1 regression (PR #383 review): 300 comparable pairs where EVERY row
    disagrees, but the deltas are spread across 75 distinct additive offsets
    (4 rows each), each individually below CFG.cluster_min_count, so no
    signature ever reaches the cluster threshold and `clusters` comes back
    empty. Passing this would read "we found no repeated bug pattern" as
    "the two sources agree", which SKILL 4b explicitly forbids: absence of a
    named cluster is not the same as absence of disagreement.
    """

    def mik_kw(idx: int) -> dict:
        # rekordbox bpm is left to vary as usual (_centi_bpm cycles over 25
        # values) so the zero-variance guard never fires; the offset added
        # here is what actually drives the delta signature, a distinct
        # additive bucket every 4 rows, comfortably below cluster_min_count
        # and far from any SUSPICIOUS_FACTORS ratio.
        return {"tempo": _centi_bpm(idx) / 100.0 + 2.0 + 0.5 * (idx // 4)}

    spec = _spec("bpm", "bpm", "centi_bpm", "bpm", "bpm_raw", "tempo", tolerance=1.0)
    verdict, agreement, clusters = _run(spec, _pairings(mik_kw_fn=mik_kw))
    assert agreement.rate == 0.0
    assert clusters == []
    assert verdict.status != PASSED, verdict.reasons


# --------------------------------------------- structural non-comparability


def test_a_constant_column_is_untested_not_a_constant_offset_bug():
    """MIK's ZRATING is 0 on every row. Against rekordbox 0-5 that produces a
    perfect 'additive_+3' cluster which is an artefact of the absent data."""
    spec = _spec("rating", "rating", "stars_0_5", "stars_0_5", "rating", "rating")
    verdict, _agreement, clusters = _run(spec, _pairings())
    assert verdict.status == UNTESTED
    assert verdict.suite_status == SUITE_NO_DATA
    assert clusters == []
    assert any("ZERO VARIANCE" in r for r in verdict.reasons), verdict.reasons


def test_an_absent_column_is_untested():
    spec = _spec("energy", "energy", "absent", "energy_1_10", "energy", "energy")
    verdict, _agreement, _clusters = _run(spec, _pairings())
    assert verdict.status == UNTESTED
    assert verdict.suite_status == SUITE_NO_DATA
    assert verdict.normaliser is None


def test_an_unknown_loudness_unit_is_untested_never_guessed():
    spec = _spec(
        "loudness", "loudness", "dbfs", "unknown_db_family", "loudness", "volume"
    )
    verdict, _agreement, _clusters = _run(spec, _pairings())
    assert verdict.status == UNTESTED
    assert verdict.suite_status == SUITE_UNKNOWN_UNIT


def test_cross_family_loudness_is_untested():
    spec = _spec(
        "loudness", "loudness", "dbfs", "lufs_integrated", "loudness", "volume"
    )
    verdict, _agreement, _clusters = _run(spec, _pairings())
    assert verdict.status == UNTESTED
    assert verdict.suite_status == SUITE_UNKNOWN_UNIT
    assert any("family mismatch" in r for r in verdict.reasons), verdict.reasons


# --------------------------------------------------------- thin evidence


def test_too_few_comparable_pairs_is_untested_not_a_passing_rate():
    spec = _spec("bpm", "bpm", "centi_bpm", "bpm", "bpm_raw", "tempo", tolerance=1.0)
    few = [
        Pairing(tier="exact_path", left=_rb(i), right=_mik(i))
        for i in range(CFG.min_comparable - 1)
    ]
    verdict, agreement, _clusters = _run(spec, few)
    assert agreement.rate == 1.0  # a perfect rate...
    assert verdict.status == UNTESTED  # ...over too little data is not evidence


# ------------------------------------------------- key model sanity check


@pytest.mark.parametrize(
    "camelot,expected",
    [("8A", Key(9, "min")), ("8B", Key(0, "maj")), ("1A", Key(8, "min"))],
)
def test_camelot_anchors(camelot: str, expected: Key):
    from apps.equivalence.normalisers import normalise_key

    assert normalise_key(camelot, "camelot") == expected


# --------------------------------------- the collapse finding must stay signal


def _collapse_findings(probe) -> list[str]:
    return [f for f in probe.findings if "collapse to" in f]


def test_a_sentinel_alone_does_not_report_a_second_notation():
    """A documented sentinel is the ABSENCE of a value, not a second spelling.

    Measured against the real stores Tue 28 Jul 2026: comparing canonical
    cardinality against RAW distinct made rekordbox BPM (1266 raw -> 1265
    canonical, sentinel 0.0), MIK ZTEMPO (4202 -> 4201), rekordbox Length
    (561 -> 560) and MIK ZKEY (25 -> 24, sentinel '0') every one report "the
    raw column holds more than one notation", which is false for all four. A
    finding that fires on every column with a sentinel is noise.
    """
    spec = _spec("bpm", "bpm", "centi_bpm", "bpm", "bpm_raw", "tempo", tolerance=1.0)
    rows = [_rb(i) for i in range(N)] + [_rb(N, bpm_raw=0)]  # 0 = not analysed
    probe = probe_field(rows, spec.left)

    assert probe.sentinel_count == 1
    assert probe.mapped_raw_distinct == probe.distinct - 1
    assert _collapse_findings(probe) == []


def test_a_genuine_two_notation_column_still_reports_the_collapse():
    """The signal the previous test must not have suppressed.

    Camelot and musical spellings of the SAME key in one column, plus a
    sentinel. The finding must fire, and must count only the 2 spellings that
    produced a canonical value.
    """
    spec = _spec(
        "key", "key", "mixed_camelot_musical", "camelot", "key_raw", "key_camelot"
    )
    rows = (
        [_rb(0, key_raw="1A") for _ in range(10)]
        + [_rb(0, key_raw="Abm") for _ in range(10)]  # Abm IS 1A
        + [_rb(0, key_raw="All")]  # documented rekordbox sentinel
    )
    probe = probe_field(rows, spec.left)

    assert probe.distinct == 3
    assert probe.mapped_raw_distinct == 2
    assert probe.canonical_distinct == 1
    findings = _collapse_findings(probe)
    assert len(findings) == 1
    assert findings[0].startswith("2 raw spellings collapse to 1 canonical")
    assert "documented sentinel" in findings[0]
