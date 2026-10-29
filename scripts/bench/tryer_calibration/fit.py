"""Bradley-Terry fit of layer-2 weights from the maintainer's blind rankings (TRYER-23).

Usage:
  python -m scripts.bench.tryer_calibration.fit --ratings <ratings.json> [--key <key.json>] [--json-out f]

``--key`` defaults to ``key.json`` beside the manifest the ratings name
(``ratings["manifest"]`` is relative to ``scripts/bench``).

Inputs are the two files a session produces: the ratings artifact the rater
page saves (POST /api/v1/bench/ratings or its download), and the hidden key
the manifest builder wrote beside the manifest (candidate role, style and
layer-2 features; never sent to the page's UI).

Model: for every ordered pair of non-anchor candidates inside one transition
pair, P(winner beats loser) = sigmoid(w . (x_winner - x_loser)), the logistic
form of Bradley-Terry, fitted by Newton's method with a small ridge. Features
are standardized by the spread of their deltas so the weights are comparable.

Reported: weights, separate style-family and exit-bar effects, held-out agreement (k-fold grouped by pair, so no pair is
in train and test at once), mean Kendall tau of the fitted composite and of
the try-er's own score against the human rank, and the low-anchor post-screen.

Requirements:
  ✔︎ ✅ 🎯 recovers the signs and order of known weights from synthetic rankings
  ✔︎ ✅ 🎯 random picks give near-chance held-out agreement (negative control)
  ✔︎ ✅ 🎯 post-screen voids a session whose low anchor is rated Good on > 15% of pairs
Acceptance:
  [if] a candidate id in the ratings is absent from the key [then ⛔️] FitError
  [if] a rated pair has no anchor rating [then ⛔️] FitError (post-screen unmeasurable)
  [if] fewer than MIN_COMPARISONS comparisons survive [then ⛔️] FitError, no weights
  [if] the post-screen fails [then] the report says VOID and the CLI exits 2
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

BENCH_ROOT: Path = Path(__file__).resolve().parents[1]


EXIT_FEATURE = "exit_bar_phrases"


class CFG:
    """Fit settings, all in one place."""

    RIDGE: float = 1.0
    NEWTON_STEPS: int = 50
    FOLDS: int = 5
    MIN_COMPARISONS: int = 10
    ANCHOR_ROLE: str = "anchor"
    ANCHOR_GOOD_MAX_SHARE: float = 0.15
    VERDICTS: tuple[str, ...] = ("good", "ok", "bad")


class FitError(ValueError):
    """The inputs cannot support a fit; nothing is reported as a result."""


@dataclass(frozen=True)
class RatedCandidate:
    cid: str
    pair_id: str
    role: str
    rank: int
    verdict: str
    features: dict[str, float]
    tryer_score: float | None


@dataclass
class FitReport:
    void: bool
    void_reason: str | None
    pairs: int
    comparisons: int
    features: list[str]
    weights: dict[str, float]
    feature_coverage: dict[str, int]
    feature_nonzero_deltas: dict[str, int]
    heldout_agreement: float | None
    heldout_comparisons: int
    kendall_tau_composite: float | None
    kendall_tau_tryer: float | None
    anchor_good_share: float
    dropped_features: dict[str, str] = field(default_factory=dict)
    effects: dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# parsing
# -----------------------------------------------------------------------------
def _human(entry: dict[str, Any]) -> dict[str, Any]:
    """One rating as the fit reads it: the note's pair, letter, rank and tags, plus the verdict from the rating's own
    ``verdict`` field, the one the ratings endpoint stores and counts. A verdict inside the note is refused, so the two
    can never disagree."""
    note = json.loads(entry["note"])
    if "verdict" in note:
        raise FitError(f"candidate {entry['id']!r}: the verdict belongs in the rating's verdict field, not its note")
    return {**note, "verdict": entry.get("verdict")}


def _rated_candidates(ratings: dict[str, Any], key: dict[str, Any]) -> list[RatedCandidate]:
    hidden: dict[str, Any] = key["candidates"]
    notes = {entry["id"]: _human(entry) for entry in ratings["ratings"]}
    touched = {n["pair_id"] for n in notes.values() if n.get("rank") is not None or n.get("verdict") is not None}
    for cid, human in notes.items():  # every id is checked, rated or not, so a stale save never slips through
        _require_in_key(cid, human, hidden)
    rated = [
        _rated_candidate(entry["id"], notes[entry["id"]], hidden)
        for entry in ratings["ratings"]
        if notes[entry["id"]]["pair_id"] in touched  # a pair not yet rated in a partial save carries no evidence
    ]
    _require_complete_pairs(rated, hidden, touched)
    return rated


def _require_in_key(cid: str, human: dict[str, Any], hidden: dict[str, Any]) -> None:
    if cid not in hidden:
        raise FitError(f"candidate {cid!r} is in the ratings but not in the key")
    if hidden[cid]["pair_id"] != human["pair_id"]:
        raise FitError(f"candidate {cid!r}: key says pair {hidden[cid]['pair_id']}, rating says {human['pair_id']}")


def _rated_candidate(cid: str, human: dict[str, Any], hidden: dict[str, Any]) -> RatedCandidate:
    """One rating of a touched pair, with its rank and verdict validated (never coerced)."""
    if human.get("rank") is None or human.get("verdict") not in CFG.VERDICTS:
        raise FitError(f"candidate {cid!r} has no rank or verdict: {human}")
    meta = hidden[cid]
    pair_size = sum(1 for m in hidden.values() if m["pair_id"] == meta["pair_id"])
    rank = human["rank"]
    # A JSON integer only: a bool, a numeric string or a fraction is refused, never coerced into a rank.
    if type(rank) is not int or not 1 <= rank <= pair_size:
        raise FitError(f"candidate {cid!r} rank {rank!r} is not an integer in 1..{pair_size}")
    return RatedCandidate(
        cid=cid,
        pair_id=meta["pair_id"],
        role=meta["role"],
        rank=rank,
        verdict=human["verdict"],
        features={k: float(v) for k, v in meta["features"].items()},
        tryer_score=meta.get("score"),
    )


def _require_complete_pairs(rated: list[RatedCandidate], hidden: dict[str, Any], touched: set[str]) -> None:
    """Every touched pair carries exactly the key's candidates, ranked 1..N with no repeats."""
    for pair_id in sorted(touched):
        expected = sorted(cid for cid, meta in hidden.items() if meta["pair_id"] == pair_id)
        got = [c for c in rated if c.pair_id == pair_id]
        got_ids = sorted(c.cid for c in got)
        if got_ids != expected:
            raise FitError(f"pair {pair_id}: rated candidates {got_ids} != key {expected}")
        ranks = sorted(c.rank for c in got)
        if ranks != list(range(1, len(got) + 1)):
            raise FitError(f"pair {pair_id}: ranks {ranks} are not exactly 1..{len(got)}")


