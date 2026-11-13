"""Bradley-Terry scorer and adoption rule for lighting A/B sessions (spec 07 section 5).

Stdlib only. Pairwise results are ``(winner, loser)`` tuples; triad results are the chosen
variant per triad. A variant is adopted only on a clear win, otherwise the placeholder default
is kept.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence

MIN_PAIR_WIN_RATE = 0.6
TRIAD_MIN_CHOICES = 7

_ITERATIONS = 200
_PRIOR = 0.5  # virtual half win and half loss against a unit-strength reference


def fit_bradley_terry(results: Iterable[tuple[str, str]]) -> dict[str, float]:
    """Fit Bradley-Terry strengths (geometric mean 1) from (winner, loser) pairs, deterministically."""
    pairs = [(w, lo) for w, lo in results if w != lo]
    players = sorted({p for pair in pairs for p in pair})
    if not players:
        return {}
    wins = Counter(w for w, _ in pairs)
    games: Counter[tuple[str, str]] = Counter()
    for w, lo in pairs:
        games[tuple(sorted((w, lo)))] += 1
    strength = dict.fromkeys(players, 1.0)
    for _ in range(_ITERATIONS):
        new: dict[str, float] = {}
        for p in players:
            denom = 2 * _PRIOR / (strength[p] + 1.0)
            for (a, b), n in games.items():
                if p in (a, b):
                    other = b if p == a else a
                    denom += n / (strength[p] + strength[other])
            new[p] = (wins[p] + _PRIOR) / denom
        log_mean = sum(math.log(v) for v in new.values()) / len(new)
        scale = math.exp(log_mean)
        strength = {p: new[p] / scale for p in players}
    return {p: round(strength[p], 9) for p in players}


def pair_win_rate(results: Sequence[tuple[str, str]], variant: str) -> float:
    """Return the share of its pairs that a variant won (0.0 when it played none)."""
    played = [pair for pair in results if variant in pair and pair[0] != pair[1]]
    if not played:
        return 0.0
    return sum(1 for w, _ in played if w == variant) / len(played)


def choose_pairwise(
    results: Sequence[tuple[str, str]],
    default: str,
    min_win_rate: float = MIN_PAIR_WIN_RATE,
) -> str:
    """Adopt the top Bradley-Terry variant if unique and it wins >= min_win_rate of pairs, else default."""
    scores = fit_bradley_terry(results)
    if not scores:
        return default
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best, top = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == top:
        return default
    if pair_win_rate(results, best) < min_win_rate:
        return default
    return best


def choose_triad(
    choices: Sequence[str],
    default: str,
    min_choices: int = TRIAD_MIN_CHOICES,
) -> str:
    """Adopt the triad variant chosen at least min_choices times (LA3: 7 of 12), else default."""
    counts = Counter(choices)
    if not counts:
        return default
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    best, n = ranked[0]
    return best if n >= min_choices else default
