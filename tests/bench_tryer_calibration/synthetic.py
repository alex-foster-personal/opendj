"""Synthetic sessions for the fit tests: rankings drawn from KNOWN weights.

The ratings and key documents have the exact shapes the rater page saves and
the manifest builder writes, so the fit's parser is exercised too. Rankings
come from a Plackett-Luce draw (utility plus Gumbel noise), the generative
model whose pairwise marginals are Bradley-Terry.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

ROLES = ("top", "contrast", "mid")


def make_session(
    weights: dict[str, float],
    *,
    pairs: int,
    seed: int,
    random_picks: bool = False,
    anchor_good_pairs: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (ratings, key) for ``pairs`` pairs of 3 rated candidates plus an anchor."""
    rng = np.random.default_rng(seed)
    names = list(weights)
    vector = np.array([weights[n] for n in names])
    hidden: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    for p in range(pairs):
        pair_id = f"p{p:03d}"
        features = rng.normal(size=(len(ROLES), len(names)))
        utility = features @ vector + rng.gumbel(size=len(ROLES))
        if random_picks:
            utility = rng.permutation(len(ROLES)).astype(float)
        order = list(np.argsort(-utility))
        for i, role in enumerate(ROLES):
            cid = f"{pair_id}-{role}"
            hidden[cid] = {
                "pair_id": pair_id,
                "role": role,
                "features": dict(zip(names, map(float, features[i]), strict=True)),
                "score": float(features[i] @ vector),
            }
            entries.append(_entry(cid, pair_id, order.index(i) + 1, "ok"))
        anchor = f"{pair_id}-anchor"
        hidden[anchor] = {
            "pair_id": pair_id,
            "role": "anchor",
            "features": hidden[f"{pair_id}-top"]["features"],
            "score": None,
        }
        entries.append(_entry(anchor, pair_id, len(ROLES) + 1, "good" if p < anchor_good_pairs else "bad"))
    ratings = {"track": "synthetic", "blind_mode": True, "manifest": None, "ratings": entries}
    return ratings, {"candidates": hidden}


def _entry(cid: str, pair_id: str, rank: int, verdict: str) -> dict[str, Any]:
    return {
        "id": cid,
        "file": f"clips/{cid}.wav",
        "params": "",
        "machine_score": None,
        "human_score": None,
        "verdict": verdict,
        "note": json.dumps({"pair_id": pair_id, "letter": "A", "rank": rank, "tags": []}),
    }


FAMILIES = ("blend", "crossfade", "filter")


def make_factorial_session(
    family_effect: dict[str, float], exit_effect: float, *, pairs: int, seed: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """taste-v2 sessions: (A, X), (B, X), (A, Y) per pair; utility = family effect + exit effect * phrases + Gumbel."""
    rng = np.random.default_rng(seed)
    hidden: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    for p in range(pairs):
        pair_id = f"p{p:03d}"
        fam_a, fam_b = rng.choice(FAMILIES, size=2, replace=False)
        exit_x, exit_y = rng.choice(np.arange(8, 30), size=2, replace=False).astype(float)
        cells = [(fam_a, exit_x), (fam_b, exit_x), (fam_a, exit_y)]
        utility = np.array([family_effect[f] + exit_effect * e for f, e in cells]) + rng.gumbel(size=3)
        order = list(np.argsort(-utility))
        for i, (family, exit_phrases) in enumerate(cells):
            cid = f"{pair_id}-t{i}"
            features = {f"family:{f}": float(f == family) for f in FAMILIES} | {"exit_bar_phrases": float(exit_phrases)}
            hidden[cid] = {"pair_id": pair_id, "role": f"taste{i + 1}", "features": features, "score": 0.0}
            entries.append(_entry(cid, pair_id, order.index(i) + 1, "ok"))
        anchor = f"{pair_id}-anchor"
        hidden[anchor] = {
            "pair_id": pair_id,
            "role": "anchor",
            "features": hidden[f"{pair_id}-t0"]["features"],
            "score": None,
        }
        entries.append(_entry(anchor, pair_id, 4, "bad"))
    return {"track": "synthetic", "blind_mode": True, "manifest": None, "ratings": entries}, {"candidates": hidden}
