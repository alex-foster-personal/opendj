"""
Harmonic mixing engine for DJ Copilot.

Requirements:
- [x] R1: Camelot wheel compatibility (same key, +-1, major/minor switch)
- [x] R2: BPM range matching (configurable tolerance, default +-6%)
- [x] R3: Energy matching via star ratings
- [x] R4: Suggest next tracks from library sorted by compatibility score
- [x] R5: Integration test against real djay Pro library data

NL Tests:
- [if] two tracks share the same Camelot key [then] compatibility = 1.0
- [if] Camelot keys differ by +1 or -1 [then] compatibility >= 0.8
- [if] BPM within 3% [then] BPM score >= 0.9
- [if] BPM differs by >10% [then] BPM score = 0.0
- [if] both tracks are 5-star [then] energy score = 1.0
- [if] a key, BPM or energy term cannot be measured [then] it is omitted and weights renormalise (AGENT-33)
- [if] suggest_next called with real library [then] returns sorted recommendations
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime

# ---------------------------------------------------------------------------
# Module-level constants (Phase 13)
# ---------------------------------------------------------------------------
# Tight step-to-step BPM window used by the PLAY IT solver + AI-01 filter.
# The companion bpm_compatibility() keeps its historical 10.0% default so
# the 45 ported tests remain green; Phase 13's higher layers pass this
# tighter window explicitly.
MAX_BPM_DIFF_PCT: float = 6.0

# Camelot step budgets: 0/2 = harmonically compatible, 4 = stretchy mix,
# 6+ = hard cut. Exposed here so the solver + UI share one scale.
CAMELOT_STEP_BUDGET_COMPATIBLE: int = 2
CAMELOT_STEP_BUDGET_STRETCHY: int = 4
CAMELOT_STEP_BUDGET_HARDCUT: int = 6


# ---------------------------------------------------------------------------
# Camelot Key
# ---------------------------------------------------------------------------

# Standard notation -> Camelot mapping (from field_mapping_rekordbox_djay.md)
_KEY_TO_CAMELOT: dict[str, tuple[int, str]] = {
    # Minor keys (A suffix)
    "Abm": (1, "A"),
    "G#m": (1, "A"),
    "Ebm": (2, "A"),
    "D#m": (2, "A"),
    "Bbm": (3, "A"),
    "A#m": (3, "A"),
    "Fm": (4, "A"),
    "Cm": (5, "A"),
    "Gm": (6, "A"),
    "Dm": (7, "A"),
    "Am": (8, "A"),
    "Em": (9, "A"),
    "Bm": (10, "A"),
    "F#m": (11, "A"),
    "Gbm": (11, "A"),
    "Dbm": (12, "A"),
    "C#m": (12, "A"),
    # Major keys (B suffix)
    "B": (1, "B"),
    "Gb": (2, "B"),
    "F#": (2, "B"),
    "Db": (3, "B"),
    "C#": (3, "B"),
    "Ab": (4, "B"),
    "G#": (4, "B"),
    "Eb": (5, "B"),
    "D#": (5, "B"),
    "Bb": (6, "B"),
    "A#": (6, "B"),
    "F": (7, "B"),
    "C": (8, "B"),
    "G": (9, "B"),
    "D": (10, "B"),
    "A": (11, "B"),
    "E": (12, "B"),
}


@dataclass(frozen=True, slots=True)
class CamelotKey:
    """Represents a position on the Camelot wheel (1-12, A or B)."""

    number: int  # 1-12
    letter: str  # "A" (minor) or "B" (major)

    def __post_init__(self) -> None:
        if not (1 <= self.number <= 12):
            raise ValueError(f"Camelot number must be 1-12, got {self.number}")
        if self.letter not in ("A", "B"):
            raise ValueError(f"Camelot letter must be 'A' or 'B', got {self.letter!r}")

    def __str__(self) -> str:
        return f"{self.number}{self.letter}"

    @classmethod
    def from_string(cls, s: str) -> CamelotKey:
        """Parse Camelot notation like '8A', '12B'."""
        s = s.strip()
        if not s:
            raise ValueError("Empty Camelot key string")
        letter = s[-1].upper()
        if letter not in ("A", "B"):
            raise ValueError(f"Invalid Camelot key: {s!r}")
        try:
            number = int(s[:-1])
        except ValueError as error:
            raise ValueError(f"Invalid Camelot key: {s!r}") from error
        return cls(number=number, letter=letter)


def shift_camelot(key: str, semitones: int) -> str:
    """The Camelot key heard after a key shift of ``semitones`` (mode kept): one semitone is 7 steps round the wheel.

    Mirrors the webui's ``effectiveCamelotKey``: 9A shifted +1 is 4A (E minor up to F minor).
    """
    parsed = CamelotKey.from_string(key)
    if semitones == 0:
        return str(parsed)
    return str(CamelotKey(number=(parsed.number - 1 + 7 * semitones) % 12 + 1, letter=parsed.letter))


def key_to_camelot(key_string: str) -> CamelotKey:
    """Convert standard notation (Am, C, F#m, Bb) to CamelotKey.

    Also accepts Camelot notation (8A, 12B) directly.
    """
    if not key_string or not key_string.strip():
        raise ValueError("Empty key string")

    key_string = key_string.strip()

    # Try Camelot notation first (e.g. "8A", "12B")
    if key_string[-1] in ("A", "B") and key_string[:-1].isdigit():
        return CamelotKey.from_string(key_string)

    # Standard notation lookup
    if key_string in _KEY_TO_CAMELOT:
        number, letter = _KEY_TO_CAMELOT[key_string]
        return CamelotKey(number=number, letter=letter)

    raise ValueError(f"Unknown key: {key_string!r}")


def _camelot_distance(k1: CamelotKey, k2: CamelotKey) -> int:
    """Circular distance on the Camelot wheel (0-6)."""
    diff = abs(k1.number - k2.number)
    return min(diff, 12 - diff)


def camelot_distance(k1: CamelotKey, k2: CamelotKey) -> int:
    """Public circular distance on the Camelot wheel (0-6).

    Stable public wrapper over the internal implementation so callers
    outside this module do not depend on a private symbol.
    """
    return _camelot_distance(k1, k2)


# ---------------------------------------------------------------------------
# Camelot mixing rules (the compatible filter's checkboxes)
# ---------------------------------------------------------------------------
# ONE rule list for the server (ui-prefs validation, this module) and the
# browser (player/key/camelot-rules.generated.ts, written from this table by
# scripts/gen_camelot_rules_ts.py; tests/shared/test_camelot_rules.py fails
# when the two differ). Each rule is a set of moves FROM the reference key TO
# a candidate. A move names which reference letter it applies to ("*" = both),
# whether the candidate keeps the letter ("same") or switches it ("other"),
# and the signed Camelot-number step, wrapping 12 <-> 1. Every rule respects
# the A/B letter: no rule matches by number alone.
#
# Definitions follow Mixed In Key, "Use Advanced Harmonic Mixing Techniques"
# (https://mixedinkey.com/Book/Use-Advanced-Harmonic-Mixing-Techniques/) and
# DJ.Studio, "How To DJ using compatible Keys"
# (https://dj.studio/blog/compatible-keys):
# - diagonal: Mixed In Key's diagonal move, B to A one number UP (8B -> 9A)
#   and A to B one number DOWN (8A -> 7B).
# - energy boost / drop: two numbers up / down on the same letter (8A -> 10A
#   / 6A), DJ.Studio's "Energy Boost +2".
# - Jaws: Mixed In Key's -5 move (8A -> 3A), which is the same wheel position
#   as DJ.Studio's +7 (each wheel step is a fifth, so it is one semitone up).
# - pay attention: Mixed In Key's -3 move (8A -> 5A, the same position as +9).
#   Usable but mood-changing, so it is off by default.
#
# Every rule also carries a SCORE and each move a DIRECTION (lift, drop or
# neutral), the scoring table of
# (private path)
# section 3. Scores are Mixed In Key-mimicked priors kept as data (a later
# calibration refits them against the maintainer's blind picks); the row order is the
# claim. The rules are DIRECTIONAL: 5B -> 6A is the good diagonal (0.75) while
# 8B -> 7A, the same wheel distance the other way round, is the wrong-way
# diagonal MIK says fails (0.30). The rows after pay_attention are scoring
# relations, not MIK-named moves; they are off by default in the filter.


@dataclass(frozen=True, slots=True)
class CamelotMove:
    """One reference -> candidate move on the wheel."""

    from_letter: str  # "A", "B" or "*" (either)
    to_letter: str  # "same" or "other"
    step: int  # signed Camelot-number step, applied mod 12
    direction: str  # "lift", "drop" or "neutral": what the move does to the energy
    interval: str  # the tonic interval heard, for reasons ("fifth up")


@dataclass(frozen=True, slots=True)
class CamelotRule:
    id: str
    label: str
    example: str
    default_on: bool
    score: float  # smoothness prior, 0..1 (spec 07 section 3)
    moves: tuple[CamelotMove, ...]


LIFT = "lift"
DROP = "drop"
NEUTRAL = "neutral"
KEY_MOVES: tuple[str, ...] = (LIFT, DROP, NEUTRAL)

#: The relation and score of a pair no rule covers.
CLASH_RELATION = "clash"
CLASH_SCORE = 0.10

#: Relations that read as a key clash on long blends (16+ bars in ``propose._style_fit``).
LONG_BLEND_KEY_CLASH_RELATIONS: frozenset[str] = frozenset(
    {CLASH_RELATION, "tritone", "semitone_down", "wrong_way_diagonal"}
)

CAMELOT_RULES: tuple[CamelotRule, ...] = (
    CamelotRule(
        "same_key", "Same key", "8A \u2192 8A", True, 1.00, (CamelotMove("*", "same", 0, NEUTRAL, "same key"),)
    ),
    CamelotRule(
        "adjacent",
        "\u00b11 same letter",
        "8A \u2192 7A / 9A",
        True,
        0.90,
        (CamelotMove("*", "same", 1, LIFT, "fifth up"), CamelotMove("*", "same", -1, NEUTRAL, "fourth up")),
    ),
    CamelotRule(
        "relative",
        "Relative (A/B)",
        "8A \u2192 8B, 8B \u2192 8A",
        True,
        0.95,
        (CamelotMove("*", "other", 0, NEUTRAL, "relative major/minor"),),
    ),
    CamelotRule(
        "diagonal",
        "Diagonal",
        "8B \u2192 9A, 8A \u2192 7B",
        False,
        0.75,
        (
            CamelotMove("B", "other", 1, NEUTRAL, "mediant, I to iii"),
            CamelotMove("A", "other", -1, NEUTRAL, "submediant, i to VI"),
        ),
    ),
    CamelotRule(
        "energy_boost",
        "Energy boost +2",
        "8A \u2192 10A",
        False,
        0.55,
        (CamelotMove("*", "same", 2, LIFT, "whole tone up"),),
    ),
    CamelotRule(
        "energy_drop",
        "Energy drop \u22122",
        "8A \u2192 6A",
        False,
        0.50,
        (CamelotMove("*", "same", -2, DROP, "whole tone down"),),
    ),
    CamelotRule(
        "jaws", "Jaws \u22125 (= +7)", "8A \u2192 3A", False, 0.30, (CamelotMove("*", "same", -5, LIFT, "semitone up"),)
    ),
    CamelotRule(
        "pay_attention",
        "Pay attention \u22123",
        "8A \u2192 5A",
        False,
        0.45,
        (CamelotMove("*", "same", -3, NEUTRAL, "minor third up"),),
    ),
    CamelotRule(
        "wrong_way_diagonal",
        "Wrong-way diagonal",
        "8B \u2192 7A, 8A \u2192 9B",
        False,
        0.30,
        (
            CamelotMove("B", "other", -1, NEUTRAL, "whole tone up, no shared triad tone"),
            CamelotMove("A", "other", 1, NEUTRAL, "whole tone down, no shared triad tone"),
        ),
    ),
    CamelotRule(
        "parallel",
        "Parallel (same tonic)",
        "8B \u2192 5A, 5A \u2192 8B",
        False,
        0.40,
        (
            CamelotMove("B", "other", -3, NEUTRAL, "same tonic, major to minor"),
            CamelotMove("A", "other", 3, NEUTRAL, "same tonic, minor to major"),
        ),
    ),
    CamelotRule(
        "minor_third_down",
        "Minor third down +3",
        "8A \u2192 11A",
        False,
        0.35,
        (CamelotMove("*", "same", 3, NEUTRAL, "minor third down"),),
    ),
    CamelotRule(
        "major_third",
        "Major third +4",
        "10B \u2192 2B",
        False,
        0.35,
        (CamelotMove("*", "same", 4, LIFT, "major third up"),),
    ),
    CamelotRule(
        "semitone_down",
        "Semitone down +5",
        "8A \u2192 1A",
        False,
        0.15,
        (CamelotMove("*", "same", 5, NEUTRAL, "semitone down"),),
    ),
    CamelotRule(
        "tritone", "Tritone +6", "8A \u2192 2A", False, 0.05, (CamelotMove("*", "same", 6, NEUTRAL, "tritone"),)
    ),
)

CAMELOT_RULE_IDS: tuple[str, ...] = tuple(rule.id for rule in CAMELOT_RULES)
DEFAULT_CAMELOT_RULE_IDS: tuple[str, ...] = tuple(rule.id for rule in CAMELOT_RULES if rule.default_on)
_CAMELOT_RULES_BY_ID: dict[str, CamelotRule] = {rule.id: rule for rule in CAMELOT_RULES}


def _move_matches(move: CamelotMove, reference: CamelotKey, candidate: CamelotKey) -> bool:
    if move.from_letter not in ("*", reference.letter):
        return False
    same_letter = candidate.letter == reference.letter
    if same_letter != (move.to_letter == "same"):
        return False
    return (reference.number - 1 + move.step) % 12 == candidate.number - 1


def camelot_rule_matches(rule_id: str, reference: str, candidate: str) -> bool:
    """True when ``candidate`` is ``rule_id``'s move away from ``reference``.

    Raises ValueError for an unknown rule id or an unparseable key: a filter
    that silently treated either as "no match" would hide the bad input.
    """
    rule = _CAMELOT_RULES_BY_ID.get(rule_id)
    if rule is None:
        raise ValueError(f"unknown Camelot rule {rule_id!r}; known: {', '.join(CAMELOT_RULE_IDS)}")
    ref = key_to_camelot(reference)
    cand = key_to_camelot(candidate)
    return any(_move_matches(move, ref, cand) for move in rule.moves)


def camelot_rules_allow(reference: str, candidate: str, rule_ids: tuple[str, ...] | list[str]) -> bool:
    """True when any enabled rule takes ``reference`` to ``candidate``.

    Every rule id is checked before any is tried, so an unknown one always raises.
    """
    unknown = [rule_id for rule_id in rule_ids if rule_id not in _CAMELOT_RULES_BY_ID]
    if unknown:
        raise ValueError(f"unknown Camelot rule {unknown[0]!r}; known: {', '.join(CAMELOT_RULE_IDS)}")
    return any(camelot_rule_matches(rule_id, reference, candidate) for rule_id in rule_ids)


# ---------------------------------------------------------------------------
# Compatibility Scorers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyCompat:
    """How the incoming key sits against the outgoing one: the Camelot RULE, its score and the move it makes."""

    relation: str  # a CAMELOT_RULES id, or CLASH_RELATION
    score: float  # the rule's prior, 0..1
    move: str  # "lift", "drop" or "neutral"
    interval: str  # the tonic interval heard, for reasons


def key_compat(key_out: str, key_in: str) -> KeyCompat:
    """Score the move FROM ``key_out`` TO ``key_in`` by Camelot rule, directionally.

    Accepts Camelot ("8A") and standard ("Am") notation. The first rule whose
    move takes the outgoing key to the incoming one wins (the cells are
    disjoint, see tests/shared/test_camelot_rules.py); a pair no rule covers
    is a ``clash`` at the clash floor. 5B -> 6A is ``diagonal`` 0.75 while
    8B -> 7A is ``wrong_way_diagonal`` 0.30: the distance table this replaced
    could not tell them apart.
    """
    out_key = key_to_camelot(key_out)
    in_key = key_to_camelot(key_in)
    for rule in CAMELOT_RULES:
        for move in rule.moves:
            if _move_matches(move, out_key, in_key):
                return KeyCompat(rule.id, rule.score, move.direction, move.interval)
    return KeyCompat(CLASH_RELATION, CLASH_SCORE, NEUTRAL, "no Camelot relation")


def camelot_compatibility(key1: str, key2: str) -> float:
    """The ``key_compat`` score of mixing FROM ``key1`` TO ``key2`` (0.0-1.0)."""
    return key_compat(key1, key2).score


def bpm_compatibility(bpm1: float | None, bpm2: float | None, max_diff_pct: float = 10.0) -> float:
    """Score BPM compatibility (0.0-1.0).

    - Identical BPM = 1.0
    - Within max_diff_pct = linear falloff to 0.0
    - Half/double tempo gets a bonus (treated as close match)
    - 0 or None BPM = 0.0
    """
    if bpm1 is None or bpm2 is None or bpm1 <= 0 or bpm2 <= 0:
        return 0.0

    # Check direct ratio
    ratio = max(bpm1, bpm2) / min(bpm1, bpm2)
    direct_diff_pct = (ratio - 1.0) * 100.0

    # Check half/double tempo
    half_double_diff_pct = float("inf")
    for multiplier in (2.0, 0.5):
        adjusted = bpm2 * multiplier
        adj_ratio = max(bpm1, adjusted) / min(bpm1, adjusted)
        adj_diff = (adj_ratio - 1.0) * 100.0
        half_double_diff_pct = min(half_double_diff_pct, adj_diff)

    # Use the better of direct or half/double, with a small penalty for tempo shift
    if half_double_diff_pct < direct_diff_pct:
        effective_diff = half_double_diff_pct
        # Small penalty for needing tempo adjustment
        tempo_penalty = 0.15
    else:
        effective_diff = direct_diff_pct
        tempo_penalty = 0.0

    if effective_diff > max_diff_pct:
        return 0.0

    # Quadratic falloff: gentle near 0%, steep near max
    # 3% diff -> ~0.91, 6% diff -> ~0.64, 10% -> 0.0
    normalized = effective_diff / max_diff_pct
    score = 1.0 - (normalized**2)
    return max(0.0, score - tempo_penalty)


def energy_compatibility(rating1: int | None, rating2: int | None) -> float | None:
    """Score energy compatibility via star ratings (0.0-1.0).

    - Same rating = 1.0
    - Difference of N stars = linear falloff
    - Unrated (0 or None) = unscored, returns None
    """
    if rating1 is None or rating2 is None or rating1 == 0 or rating2 == 0:
        return None

    diff = abs(rating1 - rating2)
    max_diff = 4  # max possible diff between 1-5 star ratings
    return 1.0 - (diff / max_diff) * 0.75


# ---------------------------------------------------------------------------
# Combined Track Compatibility
# ---------------------------------------------------------------------------

# Weights for the combined score
WEIGHT_KEY = 0.50
WEIGHT_BPM = 0.30
WEIGHT_ENERGY = 0.20

_TRACK_TERM_WEIGHTS = {
    "key": WEIGHT_KEY,
    "bpm": WEIGHT_BPM,
    "energy": WEIGHT_ENERGY,
}
TRACK_TERMS = ("key", "bpm", "energy")


def _track_weighted_mean(terms: dict[str, float]) -> float:
    weight = sum(_TRACK_TERM_WEIGHTS[name] for name in terms)
    if weight == 0.0:
        return 0.0
    return sum(_TRACK_TERM_WEIGHTS[name] * value for name, value in terms.items()) / weight


def track_compatibility_terms(
    current_key: str | None,
    current_bpm: float | None,
    current_rating: int | None,
    candidate_key: str | None,
    candidate_bpm: float | None,
    candidate_rating: int | None,
) -> dict[str, float]:
    """Measured key, BPM and energy scores keyed by term name.

    An unmeasurable term is omitted, never filled with a neutral value (AGENT-33).
    """
    terms: dict[str, float] = {}
    if current_key and candidate_key:
        with contextlib.suppress(ValueError):
            terms["key"] = camelot_compatibility(current_key, candidate_key)
    if current_bpm is not None and candidate_bpm is not None and current_bpm > 0 and candidate_bpm > 0:
        terms["bpm"] = bpm_compatibility(current_bpm, candidate_bpm)
    energy_score = energy_compatibility(current_rating, candidate_rating)
    if energy_score is not None:
        terms["energy"] = energy_score
    return terms


def track_compatibility(
    current_key: str | None,
    current_bpm: float | None,
    current_rating: int | None,
    candidate_key: str | None,
    candidate_bpm: float | None,
    candidate_rating: int | None,
) -> float:
    """Combined compatibility score (0.0-1.0) from key, BPM, and energy.

    Missing terms are unscored; the score is the weighted mean of measured
    terms with weights renormalised over those terms (AGENT-33).
    """
    return _track_weighted_mean(
        track_compatibility_terms(
            current_key,
            current_bpm,
            current_rating,
            candidate_key,
            candidate_bpm,
            candidate_rating,
        )
    )


# ---------------------------------------------------------------------------
# Suggest Next Track
# ---------------------------------------------------------------------------


def suggest_next(
    current_key: str | None,
    current_bpm: float,
    current_rating: int,
    library: list[dict],
    top_n: int = 5,
    exclude_titles: list[str] | None = None,
) -> list[dict]:
    """Suggest next tracks from library, sorted by compatibility score.

    Args:
        current_key: Current track's key (Camelot or standard notation)
        current_bpm: Current track's BPM
        current_rating: Current track's star rating (0-5)
        library: List of track dicts with title, artist, key, bpm, rating
        top_n: Max results to return
        exclude_titles: Track titles to exclude (e.g. current track)

    Returns:
        List of track dicts with added "score" field, sorted descending by score.
    """
    if not library:
        return []

    exclude_set = set(exclude_titles) if exclude_titles else set()
    scored: list[dict] = []

    for track in library:
        if track.get("title", "") in exclude_set:
            continue

        score = track_compatibility(
            current_key=current_key,
            current_bpm=current_bpm,
            current_rating=current_rating,
            candidate_key=track.get("key") or None,
            candidate_bpm=track.get("bpm", 0.0) or 0.0,
            candidate_rating=track.get("rating", 0) or 0,
        )

        result = dict(track)
        result["score"] = round(score, 4)
        scored.append(result)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


# ---------------------------------------------------------------------------
# TrackFeature (Phase 13)
# ---------------------------------------------------------------------------
#
# Shared feature struct consumed by the PLAY IT solver (apps/dj_copilot)
# and the AI-01 suggester. Lives here because it's the common input to the
# whole harmonic-math pipeline; the solver + suggester import it rather
# than re-declaring per-module.


@dataclass(slots=True)
class TrackFeature:
    """Compact scoring features for a single track.

    Fields
    ------
    stable_id: open-dj stable id (see apps.shared.state.ids).
    artist:    Primary artist string; used by artist-repeat cooldown. May
               be ``None`` on libraries that have not been enriched.
    bpm:       Tempo in beats-per-minute, post Phase 6 analysis.
    key_camelot: Camelot notation ("8A", "12B"). Use ``key_to_camelot`` to
               parse other notations before constructing.
    energy:    MIK / Phase 6 energy rating on the 1-10 scale.
    title:     Track title; used for recording-identity drops in suggest-next.
    last_played_at: Imported last-played timestamp for recency scoring.
    never_played: When True, history term treats the track as never played.
    """

    stable_id: str
    artist: str | None
    bpm: float | None
    key_camelot: str | None
    energy: int | None
    title: str | None = None
    last_played_at: datetime | None = None
    never_played: bool = False
