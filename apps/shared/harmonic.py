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
- [if] suggest_next called with real library [then] returns sorted recommendations
"""

from __future__ import annotations

from dataclasses import dataclass

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
        except ValueError:
            raise ValueError(f"Invalid Camelot key: {s!r}")
        return cls(number=number, letter=letter)


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
# Compatibility Scorers
# ---------------------------------------------------------------------------

_CAMELOT_COMPAT: dict[tuple[int, bool | None], float] = {
    (0, True): 1.0,
    (0, False): 0.8,
    (1, True): 0.9,
    (1, False): 0.7,
    (2, True): 0.5,
    (2, False): 0.4,
    (3, None): 0.25,
    (4, None): 0.15,
    (5, None): 0.1,
}


def camelot_compatibility(key1: str, key2: str) -> float:
    """Score harmonic compatibility between two keys (0.0-1.0).

    Accepts both Camelot notation ("8A") and standard notation ("Am").
    """
    ck1 = key_to_camelot(key1)
    ck2 = key_to_camelot(key2)

    dist = _camelot_distance(ck1, ck2)
    same_mode = ck1.letter == ck2.letter
    mode_key = same_mode if dist <= 2 else None
    return _CAMELOT_COMPAT.get((dist, mode_key), 0.05)


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
    score = 1.0 - (normalized ** 2)
    score = max(0.0, score - tempo_penalty)
    return score


def energy_compatibility(rating1: int, rating2: int) -> float:
    """Score energy compatibility via star ratings (0.0-1.0).

    - Same rating = 1.0
    - Difference of N stars = linear falloff
    - Unrated (0) = neutral 0.5
    """
    if rating1 == 0 or rating2 == 0:
        return 0.5

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


def track_compatibility(
    current_key: str | None,
    current_bpm: float,
    current_rating: int,
    candidate_key: str | None,
    candidate_bpm: float,
    candidate_rating: int,
) -> float:
    """Combined compatibility score (0.0-1.0) from key, BPM, and energy.

    Key is weighted highest (0.50) since harmonic clashes are the most noticeable.
    BPM (0.30) matters for beat-matching feasibility.
    Energy (0.20) keeps the flow consistent.
    """
    # Key score
    if current_key and candidate_key:
        try:
            key_score = camelot_compatibility(current_key, candidate_key)
        except ValueError:
            key_score = 0.5  # Unknown key format, neutral
    else:
        key_score = 0.5  # Missing key data, neutral

    # BPM score
    bpm_score = bpm_compatibility(current_bpm, candidate_bpm)

    # Energy score
    eng_score = energy_compatibility(current_rating, candidate_rating)

    return (key_score * WEIGHT_KEY) + (bpm_score * WEIGHT_BPM) + (eng_score * WEIGHT_ENERGY)


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
    """

    stable_id: str
    artist: str | None
    bpm: float | None
    key_camelot: str | None
    energy: int | None