def _by_pair(rated: list[RatedCandidate]) -> dict[str, list[RatedCandidate]]:
    grouped: dict[str, list[RatedCandidate]] = {}
    for candidate in rated:
        grouped.setdefault(candidate.pair_id, []).append(candidate)
    return grouped


# -----------------------------------------------------------------------------
# post-screen
# -----------------------------------------------------------------------------
def anchor_good_share(grouped: dict[str, list[RatedCandidate]]) -> float:
    """Share of pairs whose low anchor was rated above OK (Good)."""
    if not grouped:
        raise FitError("no pair is rated: the ratings carry no rank or verdict at all")
    goods = 0
    for pair_id, candidates in grouped.items():
        anchors = [c for c in candidates if c.role == CFG.ANCHOR_ROLE]
        if len(anchors) != 1:
            raise FitError(f"pair {pair_id}: expected one rated anchor, found {len(anchors)}")
        goods += anchors[0].verdict == "good"
    return goods / len(grouped)


# -----------------------------------------------------------------------------
# Bradley-Terry
# -----------------------------------------------------------------------------
def _comparisons(
    grouped: dict[str, list[RatedCandidate]], features: list[str]
) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Winner-minus-loser deltas (rows), the pair each row came from, and where both sides measured each feature."""
    rows: list[list[float]] = []
    owners: list[str] = []
    present: list[list[bool]] = []
    for pair_id, candidates in grouped.items():
        ranked = sorted((c for c in candidates if c.role != CFG.ANCHOR_ROLE), key=lambda c: c.rank)
        for winner, loser in itertools.combinations(ranked, 2):
            if winner.rank == loser.rank:
                continue
            rows.append([_delta(winner, loser, name) for name in features])
            present.append([name in winner.features and name in loser.features for name in features])
            owners.append(pair_id)
    shape = (len(rows), len(features))
    return np.array(rows, dtype=float).reshape(shape), owners, np.array(present, dtype=bool).reshape(shape)


def _delta(winner: RatedCandidate, loser: RatedCandidate, name: str) -> float:
    """A feature unmeasured on either side contributes no evidence (0)."""
    if name not in winner.features or name not in loser.features:
        return 0.0
    return winner.features[name] - loser.features[name]


def feature_scale(deltas: np.ndarray) -> np.ndarray:
    """Per-column scale: the spread of the deltas, or, where every delta is the same, their mean absolute value
    (a unanimous contrast is evidence, so it is kept and scaled, never dropped); 1 for an all-zero column."""
    spread = deltas.std(axis=0)
    level = np.abs(deltas).mean(axis=0)
    return np.where(spread > 0, spread, np.where(level > 0, level, 1.0))


def fit_weights(deltas: np.ndarray, ridge: float = CFG.RIDGE) -> np.ndarray:
    """Maximize sum log sigmoid(w . d) - ridge/2 |w|^2 by Newton's method."""
    weights = np.zeros(deltas.shape[1])
    for _ in range(CFG.NEWTON_STEPS):
        prob = 1.0 / (1.0 + np.exp(-(deltas @ weights)))
        gradient = deltas.T @ (1.0 - prob) - ridge * weights
        hessian = -(deltas.T * (prob * (1.0 - prob))) @ deltas - ridge * np.eye(len(weights))
        step = np.linalg.solve(hessian, gradient)
        weights = weights - step
        if np.max(np.abs(step)) < 1e-9:
            break
    return weights


