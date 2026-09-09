"""TOTAL normalisers: every source value maps to a defined target or RAISES.

The bug this module exists to prevent is a silent pass-through of an unmapped
value (SKILL 4b step 5). So there is no ``else: return value`` anywhere, and
no bare ``except``.

Three outcomes only, and they are distinct on purpose:

* a canonical value      -- the value is understood
* :data:`MISSING`        -- the source uses a DOCUMENTED sentinel for "no
  value". Measured sentinels: rekordbox ``BPM`` 0 (72 rows), rekordbox
  ``ScaleName`` ``'All'`` (1 row), MIK ``ZTEMPO`` 0.0, MIK ``ZKEY`` ``'0'``
  (3 rows), rekordbox ``Length`` 0 (11 rows). A sentinel NEVER becomes a real
  key or tempo. An UNdocumented odd value raises.
* :class:`NormaliseError` -- the value is not understood. Never swallowed.

``None`` in means ``MISSING`` out for every normaliser: a NULL column is an
absent value, not an unmapped one.

Key mapping is DELEGATED to ``apps.shared.harmonic.key_to_camelot``, which
already covers Camelot and musical notation including enharmonics and raises
on anything unknown. A second key table would silently diverge from it. What
lives here is a TOTAL pre-pass in front of it for the three things it does
not cover: open-key notation, the ``maj``/``min`` word suffixes (``Fmaj``),
and sentinels.

Mini-PRD:

* R1 no silent pass-through: every path returns canonical, MISSING, or
  raises. ok+ran+tests
  - [if] a value matches no rule [then] NormaliseError names the value
  - [if] a value is a declared sentinel [then] MISSING, never a real value
  - [if] a normaliser is fuzzed over junk [then] no input returns unchanged
* R2 out-of-range values raise instead of passing through. ok+ran+tests
  - [if] energy 17 is offered on a 1-10 scale [then] raise (this is the
    DJ.Studio ``energyLevelNr`` index-masquerading-as-energy trap)
  - [if] rating 9 is offered on a 0-5 scale [then] raise
  - [if] bpm 18000 is offered as ``bpm`` [then] raise
* R3 two loudness family members are never silently interchanged. ok+ran+tests
  - [if] dBFS is compared with LUFS [then] the pair is not comparable
  - [if] the unit is unknown [then] UNTESTED, never a guessed family
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from apps.shared.harmonic import CamelotKey, key_to_camelot


# Sentinel for "the source says it has no value here".
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()


class NormaliseError(ValueError):
    """A source value that no rule covers. Fail fast; never pass it through."""

    def __init__(self, kind: str, unit: str, value: object, reason: str) -> None:
        super().__init__(
            f"{kind}/{unit}: cannot normalise {value!r} ({reason}). A silent "
            f"pass-through here is the exact bug SKILL 4b step 5 forbids."
        )
        self.kind = kind
        self.unit = unit
        self.value = value
        self.reason = reason


# --------------------------------------------------------------- key model

MODE_MINOR: Final = "min"
MODE_MAJOR: Final = "maj"

# Camelot 1A is A-flat minor (pitch class 8); each +1 is a perfect fifth.
_CAMELOT_1A_PC: Final = 8
_CAMELOT_1B_PC: Final = 11
_FIFTH: Final = 7
# Open Key 1m is A minor, which is Camelot 8A: a constant wheel offset of 7.
_OPEN_KEY_TO_CAMELOT_OFFSET: Final = 7


@dataclass(frozen=True, order=True)
class Key:
    """Canonical key: pitch class 0-11 (C=0) plus mode. Notation-free."""

    pitch_class: int
    mode: str

    def __post_init__(self) -> None:
        if not 0 <= self.pitch_class <= 11:
            raise ValueError(f"pitch_class out of range: {self.pitch_class}")
        if self.mode not in (MODE_MINOR, MODE_MAJOR):
            raise ValueError(f"mode must be {MODE_MINOR!r} or {MODE_MAJOR!r}")

    @classmethod
    def from_camelot(cls, camelot: CamelotKey) -> Key:
        if camelot.letter == "A":
            pitch = (_CAMELOT_1A_PC + _FIFTH * (camelot.number - 1)) % 12
            return cls(pitch, MODE_MINOR)
        pitch = (_CAMELOT_1B_PC + _FIFTH * (camelot.number - 1)) % 12
        return cls(pitch, MODE_MAJOR)

    @property
    def camelot(self) -> str:
        base = _CAMELOT_1A_PC if self.mode == MODE_MINOR else _CAMELOT_1B_PC
        letter = "A" if self.mode == MODE_MINOR else "B"
        # Invert pc = (base + 7*(n-1)) % 12. 7 is its own inverse mod 12.
        number = ((self.pitch_class - base) * _FIFTH) % 12 + 1
        return f"{number}{letter}"

    @property
    def camelot_number(self) -> int:
        return int(self.camelot[:-1])

    @property
    def open_key(self) -> str:
        # Reverse of the open_key pre-pass rule above. No caller needed
        # this direction before apps/analysis_key/canon.py.
        letter = "m" if self.mode == MODE_MINOR else "d"
        number = ((self.camelot_number - 1 - _OPEN_KEY_TO_CAMELOT_OFFSET) % 12) + 1
        return f"{number}{letter}"

    @property
    def relative(self) -> Key:
        """The relative major of a minor key, or relative minor of a major.

        Relative major/minor is the single easiest way to look 95% right and
        be systematically wrong, so the comparator needs this explicitly.
        """
        if self.mode == MODE_MINOR:
            return Key((self.pitch_class + 3) % 12, MODE_MAJOR)
        return Key((self.pitch_class - 3) % 12, MODE_MINOR)


# ------------------------------------------------------- key pre-pass

# Notation tokens. Case matters: open key uses lower-case 'd'/'m', and a bare
# upper-case 'M' is the conventional major marker, so the whole string is
# never blindly upper-cased.
_CAMELOT_RE: Final = re.compile(r"^(1[0-2]|[1-9])\s*([AaBb])$")
_OPEN_KEY_RE: Final = re.compile(r"^(1[0-2]|[1-9])\s*([dm])$")
_MUSICAL_RE: Final = re.compile(
    r"^([A-Ga-g])\s*([#b])?\s*(m|min|minor|maj|major|M|Min|Maj|Minor|Major)?$"
)

_MINOR_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"m", "min", "minor", "Min", "Minor"}
)
_MAJOR_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"", "maj", "major", "M", "Maj", "Major"}
)

# Sentinels MEASURED in the real columns, per notation. Deliberately short:
# an invented sentinel would turn a genuinely unmapped value into MISSING and
# hide exactly what this module is for.
KEY_SENTINELS: Final[dict[str, frozenset[str]]] = {
    # MIK ZSONG.ZKEY carries the literal '0' on 3 rows (OBSERVED).
    "camelot": frozenset({"", "0"}),
    "open_key": frozenset({""}),
    # djmdKey.ScaleName carries the literal 'All' on 1 row (OBSERVED).
    "musical": frozenset({"", "All"}),
    "mixed_camelot_musical": frozenset({"", "0", "All"}),
    "pitch_class_and_mode": frozenset({""}),
}

KEY_UNITS: Final[tuple[str, ...]] = tuple(sorted(KEY_SENTINELS))


def _clean(value: Any) -> str:
    """NFC, trimmed, with unicode accidentals folded to ASCII."""
    text = unicodedata.normalize("NFC", str(value)).strip()
    return text.replace("♭", "b").replace("♯", "#")


def _prepass_to_harmonic_spelling(text: str, unit: str, raw: Any) -> str:
    """Rewrite ``text`` into a spelling ``key_to_camelot`` accepts, or raise.

    This is the TOTAL pre-pass. It never returns the input unchanged unless
    the input is already a spelling the delegate accepts.
    """
    camelot = _CAMELOT_RE.match(text)
    if camelot and unit in ("camelot", "mixed_camelot_musical"):
        return f"{int(camelot.group(1))}{camelot.group(2).upper()}"

    if unit == "open_key":
        open_key = _OPEN_KEY_RE.match(text)
        if not open_key:
            raise NormaliseError(
                "key", unit, raw, "not open-key notation (expected 1d..12m)"
            )
        number = (int(open_key.group(1)) - 1 + _OPEN_KEY_TO_CAMELOT_OFFSET) % 12 + 1
        return f"{number}{'A' if open_key.group(2) == 'm' else 'B'}"

    if unit in ("musical", "mixed_camelot_musical"):
        musical = _MUSICAL_RE.match(text)
        if musical:
            note = musical.group(1).upper()
            accidental = (musical.group(2) or "").lower()
            suffix = musical.group(3) or ""
            if suffix in _MINOR_SUFFIXES:
                mode_suffix = "m"
            elif suffix in _MAJOR_SUFFIXES:
                mode_suffix = ""
            else:  # unreachable while the regex and the sets agree
                raise NormaliseError(
                    "key", unit, raw, f"unrecognised mode suffix {suffix!r}"
                )
            return f"{note}{accidental}{mode_suffix}"

    raise NormaliseError("key", unit, raw, f"no {unit} rule matches {text!r}")


def normalise_key(value: Any, unit: str) -> Key | _Missing:
    """Canonicalise a key in notation ``unit``. Raises on anything unmapped.

    Delegates the actual note-to-wheel mapping to
    ``apps.shared.harmonic.key_to_camelot`` so there is exactly one key table
    in the repo.
    """
    if unit not in KEY_SENTINELS:
        raise NormaliseError("key", unit, value, "no normaliser registered for unit")
    if value is None:
        return MISSING
    if unit == "pitch_class_and_mode":
        return _normalise_pitch_class_pair(value)
    text = _clean(value)
    if text in KEY_SENTINELS[unit]:
        return MISSING

    spelling = _prepass_to_harmonic_spelling(text, unit, value)
    try:
        return Key.from_camelot(key_to_camelot(spelling))
    except ValueError as exc:
        raise NormaliseError(
            "key",
            unit,
            value,
            f"apps.shared.harmonic rejected the pre-passed spelling "
            f"{spelling!r}: {exc}",
        ) from exc


def _normalise_pitch_class_pair(value: Any) -> Key | _Missing:
    if isinstance(value, Key):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        pitch, mode = value
        try:
            return Key(int(pitch), str(mode))
        except (TypeError, ValueError) as exc:
            raise NormaliseError(
                "key", "pitch_class_and_mode", value, str(exc)
            ) from exc
    if (
        isinstance(value, str)
        and _clean(value) in KEY_SENTINELS["pitch_class_and_mode"]
    ):
        return MISSING
    raise NormaliseError(
        "key", "pitch_class_and_mode", value, "expected Key or (pitch_class, mode)"
    )


# ----------------------------------------------------------- numeric units

# unit -> multiplier onto the canonical unit for that kind.
NUMERIC_UNITS: Final[dict[str, dict[str, float]]] = {
    "bpm": {"bpm": 1.0, "centi_bpm": 0.01, "milli_bpm": 0.001},
    "energy": {"energy_1_10": 1.0, "energy_0_4": 1.0},
    "loudness": {
        "dbfs": 1.0,
        "rms_db": 1.0,
        "lufs_integrated": 1.0,
        "lufs_short_term": 1.0,
    },
    "duration": {
        "milliseconds": 1.0,
        "seconds": 1_000.0,
        "samples_44100": 1_000.0 / 44_100.0,
        "samples_48000": 1_000.0 / 48_000.0,
    },
    "rating": {"stars_0_5": 1.0},
    # A plain event count. Registered so a source-unique count field still
    # goes through a TOTAL normaliser rather than being trusted raw.
    "count": {"count": 1.0},
    "time": {
        "milliseconds": 1.0,
        "seconds": 1_000.0,
        "samples_44100": 1_000.0 / 44_100.0,
        "samples_48000": 1_000.0 / 48_000.0,
    },
}

# A loudness number is only comparable to another number of the SAME family.
# dBFS peak, RMS dB, LUFS integrated and LUFS short-term are four different
# measurements wearing one word, and all four have factor 1.0 above precisely
# because no multiplier can reconcile them. The comparator must therefore
# refuse a cross-family pair rather than scale it.
LOUDNESS_FAMILIES: Final[frozenset[str]] = frozenset(NUMERIC_UNITS["loudness"])

# Per-unit legal input window, in the unit's OWN scale, checked before
# scaling. Without this an index value (DJ.Studio's energyLevelNr reaches 17)
# passes straight through as an "energy", which is the trap SKILL 4b names.
UNIT_INPUT_RANGE: Final[dict[tuple[str, str], tuple[float, float]]] = {
    ("bpm", "bpm"): (20.0, 400.0),
    ("bpm", "centi_bpm"): (2_000.0, 40_000.0),
    ("bpm", "milli_bpm"): (20_000.0, 400_000.0),
    ("energy", "energy_1_10"): (1.0, 10.0),
    ("energy", "energy_0_4"): (0.0, 4.0),
    ("rating", "stars_0_5"): (0.0, 5.0),
    ("loudness", "dbfs"): (-120.0, 0.0),
    ("loudness", "rms_db"): (-120.0, 0.0),
    ("loudness", "lufs_integrated"): (-70.0, 10.0),
    ("loudness", "lufs_short_term"): (-70.0, 10.0),
    ("count", "count"): (0.0, 1_000_000_000.0),
    ("duration", "seconds"): (0.0, 36_000.0),
    ("duration", "milliseconds"): (0.0, 36_000_000.0),
}

# Units whose family is knowable but whose MEMBER is not.
UNDECIDABLE_UNITS: Final[frozenset[str]] = frozenset({"unknown_db_family", "unknown"})

# Units meaning "this source has no such column". Not an error, not a value.
ABSENT_UNITS: Final[frozenset[str]] = frozenset({"absent"})

# Documented numeric sentinels per (kind, unit): a stored "no value".
NUMERIC_SENTINELS: Final[dict[tuple[str, str], frozenset[float]]] = {
    ("bpm", "bpm"): frozenset({0.0}),
    ("bpm", "centi_bpm"): frozenset({0.0}),
    ("bpm", "milli_bpm"): frozenset({0.0}),
    ("duration", "seconds"): frozenset({0.0}),
    ("duration", "milliseconds"): frozenset({0.0}),
}

# rekordbox writes 0/51/102/153/204/255 into the ID3 POPM byte. Kept as a
# real, testable unit even though djmdContent.Rating itself stores 0-5.
_POPM_STEPS: Final[dict[int, int]] = {0: 0, 51: 1, 102: 2, 153: 3, 204: 4, 255: 5}


def normalise_rating_popm(value: Any) -> float | _Missing:
    """ID3 POPM byte -> stars 0-5. Only the six legal steps are accepted."""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        raise NormaliseError(
            "rating", "rekordbox_popm_0_255", value, "bool is not a rating"
        )
    try:
        as_int = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        # OverflowError is int(inf). Found by the totality fuzz test, which is
        # the whole reason it exists: without this the escaping exception is an
        # OverflowError, not a NormaliseError, so a caller that handles only
        # NormaliseError crashes instead of recording an unmapped value.
        raise NormaliseError(
            "rating", "rekordbox_popm_0_255", value, "not an integer"
        ) from exc
    if as_int not in _POPM_STEPS:
        raise NormaliseError(
            "rating",
            "rekordbox_popm_0_255",
            value,
            "not one of the 0/51/102/153/204/255 steps; a linear 0-255 read "
            "would silently invent half-stars",
        )
    return float(_POPM_STEPS[as_int])


def normalise_rating_linear_255(value: Any) -> float | _Missing:
    """A genuinely linear 0-255 rating -> stars 0-5."""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        raise NormaliseError("rating", "linear_0_255", value, "bool is not a rating")
    try:
        as_num = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise NormaliseError("rating", "linear_0_255", value, "not a number") from exc
    if math.isnan(as_num):
        raise NormaliseError("rating", "linear_0_255", value, "NaN is not a rating")
    if not 0.0 <= as_num <= 255.0:
        raise NormaliseError("rating", "linear_0_255", value, "outside 0..255")
    return as_num * 5.0 / 255.0


SPECIAL_NUMERIC: Final[dict[tuple[str, str], Callable[[Any], Any]]] = {
    ("rating", "rekordbox_popm_0_255"): normalise_rating_popm,
    ("rating", "linear_0_255"): normalise_rating_linear_255,
}


def _numeric_scale_factor(kind: str, unit: str, value: Any) -> float:
    """Resolves the scale factor for (kind, unit), split out of
    ``normalise_numeric`` to keep it under the complexity ceiling."""
    if unit in ABSENT_UNITS:
        raise NormaliseError(
            kind, unit, value, "the source has no such column; nothing to normalise"
        )
    if unit in UNDECIDABLE_UNITS:
        raise NormaliseError(
            kind,
            unit,
            value,
            "the unit is UNKNOWN. dBFS vs RMS vs LUFS integrated vs LUFS "
            "short-term are four different numbers wearing one word, so the "
            "correct outcome is UNTESTED, not a guess",
        )
    scales = NUMERIC_UNITS.get(kind)
    if scales is None:
        raise NormaliseError(kind, unit, value, "unknown field kind")
    factor = scales.get(unit)
    if factor is None:
        raise NormaliseError(kind, unit, value, "no normaliser registered for unit")
    return factor


def _validated_numeric_value(kind: str, unit: str, value: Any) -> float | _Missing:
    """Validates ``value`` is a sane measurement, split out of
    ``normalise_numeric`` to keep it under the complexity ceiling. Returns
    the value AS A FLOAT, un-scaled; the caller applies the unit factor."""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        raise NormaliseError(kind, unit, value, "bool is not a measurement")
    try:
        as_float = float(value)
    except (TypeError, ValueError) as exc:
        raise NormaliseError(kind, unit, value, "not a number") from exc
    if not math.isfinite(as_float):
        raise NormaliseError(kind, unit, value, "NaN / infinity is not a measurement")
    if as_float in NUMERIC_SENTINELS.get((kind, unit), frozenset()):
        return MISSING
    window = UNIT_INPUT_RANGE.get((kind, unit))
    if window is not None and not window[0] <= as_float <= window[1]:
        raise NormaliseError(
            kind,
            unit,
            value,
            f"outside the legal {window[0]:g}..{window[1]:g} window for unit "
            f"{unit!r}. Passing it through is how an INDEX gets stored as a "
            f"measurement (DJ.Studio energyLevelNr reaches 17 on a 1-10 scale)",
        )
    return as_float


def normalise_numeric(value: Any, kind: str, unit: str) -> float | _Missing:
    """Scale ``value`` from ``unit`` into the canonical unit for ``kind``."""
    special = SPECIAL_NUMERIC.get((kind, unit))
    if special is not None:
        return special(value)
    factor = _numeric_scale_factor(kind, unit, value)
    validated = _validated_numeric_value(kind, unit, value)
    if isinstance(validated, _Missing):
        return MISSING
    return validated * factor


def normalise_time(
    value: Any, unit: str, *, bpm: float | None = None
) -> float | _Missing:
    """A time base -> milliseconds. ``beats`` needs a positive BPM or raises."""
    if unit == "beats":
        if value is None:
            return MISSING
        if bpm is None or bpm <= 0:
            raise NormaliseError(
                "time",
                unit,
                value,
                "beat-indexed time cannot be converted without a positive BPM; "
                "converting anyway is how beat-indexed sources silently drift",
            )
        if isinstance(value, bool):
            raise NormaliseError("time", unit, value, "bool is not a beat index")
        try:
            beats = float(value)
        except (TypeError, ValueError) as exc:
            raise NormaliseError("time", unit, value, "not a number") from exc
        return beats * 60_000.0 / bpm
    return normalise_numeric(value, "time", unit)


# ---------------------------------------------------------------- registry

KIND_CANONICAL: Final[dict[str, str]] = {
    "key": "pitch_class_and_mode",
    "bpm": "bpm",
    "energy": "energy_1_10",
    "loudness": "db_same_family_only",
    "duration": "milliseconds",
    "rating": "stars_0_5",
    "count": "count",
    "time": "milliseconds",
}


def normalise(value: Any, kind: str, unit: str) -> Any:
    """Front door. Dispatches on ``kind``; raises for an unknown kind."""
    if kind == "key":
        return normalise_key(value, unit)
    if kind == "time":
        return normalise_time(value, unit)
    if kind in NUMERIC_UNITS:
        return normalise_numeric(value, kind, unit)
    raise NormaliseError(kind, unit, value, "unknown field kind")


def normaliser_name(kind: str, unit: str) -> str:
    """Stable identifier recorded in the verdict file (apps.shared.equivalence)."""
    return f"{kind}:{unit}->{KIND_CANONICAL[kind]}"


def is_comparable_unit(kind: str, unit: str) -> bool:
    """False when the unit itself blocks comparison (absent or undecidable)."""
    if unit in ABSENT_UNITS or unit in UNDECIDABLE_UNITS:
        return False
    if kind == "key":
        return unit in KEY_SENTINELS
    if kind == "time":
        return unit == "beats" or unit in NUMERIC_UNITS["time"]
    if (kind, unit) in SPECIAL_NUMERIC:
        return True
    return unit in NUMERIC_UNITS.get(kind, {})


def units_incompatible(kind: str, left_unit: str, right_unit: str) -> str | None:
    """Why these two declared units cannot be compared, or None if they can.

    The loudness case is the one that matters: a multiplier cannot turn dBFS
    into LUFS, so a cross-family pair is refused rather than scaled.
    """
    if (
        kind == "loudness"
        and left_unit != right_unit
        and left_unit in LOUDNESS_FAMILIES
        and right_unit in LOUDNESS_FAMILIES
    ):
        return (
            f"loudness family mismatch: {left_unit!r} vs {right_unit!r}. "
            f"dBFS, RMS dB, LUFS integrated and LUFS short-term are "
            f"different measurements, and no constant factor converts "
            f"between them"
        )
    return None


REGISTERED_UNITS: Final[dict[str, tuple[str, ...]]] = {
    "key": KEY_UNITS,
    "bpm": tuple(sorted(NUMERIC_UNITS["bpm"])),
    "energy": tuple(sorted(NUMERIC_UNITS["energy"])),
    "loudness": tuple(sorted(NUMERIC_UNITS["loudness"])),
    "duration": tuple(sorted(NUMERIC_UNITS["duration"])),
    "rating": (
        *sorted(NUMERIC_UNITS["rating"]), "rekordbox_popm_0_255", "linear_0_255"
    ),
    "count": tuple(sorted(NUMERIC_UNITS["count"])),
    "time": (*sorted(NUMERIC_UNITS["time"]), "beats"),
}


__all__ = [
    "ABSENT_UNITS",
    "KEY_SENTINELS",
    "KEY_UNITS",
    "KIND_CANONICAL",
    "LOUDNESS_FAMILIES",
    "MISSING",
    "MODE_MAJOR",
    "MODE_MINOR",
    "NUMERIC_SENTINELS",
    "NUMERIC_UNITS",
    "REGISTERED_UNITS",
    "UNDECIDABLE_UNITS",
    "UNIT_INPUT_RANGE",
    "Key",
    "NormaliseError",
    "is_comparable_unit",
    "normalise",
    "normalise_key",
    "normalise_numeric",
    "normalise_time",
    "normaliser_name",
    "units_incompatible",
]
