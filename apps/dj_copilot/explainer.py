"""Deterministic suggest-next explainer.

Builds a one-line reason from the measured key move (with the Camelot
rule name), the BPM step, the energy step and the pairing source.
Unknown terms are named as unknown rather than filled with canned
phrases. The hot path (explain=False) never calls this module's
``explain``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.shared.harmonic import CLASH_RELATION, TrackFeature, key_compat

MAX_EXPLAIN_CHARS = 120

# Human labels for every CAMELOT_RULES id plus clash. A new rule without
# a label fails test_explainer.test_every_camelot_rule_has_a_label.
RULE_LABELS: dict[str, str] = {
    "same_key": "same key",
    "adjacent": "adjacent",
    "relative": "relative",
    "diagonal": "diagonal",
    "energy_boost": "energy boost +2",
    "energy_drop": "energy drop -2",
    "jaws": "jaws -5",
    "pay_attention": "pay attention -3",
    "wrong_way_diagonal": "wrong-way diagonal",
    "parallel": "parallel",
    "minor_third_down": "minor third down +3",
    "major_third": "major third +4",
    "semitone_down": "semitone down +5",
    "tritone": "tritone +6",
    CLASH_RELATION: "clash",
}

_PAIR_LABELS: tuple[tuple[str, str], ...] = (
    ("pair_manual", "paired manually"),
    ("pair_learned", "paired from history"),
    ("pair_ai", "paired by AI"),
)


def _fmt_bpm(bpm: float) -> str:
    text = f"{bpm:.1f}"
    if text.endswith(".0"):
        return text[:-2]
    return text


def _key_measured(key: str | None) -> bool:
    return bool(key and str(key).strip())


def _bpm_measured(bpm: float | None) -> bool:
    return bpm is not None and bpm > 0


def _energy_measured(energy: int | None) -> bool:
    return energy is not None and energy > 0


def _key_term(current: TrackFeature, candidate: TrackFeature) -> str:
    if not _key_measured(current.key_camelot) or not _key_measured(candidate.key_camelot):
        return "key unknown"
    cur = str(current.key_camelot).strip()
    cand = str(candidate.key_camelot).strip()
    try:
        relation = key_compat(cur, cand).relation
    except ValueError:
        return "key unknown"
    return f"{cur} to {cand}, {RULE_LABELS[relation]}"


def _bpm_term(current: TrackFeature, candidate: TrackFeature) -> str:
    if not _bpm_measured(current.bpm) or not _bpm_measured(candidate.bpm):
        return "BPM unknown"
    cur = float(current.bpm)
    cand = float(candidate.bpm)
    pct = (cand - cur) / cur * 100.0
    return f"{_fmt_bpm(cur)} to {_fmt_bpm(cand)} BPM ({pct:+.1f}%)"


def _energy_term(current: TrackFeature, candidate: TrackFeature) -> str:
    if not _energy_measured(current.energy) or not _energy_measured(candidate.energy):
        return "energy unknown"
    return f"energy {current.energy} to {candidate.energy}"


def _pair_term(rationale: Mapping[str, Any] | None) -> str | None:
    if not rationale:
        return None
    for key, label in _PAIR_LABELS:
        if rationale.get(key):
            return label
    return None


def join_terms(terms: list[str], *, limit: int = MAX_EXPLAIN_CHARS) -> str:
    """Join terms with '; ', dropping whole trailing terms to stay within limit."""
    kept: list[str] = []
    for term in terms:
        candidate = "; ".join([*kept, term])
        if kept and len(candidate) > limit:
            break
        kept.append(term)
    return "; ".join(kept)


def explain(
    current: TrackFeature,
    candidate: TrackFeature,
    rationale: Mapping[str, Any] | None = None,
) -> str:
    """Return a one-line reason from the scored terms and the Camelot rule.

    Terms appear in order: key move, BPM step, energy step, pairing source.
    A missing key, BPM or energy is written as unknown, never as a canned
    match phrase. Pairing is included only when a pair source was measured.
    """
    terms = [
        _key_term(current, candidate),
        _bpm_term(current, candidate),
        _energy_term(current, candidate),
    ]
    pair = _pair_term(rationale)
    if pair is not None:
        terms.append(pair)
    return join_terms(terms)