def heldout_agreement(deltas: np.ndarray, owners: list[str], folds: int = CFG.FOLDS) -> tuple[float, int]:
    """Share of held-out comparisons the model orders the same way, k-fold grouped by pair."""
    pair_ids = sorted(set(owners))
    k = min(folds, len(pair_ids))
    if k < 2:
        raise FitError(f"held-out agreement needs at least 2 pairs, got {len(pair_ids)}")
    fold_of = {pid: i % k for i, pid in enumerate(pair_ids)}
    owner_folds = np.array([fold_of[o] for o in owners])
    hits = 0
    total = 0
    for fold in range(k):
        test = owner_folds == fold
        # Scale from the training pairs only: held-out rankings must not shape the preprocessing.
        spread = feature_scale(deltas[~test])
        weights = fit_weights(deltas[~test] / spread)
        margins = (deltas[test] / spread) @ weights
        hits += int(np.sum(margins > 0))
        total += int(np.sum(test))
    return hits / total, total


# -----------------------------------------------------------------------------
# rank agreement
# -----------------------------------------------------------------------------
def kendall_tau(scores: list[float], human_ranks: list[int]) -> float | None:
    """Tau-a between a score (higher better) and a human rank (1 best); None under 2 items."""
    if len(scores) < 2:
        return None
    concordant = 0
    discordant = 0
    for i, j in itertools.combinations(range(len(scores)), 2):
        model = np.sign(scores[i] - scores[j])
        human = np.sign(human_ranks[j] - human_ranks[i])
        concordant += int(model * human > 0)
        discordant += int(model * human < 0)
    pairs = len(scores) * (len(scores) - 1) / 2
    return (concordant - discordant) / pairs


