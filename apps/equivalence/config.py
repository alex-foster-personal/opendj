"""CFG singleton + the candidate field-pair declarations.

Every decision a reader needs to argue with lives here: which columns are
claimed to be the same field, what unit each side is DECLARED to be in, the
tolerance, and the thresholds that turn a scatter of disagreements into a
"systematic offset cluster".

Declaring the unit is the point. The probe then checks the OBSERVED range
against the plausible range for that kind of field, so a declared unit that
does not match reality is a mechanical failure, not a judgement call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------- taxonomy

# Plausible canonical range per field kind. A source whose declared unit
# converts its observed range OUTSIDE this window is a scale mismatch.
KIND_CANONICAL_RANGE: dict[str, tuple[float, float]] = {
    "bpm": (40.0, 220.0),
    "energy": (1.0, 10.0),
    "loudness": (-70.0, 0.0),
    "duration": (1_000.0, 36_000_000.0),  # ms: 1 s to 10 h
    "rating": (0.0, 5.0),
    "count": (0.0, 1_000_000_000.0),
}
"""Categorical kinds (``key``) are absent on purpose: they have no range."""

KIND_CANONICAL_UNIT: dict[str, str] = {
    "bpm": "bpm",
    "energy": "energy_1_10",
    "loudness": "db",
    "duration": "milliseconds",
    "rating": "stars_0_5",
    "count": "count",
    "key": "pitch_class_and_mode",
}


@dataclass(frozen=True)
class SourceField:
    """One side of a candidate pair: where it lives and what unit it claims."""

    source: str  # 'rekordbox' | 'mik'
    locator: str  # 'djmdContent.BPM', 'ZSONG.ZTEMPO'
    kind: str  # key into KIND_CANONICAL_RANGE / normaliser registry
    unit: str  # declared unit token; must have a registered normaliser
    attr: str  # attribute name on the reader's row dataclass
    note: str = ""


@dataclass(frozen=True)
class FieldPair:
    """A claim that two columns are the same field, plus how to test it."""

    field_name: str  # OUR field name (matches track_fields.field_name)
    left: SourceField
    right: SourceField
    tolerance: float = 0.0  # in the canonical unit; 0.0 means exact equality
    tolerance_note: str = ""
    proxy: bool = False  # True: one side is a DERIVED stand-in, not the field
    proxy_reason: str = ""

    @property
    def pair_id(self) -> str:
        return f"{self.field_name}:{self.left.source}-vs-{self.right.source}"


# ------------------------------------------------- the candidate pairs

_RB_BPM = SourceField(
    source="rekordbox",
    locator="djmdContent.BPM",
    kind="bpm",
    unit="centi_bpm",
    attr="bpm_raw",
    note=(
        "OBSERVED Tue 28 Jul 2026: raw range 0..18000, so the column is "
        "centi-BPM, not BPM. 72 rows are 0 (never analysed) plus 1 NULL. "
        "state.db has already divided by 100 on ingest."
    ),
)
_MIK_BPM = SourceField(
    source="mik",
    locator="ZSONG.ZTEMPO",
    kind="bpm",
    unit="bpm",
    attr="tempo",
    note="OBSERVED: float BPM, range 0.0..172.01. 0.0 is a not-analysed sentinel.",
)

_RB_KEY = SourceField(
    source="rekordbox",
    locator="djmdKey.ScaleName",
    kind="key",
    unit="mixed_camelot_musical",
    attr="key_raw",
    note=(
        "OBSERVED: MIXED notation in one column - 8152 rows Camelot (1A..12B), "
        "129 rows musical (Fm, Gm, F#m, Abm, Bb, C, Fmaj, ...) and 1 literal "
        "'All'. A string-equality comparison against MIK Camelot reports those "
        "129 as disagreements when most of them are the SAME key."
    ),
)
_MIK_KEY = SourceField(
    source="mik",
    locator="ZSONG.ZKEY",
    kind="key",
    unit="camelot",
    attr="key_camelot",
    note="OBSERVED: Camelot 1A..12B, 24 values, plus 3 rows of literal '0'.",
)

_MIK_ENERGY = SourceField(
    source="mik",
    locator="ZSONG.ZENERGY",
    kind="energy",
    unit="energy_1_10",
    attr="energy",
    note=(
        "OBSERVED Tue 28 Jul 2026: float column holding integers, 1.0..9.0 (9 "
        "distinct), no NULLs, present on all 7,026 rows. "
        "ZENERGYSEGMENT.ZENERGY reaches 10.0, so the scalar range is a SUBSET "
        "of the segment range - the declared scale is 1-10. "
        "NO SENTINEL, and that is a measured negative, not an omission: unlike "
        "ZKEY (3 literal '0' rows) and ZTEMPO (1 row of 0.0), ZENERGY has zero "
        "rows outside 1..9. The catch is that MIK song 5021 is provably NOT "
        "analysed (ZTEMPO 0.0, ZKEY '0', zero energy segments) and still "
        "carries ZENERGY 4.0, so on that one row 'energy 4' and 'never "
        "analysed' are indistinguishable from this column alone. 4.0 is a real "
        "value on 431 other rows, so registering it as a sentinel would "
        "destroy real data - detect analysis-absence from ZTEMPO/ZKEY/segment "
        "count instead."
    ),
)

_MIK_LOUDNESS = SourceField(
    source="mik",
    locator="ZSONG.ZVOLUME",
    kind="loudness",
    unit="rms_db",
    attr="volume",
    note=(
        "MEASURED Tue 28 Jul 2026, not declared: full-column range "
        "-31.205..-3.860, 5,378 distinct over 7,026 rows, no NULLs, no zeros, "
        "mean -11.677, sd 2.227, median -12.788. MIK documents no unit, so it "
        "was identified against the AUDIO by scripts/mik_volume_identify.py. "
        "On the 148 sampled tracks whose current bitrate still matches MIK's "
        "own stored ZBITRATE (i.e. the same encode MIK analysed), ZVOLUME "
        "correlates with whole-file RMS dB at r=0.919 (95% CI 0.889..0.941) "
        "against integrated LUFS at r=0.832 (0.775..0.876), and the RMS "
        "residual is the smaller one on 100 of 148 tracks (sign test "
        "p=2.3e-05). On the 18 of those whose file has ALSO not been touched "
        "since MIK analysed it, RMS reaches r=0.9963 with a mean absolute "
        "error of 0.155 dB about a near-constant +1.653 dB offset (sd 0.194, "
        "range +1.335..+1.993), and RMS is closer on 18 of 18. Sample peak "
        "(r=0.076) and true peak (r=0.026) COLLAPSE on that subset, so the "
        "peak family is ruled out, not merely outranked. "
        "CAVEAT the offset is real: MIK's RMS window is not ffmpeg's "
        "whole-file window, so ZVOLUME must not be treated as interchangeable "
        "with a recomputed RMS at better than about 1 dB, and no correction "
        "constant is applied here because inventing one from n=148 would be "
        "fabricating precision."
    ),
)

_RB_DURATION = SourceField(
    source="rekordbox",
    locator="djmdContent.Length",
    kind="duration",
    unit="seconds",
    attr="length_raw",
    note="OBSERVED integer seconds, 0..8264. 11 rows are 0 plus 1 NULL.",
)
_MIK_DURATION = SourceField(
    source="mik",
    locator="MAX(ZENERGYSEGMENT.ZSTARTTIME + ZLENGTH)",
    kind="duration",
    unit="seconds",
    attr="analysed_span_s",
    note=(
        "MIK has NO duration column. This is the analysed span of the energy "
        "segments, which is a DERIVED proxy: it excludes nothing explicitly "
        "and its first segment can start at -0.019 s. Not a container length "
        "and not a decoded length."
    ),
)

_RB_RATING = SourceField(
    source="rekordbox",
    locator="djmdContent.Rating",
    kind="rating",
    unit="stars_0_5",
    attr="rating",
    note=(
        "OBSERVED 0..5, six distinct values. NOT the 0/51/102/153/204/255 "
        "form the skill checklist warns about - that is the ID3 POPM byte, "
        "which rekordbox writes to FILES, not to djmdContent."
    ),
)
_MIK_RATING = SourceField(
    source="mik",
    locator="ZSONG.ZRATING",
    kind="rating",
    unit="stars_0_5",
    attr="rating",
    note="OBSERVED: 0 for all 7026 rows. The column is unused, so there is no data.",
)


FIELD_PAIRS: tuple[FieldPair, ...] = (
    FieldPair(
        field_name="key",
        left=_RB_KEY,
        right=_MIK_KEY,
        tolerance_note="categorical: equality on (pitch class, mode)",
    ),
    FieldPair(
        field_name="bpm",
        left=_RB_BPM,
        right=_MIK_BPM,
        tolerance=1.0,
        tolerance_note=(
            "1.0 BPM, matching the MIK-AUDIT 'agree within 1.0' figure so the "
            "two numbers are comparable"
        ),
    ),
    # NOTE, Tue 28 Jul 2026: ``energy`` and ``loudness`` used to be declared
    # here as cross-source pairs with an ``absent`` left side, so both reported
    # ``untested_no_data`` forever while the energy SERIES - equally MIK-only -
    # passed on the single-source basis. Same field, same source, two different
    # verdicts, purely because of where each was declared. Both are now
    # SingleSourceFields below. ``loudness`` also needed its unit MEASURED
    # first: an undecidable unit is untestable whichever list it sits in.
    FieldPair(
        field_name="duration",
        left=_RB_DURATION,
        right=_MIK_DURATION,
        tolerance=2_000.0,
        tolerance_note="2 s, absorbing integer-second rounding on the rekordbox side",
        proxy=True,
        proxy_reason=(
            "MIK has no duration field. The right-hand side is a derived "
            "analysed span, so this pair can only ever be a DIAGNOSTIC (it "
            "catches x1000 and matching errors); it can never be 'passed'."
        ),
    ),
    FieldPair(field_name="rating", left=_RB_RATING, right=_MIK_RATING),
)


# ------------------------------------------- source-unique (single-source)


@dataclass(frozen=True)
class SingleSourceField:
    """A field only ONE source has, so no agreement rate can exist.

    Declared separately from :class:`FieldPair` on purpose: the evidence for
    these is the probe, the normaliser's totality and (for a series) the
    structural invariants. Never an agreement rate, because there is nothing
    to agree WITH. See :mod:`apps.equivalence.single_source`.
    """

    field_name: str
    source: SourceField
    shape: str  # 'scalar' | 'time_series'
    why_unique: str
    value_range: tuple[float, float] | None = None  # series values, if a series
    companion_series_field: str | None = None
    """Set on a SCALAR that has a time series of the same quantity in the same
    source. It buys the one cross-check a source-unique scalar can have: the
    scalar must lie inside its OWN series' observed range. That is exactly the
    check that would have caught DJ.Studio's ``energyLevelNr``, an INDEX into
    the segment array masquerading as an energy value (SKILL 4b). Without a
    second source it is also the only way to notice a future column swap."""


SINGLE_SOURCE_FIELDS: tuple[SingleSourceField, ...] = (
    SingleSourceField(
        field_name="energy",
        source=_MIK_ENERGY,
        shape="scalar",
        why_unique=(
            "rekordbox has NO energy column, MEASURED: `djmdContent` carries no "
            "such field, and `Commnt` holds an 'Energy N' pattern for only 163 "
            "of 10,178 rows - and those came FROM MIK's own ID3 write-back, so "
            "counting them as a second opinion would be comparing MIK with "
            "itself. No agreement rate can exist, which is why declaring this "
            "as a cross-source pair could only ever yield untested_no_data."
        ),
        value_range=(1.0, 10.0),
        companion_series_field="energy_segments",
    ),
    SingleSourceField(
        field_name="loudness",
        source=_MIK_LOUDNESS,
        shape="scalar",
        why_unique=(
            "rekordbox stores no loudness scalar in djmdContent, MEASURED: no "
            "such column exists, so no agreement rate can exist either. The "
            "field was blocked twice over until Tue 28 Jul 2026 - once because "
            "it was declared as a pair against an absent column, and once "
            "because its unit was `unknown_db_family` and an undecidable unit "
            "is untestable by construction. The unit is now MEASURED against "
            "the audio (see the note on the source field), so only the first "
            "reason had to be fixed by moving it here."
        ),
    ),
    SingleSourceField(
        field_name="energy_segments",
        source=SourceField(
            source="mik",
            locator="ZENERGYSEGMENT (ZSTARTTIME, ZLENGTH, ZENERGY)",
            kind="energy",
            unit="energy_1_10",
            attr="energy",
            note=(
                "OBSERVED Tue 28 Jul 2026: 58,948 segments over 7,019 songs, "
                "energy 1.0..10.0 (10 distinct), 81 segments start slightly "
                "before 0 s (minimum -0.019489 s), 0 segments have a length "
                "<= 0. Time-indexed in seconds, so no BPM dependency."
            ),
        ),
        shape="time_series",
        why_unique=(
            "The energy TIME SERIES exists nowhere else in the toolchain. "
            "rekordbox holds no energy at all; MIK's own ID3 write-back is "
            "scalar only ('3A - Energy 5'), so the series cannot be recovered "
            "from file tags. If it does not land, it does not exist for us."
        ),
        value_range=(1.0, 10.0),
    ),
    SingleSourceField(
        field_name="clipped_peak_count",
        source=SourceField(
            source="mik",
            locator="ZSONG.ZCLIPPEDPEAKCOUNT",
            kind="count",
            unit="count",
            attr="clipped_peak_count",
            note=(
                "OBSERVED: integer 0..88,680 over 7,026 rows, 740 distinct, no "
                "NULLs. 2,379 rows are non-zero, i.e. carry clipping; 4,647 are "
                "exactly 0. Zero here is a real measurement ('no clipped peaks "
                "found'), NOT a not-analysed sentinel, which is why 0 is not "
                "registered as a sentinel for this unit."
            ),
        ),
        shape="scalar",
        why_unique=(
            "rekordbox stores no clipping metric. Free QC data: a high count "
            "flags a track worth re-mastering or re-sourcing."
        ),
    ),
)


# ------------------------------------------------------------------ CFG


@dataclass
class _Cfg:
    """Single place for every tunable. CLI args override these."""

    # Paths (resolved against --data-dir at runtime).
    data_dir: Path = Path("data")
    rekordbox_db: Path = Path("data/master.plain.db")
    mik_db: Path = (
        Path.home() / "Library/Application Support/Mixedinkey/Collection10.mikdb"
    )

    # Outputs.
    verdicts_name: str = "equivalence-verdicts.json"
    report_name: str = "equivalence-report.md"
    disagreement_dir_name: str = "equivalence-disagreements"

    # Matching.
    allow_fuzzy_tiers: bool = True
    strip_mik_energy_prefix: bool = True

    # Agreement / clustering thresholds.
    min_comparable: int = 100
    """Below this many comparable pairs a verdict stays UNTESTED: a rate over a
    handful of rows is not evidence."""
    cluster_min_count: int = 5
    cluster_min_share: float = 0.20
    """A delta signature holding >= 20% of the disagreements AND >= 5 rows is a
    SYSTEMATIC OFFSET worth naming."""
    systematic_population_share: float = 0.25
    """Share of the COMPARABLE population at which a cluster is a proven mapping
    bug. A unit or convention error is near-universal by construction: a x100
    column read as base units is wrong on every row. Measuring share against the
    disagreements alone cannot tell "our mapping is wrong" from "this is simply
    the commonest way two analysers differ", which is why both are computed."""
    subset_coverage_share: float = 0.90
    """A cluster covering >= 90% of the rows of ONE identifiable raw notation is
    also a proven mapping bug, however small that subset is: that is what a
    mis-read notation looks like (e.g. every musical-notation row wrong while
    every Camelot row is right)."""
    suspect_population_share: float = 0.005
    """Below the systematic threshold but above this, a named cluster is a
    SUSPECT: not proven to be our bug, not dismissible either. The verdict is
    INCONCLUSIVE and the named cluster is what the known-answer harness must
    adjudicate. Per SKILL 4b a low or patterned disagreement is AMBIGUOUS, and
    the numbers alone cannot resolve it."""
    ratio_tolerance: float = 0.01
    """Relative tolerance when testing a disagreement for an exact 2x/100x ratio."""
    additive_bucket: float = 0.5
    """Rounding bucket, in canonical units, for constant-additive-offset clustering."""

    # Probe.
    histogram_max_buckets: int = 24
    numeric_histogram_bins: int = 12

    fields: tuple[FieldPair, ...] = field(default_factory=lambda: FIELD_PAIRS)

    def resolve(self, data_dir: Path) -> None:
        """Re-root the data-dependent paths onto ``data_dir`` (fail fast later)."""
        self.data_dir = data_dir
        self.rekordbox_db = data_dir / "master.plain.db"

    @property
    def verdicts_path(self) -> Path:
        return self.data_dir / "state" / self.verdicts_name

    @property
    def report_path(self) -> Path:
        return self.data_dir / "state" / self.report_name

    @property
    def disagreement_dir(self) -> Path:
        return self.data_dir / "state" / self.disagreement_dir_name


CFG = _Cfg()

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "known_answers.json"
FIXTURE_MANIFEST_PATH = FIXTURE_PATH.with_suffix(".manifest.json")
"""The locked ``{schema, sha256}`` declaration :func:`apps.equivalence.known.
load_fixture` checks the release-gating fixture against. Regenerate with
``python -m apps.equivalence.known --write-manifest`` after a REVIEWED
change to ``known_answers.json``, never as a convenience workaround for a
checksum mismatch."""


__all__ = [
    "CFG",
    "FIELD_PAIRS",
    "FIXTURE_MANIFEST_PATH",
    "FIXTURE_PATH",
    "KIND_CANONICAL_RANGE",
    "KIND_CANONICAL_UNIT",
    "SINGLE_SOURCE_FIELDS",
    "FieldPair",
    "SingleSourceField",
    "SourceField",
]
