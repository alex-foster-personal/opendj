"""Bidirectional key canonicalizer: rekordbox / MIK Camelot / MIK Open Key /
our internal (pitch_class, is_minor) representation.

WHY THIS FILE EXISTS. apps/analysis/backends/librosa.py:50-54 carried an
`_OPENKEY_MINOR` table that reused the Camelot number directly instead of
applying the Open Key rule (Open Key number = Camelot number + 5, mod 12,
1-indexed). A minor (pitch class 9) is Camelot 8A, so it should be Open Key
1m; the old table said 8m. The bug was a whole-table one-position rotation of
the correct table (confirmed here by
`test_open_key_number_equals_camelot_number_plus_five_mod_twelve`, which
checks the rule directly for all 12 pitch classes in both modes rather than
trusting a hand-built table), affecting all 24 entries across both the major
and minor tables, not just A minor. librosa.py's own tables are corrected to
match in the same commit.

WHY THIS FILE DOES NOT OWN A KEY TABLE. apps/equivalence/normalisers.py
already says it plainly: "Key mapping is DELEGATED to
apps.shared.harmonic.key_to_camelot ... A second key table would silently
diverge from it." This module is a thin, analysis-lane-flavored adapter over
that existing infrastructure (apps.shared.harmonic + apps.equivalence.
normalisers), not a fourth table:
  - Camelot <-> pitch_class/mode: apps.shared.harmonic.key_to_camelot via
    apps.equivalence.normalisers.Key.
  - Open Key <-> Camelot: apps.equivalence.normalisers.Key.open_key (added
    alongside this module, since nothing needed that direction before).
  - rekordbox djmdKey.ScaleName, which stores EITHER a spelled name ("F#m")
    OR a bare Camelot code ("8A") depending on how the track was analyzed
    (confirmed against /opt/mdt-fixtures/rekordbox/master.plain.db: 13 of 21
    rows spelled, 8 of 21 bare Camelot): normalise_key(..., "mixed_camelot_
    musical"), which already accepts both shapes.
  - The canonical rekordbox-style SPELLING (flats for Db/Eb/Ab/Bb, sharp for
    F#) used when we need to WRITE a scale name back out: genuinely new,
    since `key_to_camelot` is many-to-one over enharmonic spellings and
    cannot be inverted to pick one.

Neither rekordbox nor MIK is ground truth for the musical key of a track
(see specs/native-analysis-v1.md section 5); this module only converts
between NOTATIONS of a key both sides can already agree to disagree about.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.equivalence.normalisers import MISSING, MODE_MAJOR, MODE_MINOR, normalise_key
from apps.equivalence.normalisers import Key as _NormKey

# Canonical pitch-class spelling (0=C .. 11=B) used only when WRITING a
# rekordbox-style scale name, matching the spelling already observed in real
# rekordbox djmdKey.ScaleName rows: flats for Db/Eb/Ab/Bb, sharp for F#. Same
# spelling is used for major and minor. Reading accepts any spelling
# apps.shared.harmonic.key_to_camelot understands (see module docstring).
_PITCH_SPELLING = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


@dataclass(frozen=True)
class Key:
    """Our internal key representation: pitch class 0 (C) .. 11 (B), mode bit."""

    pitch_class: int
    is_minor: bool

    def __post_init__(self) -> None:
        if not 0 <= self.pitch_class <= 11:
            raise ValueError(f"pitch_class must be 0..11, got {self.pitch_class}")


#-----------------------------------------------------------------------------
def _from_norm_key(norm_key: _NormKey) -> Key:
    return Key(pitch_class=norm_key.pitch_class, is_minor=norm_key.mode == MODE_MINOR)


def _to_norm_key(key: Key) -> _NormKey:
    return _NormKey(
        pitch_class=key.pitch_class, mode=MODE_MINOR if key.is_minor else MODE_MAJOR
    )


def _normalise_or_raise(value: str, unit: str) -> Key:
    result = normalise_key(value, unit)
    if result is MISSING:
        raise ValueError(
            f"{unit} value {value!r} is a documented sentinel for 'no key', "
            "not a parseable key"
        )
    return _from_norm_key(result)


#-----------------------------------------------------------------------------
def to_camelot(key: Key) -> str:
    return _to_norm_key(key).camelot


def from_camelot(camelot: str) -> Key:
    return _normalise_or_raise(camelot, "camelot")


def to_open_key(key: Key) -> str:
    return _to_norm_key(key).open_key


def from_open_key(open_key: str) -> Key:
    return _normalise_or_raise(open_key, "open_key")


#-----------------------------------------------------------------------------
def to_rekordbox_scale_name(key: Key) -> str:
    name = _PITCH_SPELLING[key.pitch_class]
    return f"{name}m" if key.is_minor else name


def from_rekordbox_scale_name(scale_name: str) -> Key:
    """Real rekordbox djmdKey.ScaleName rows carry EITHER a spelled name
    ("F#m", "C") OR a bare Camelot code ("8A", "2B") depending on how the
    track was analyzed, per the fixture at
    /opt/mdt-fixtures/rekordbox/master.plain.db (8 of 21 rows are Camelot
    codes, 13 are spelled names). normalise_key's "mixed_camelot_musical"
    unit already accepts both shapes.
    """
    return _normalise_or_raise(scale_name, "mixed_camelot_musical")


#-----------------------------------------------------------------------------
# MIK stores keys as Camelot and Open Key strings using the same notations
# rekordbox and this module use, so no separate parser is needed: these
# aliases exist only to give MIK-sourced callers self-documenting names.
from_mik_camelot = from_camelot
to_mik_camelot = to_camelot
from_mik_open_key = from_open_key
to_mik_open_key = to_open_key