def pairwise_tau(margins: list[float], human_orders: list[int]) -> float | None:
    """Tau-a from pairwise margins: each margin is the model's (i minus j) and each order is sign(rank_j - rank_i)."""
    if not margins:
        return None
    concordant = sum(1 for m, h in zip(margins, human_orders, strict=True) if m * h > 0)
    discordant = sum(1 for m, h in zip(margins, human_orders, strict=True) if m * h < 0)
    return (concordant - discordant) / len(margins)


def _mean_model_tau(grouped: dict[str, list[RatedCandidate]], weights: dict[str, float]) -> float | None:
    """Kendall tau of the FITTED model, scored exactly as it was trained: per pair of candidates, w . delta,
    where a feature missing on either side contributes 0 (``_delta``). No missing value is ever imputed."""
    taus: list[float] = []
    for candidates in grouped.values():
        rated = [c for c in candidates if c.role != CFG.ANCHOR_ROLE]
        margins: list[float] = []
        orders: list[int] = []
        for a, b in itertools.combinations(rated, 2):
            margins.append(sum(w * _delta(a, b, n) for n, w in weights.items()))
            orders.append(int(np.sign(b.rank - a.rank)))
        tau = pairwise_tau(margins, orders)
        if tau is not None:
            taus.append(tau)
    return float(np.mean(taus)) if taus else None


def _mean_tau(grouped: dict[str, list[RatedCandidate]], score_of: Any) -> float | None:
    taus: list[float] = []
    for candidates in grouped.values():
        rated = [c for c in candidates if c.role != CFG.ANCHOR_ROLE and score_of(c) is not None]
        tau = kendall_tau([score_of(c) for c in rated], [c.rank for c in rated])
        if tau is not None:
            taus.append(tau)
    return float(np.mean(taus)) if taus else None


# -----------------------------------------------------------------------------
# entry point
# -----------------------------------------------------------------------------
def _usable_features(
    names: list[str], deltas: np.ndarray, present: np.ndarray
) -> tuple[dict[str, int], dict[str, int], dict[str, str], list[int]]:
    """Coverage (both sides measured), nonzero deltas, dropped features with why, and the kept column indexes."""
    coverage = {n: int(np.count_nonzero(present[:, i])) for i, n in enumerate(names)}
    nonzero = {n: int(np.count_nonzero(deltas[:, i])) for i, n in enumerate(names)}
    dropped = {n: "no non-zero delta in any comparison" for n in names if nonzero[n] == 0}
    keep = [i for i, n in enumerate(names) if n not in dropped]
    if not keep:
        raise FitError("every feature was dropped; nothing to fit")
    return coverage, nonzero, dropped, keep


def fit_session(ratings: dict[str, Any], key: dict[str, Any]) -> FitReport:
    grouped = _by_pair(_rated_candidates(ratings, key))
    share = anchor_good_share(grouped)
    void = share > CFG.ANCHOR_GOOD_MAX_SHARE
    names = sorted({n for cs in grouped.values() for c in cs if c.role != CFG.ANCHOR_ROLE for n in c.features})
    deltas, owners, present = _comparisons(grouped, names)
    if len(owners) < CFG.MIN_COMPARISONS:
        raise FitError(f"only {len(owners)} comparisons; a fit needs at least {CFG.MIN_COMPARISONS}")
    coverage, nonzero, dropped, keep = _usable_features(names, deltas, present)
    kept_names = [names[i] for i in keep]
    scale = feature_scale(deltas)[keep]
    scaled = deltas[:, keep] / scale
    weights = fit_weights(scaled)
    agreement, heldout_n = heldout_agreement(deltas[:, keep], owners)
    by_name = dict(zip(kept_names, weights / scale, strict=True))

    return FitReport(
        effects=separate_effects(by_name),
        void=void,
        void_reason=(
            f"low anchor rated Good on {share:.0%} of pairs (> {CFG.ANCHOR_GOOD_MAX_SHARE:.0%})" if void else None
        ),
        pairs=len(grouped),
        comparisons=len(owners),
        features=kept_names,
        weights={n: float(w) for n, w in zip(kept_names, weights, strict=True)},
        feature_coverage={n: coverage[n] for n in kept_names},
        feature_nonzero_deltas={n: nonzero[n] for n in kept_names},
        heldout_agreement=agreement,
        heldout_comparisons=heldout_n,
        kendall_tau_composite=_mean_model_tau(grouped, by_name),
        kendall_tau_tryer=_mean_tau(grouped, lambda c: c.tryer_score),
        anchor_good_share=share,
        dropped_features=dropped,
    )


def separate_effects(raw_weights: dict[str, float]) -> dict[str, Any]:
    """Style-family and exit-bar effects reported apart (taste-v2 crosses them).

    Family weights are centered on their mean: only differences between families
    are identified, since every candidate carries exactly one family column.
    Weights are per raw unit (exit bar: per 8-bar phrase later).
    """
    families = {n.split(":", 1)[1]: w for n, w in raw_weights.items() if n.startswith("family:")}
    mean = float(np.mean(list(families.values()))) if families else 0.0
    return {
        "style_family": {f: w - mean for f, w in sorted(families.items(), key=lambda kv: -kv[1])},
        "exit_bar_per_phrase": raw_weights.get(EXIT_FEATURE),
        "other": {n: w for n, w in raw_weights.items() if not n.startswith("family:") and n != EXIT_FEATURE},
    }


def _print_report(report: FitReport) -> None:
    status = "VOID" if report.void else "OK"
    print(
        f"[{status}] pairs={report.pairs} comparisons={report.comparisons} anchor_good_share={report.anchor_good_share:.2f}"
    )
    if report.void_reason:
        print(f"[WARN] {report.void_reason}: weights below are not to be used")
    for name, weight in sorted(report.weights.items(), key=lambda kv: -abs(kv[1])):
        print(
            f"  weight {name:32s} {weight:+.3f} (standardized; measured in {report.feature_coverage[name]}"
            f" of {report.comparisons} comparisons, differing in {report.feature_nonzero_deltas[name]})"
        )
    print(
        f"  held-out agreement {report.heldout_agreement:.3f} over {report.heldout_comparisons} comparisons (chance 0.5)"
    )
    print(f"  effect style family (centered, per candidate): {report.effects['style_family']}")
    print(f"  effect exit bar (per 8-bar phrase later): {report.effects['exit_bar_per_phrase']}")
    print(f"  kendall tau composite {report.kendall_tau_composite} | try-er score {report.kendall_tau_tryer}")
    for name, why in report.dropped_features.items():
        print(f"  [WARN] dropped {name}: {why}")


def _key_beside_manifest(ratings: dict[str, Any]) -> Path:
    manifest = ratings.get("manifest")
    if not manifest:
        raise FitError("the ratings name no manifest, so pass --key explicitly")
    key_path = BENCH_ROOT / Path(manifest).parent / "key.json"
    if not key_path.is_file():
        raise FitError(f"no key at {key_path}; pass --key explicitly")
    return key_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ratings", type=Path, required=True)
    parser.add_argument("--key", type=Path, help="default: key.json beside the ratings' manifest")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    ratings = json.loads(args.ratings.read_text(encoding="utf-8"))
    key_path = args.key or _key_beside_manifest(ratings)
    report = fit_session(ratings, json.loads(key_path.read_text(encoding="utf-8")))
    _print_report(report)
    if args.json_out:
        args.json_out.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8", newline="\n")
    return 2 if report.void else 0


if __name__ == "__main__":
    sys.exit(main())
