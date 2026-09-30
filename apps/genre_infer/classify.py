"""Genre from audio embeddings: a softmax classifier trained on the library's own tags.

WHY A LINEAR HEAD. The embedding (`clap_runner.py`) already separates genres;
the classifier only has to draw lines between them. A linear softmax head
trains in well under a second on a few thousand tracks, stores as a small JSON
document with no pickle, and cannot memorize a library the way a deep head
can. On the FMA spike (Thu 24 Sep 2026, 278 embedded full tracks, 10
genres, 5-fold CV) this head scored 0.62 accuracy against 0.45 for CLAP
zero-shot text prompts, 0.11 for always guessing the biggest genre and 0.08
with shuffled labels; details in
`docs/research/segmentation-genre-spike-20260924.md`.

WHY NUMPY AND NOT SCIKIT-LEARN. scikit-learn lives in the optional `ai` extra
and this has to run, and be tested, in the default app venv. Full-batch
gradient descent on an L2-regularized softmax is thirty lines and fully
deterministic, which a suggestion a user will see needs.

HONEST ACCURACY TRAVELS WITH THE MODEL. `train` runs a stratified k-fold
cross-validation before fitting on everything, and the model document carries
that held-out accuracy and the per-class counts it was measured on. A
suggestion can then say how often this model is right, rather than quoting a
training-set number that is always flattering.

WHAT IS NEVER DONE. A suggestion never overwrites a genre tag a person or
rekordbox set. It is a separate value with its own confidence, and classes
with fewer than ``min_per_class`` labeled tracks are left out of the model
(and named in it) rather than predicted from a handful of examples.

-Claude
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SCHEMA = "genre-head/v1"


@dataclass(frozen=True)
class GenreModel:
    classes: list[str]
    weights: np.ndarray  # (n_classes, dim)
    bias: np.ndarray  # (n_classes,)
    embedding_model: str
    embedding_revision: str
    train_counts: dict[str, int]
    dropped_classes: dict[str, int]
    cv_accuracy: float | None
    cv_macro_f1: float | None
    trainset_digest: str
    meta: dict[str, Any] = field(default_factory=dict)

    def proba(self, x: np.ndarray) -> np.ndarray:
        return _softmax(np.atleast_2d(x) @ self.weights.T + self.bias)

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": SCHEMA,
                "classes": self.classes,
                "weights": np.round(self.weights, 6).tolist(),
                "bias": np.round(self.bias, 6).tolist(),
                "embedding_model": self.embedding_model,
                "embedding_revision": self.embedding_revision,
                "train_counts": self.train_counts,
                "dropped_classes": self.dropped_classes,
                "cv_accuracy": self.cv_accuracy,
                "cv_macro_f1": self.cv_macro_f1,
                "trainset_digest": self.trainset_digest,
                "meta": self.meta,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> GenreModel:
        d = json.loads(text)
        if d.get("schema") != SCHEMA:
            raise ValueError(f"not a {SCHEMA} document: schema={d.get('schema')!r}")
        return cls(
            classes=list(d["classes"]),
            weights=np.asarray(d["weights"], dtype=np.float64),
            bias=np.asarray(d["bias"], dtype=np.float64),
            embedding_model=d["embedding_model"],
            embedding_revision=d["embedding_revision"],
            train_counts=dict(d["train_counts"]),
            dropped_classes=dict(d["dropped_classes"]),
            cv_accuracy=d["cv_accuracy"],
            cv_macro_f1=d["cv_macro_f1"],
            trainset_digest=d["trainset_digest"],
            meta=dict(d.get("meta", {})),
        )


@dataclass(frozen=True)
class Suggestion:
    genre: str
    confidence: float
    top: list[tuple[str, float]]


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def _normalize(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=1, keepdims=True)
    if np.any(n == 0):
        raise ValueError("an embedding has zero norm; it was not measured")
    return x / n


def fit_softmax(
    x: np.ndarray,
    y: np.ndarray,
    n_classes: int,
    *,
    l2: float = 0.3,
    steps: int = 800,
    lr: float = 0.5,
) -> tuple[np.ndarray, np.ndarray]:
    """Class-balanced, L2-regularized softmax regression by full-batch gradient descent.

    ``l2`` penalizes the weights in the scaled space below. 0.3 is measured,
    not guessed: on the 278-track FMA spike, 5-fold accuracy was flat at
    0.63 to 0.64 for any l2 from 0.1 to 2.3 (2.3 is scikit-learn's C=1
    translated to this space) and fell to 0.57 at 1e-3, which also left
    every prediction near-certain so no confidence threshold could filter
    anything. At 0.3, predictions at confidence >= 0.6 cover 46 percent of
    tracks at 0.80 accuracy.
    """
    n, dim = x.shape
    counts = np.bincount(y, minlength=n_classes).astype(np.float64)
    sample_w = (n / (n_classes * counts))[y]  # "balanced", so a big class cannot swamp a small one
    onehot = np.eye(n_classes)[y]
    w = np.zeros((n_classes, dim))
    b = np.zeros(n_classes)
    # Adam: plain gradient descent needs a per-dataset learning rate to converge.
    mw, vw, mb, vb = (np.zeros_like(w), np.zeros_like(w), np.zeros_like(b), np.zeros_like(b))
    b1, b2, eps = 0.9, 0.999, 1e-8
    scale = x.shape[1] ** 0.5  # unit vectors have tiny logits; scale so steps stay O(1)
    xs = x * scale
    for t in range(1, steps + 1):
        p = _softmax(xs @ w.T + b)
        g = (p - onehot) * sample_w[:, None] / n
        gw = g.T @ xs + l2 * w
        gb = g.sum(axis=0)
        mw = b1 * mw + (1 - b1) * gw
        vw = b2 * vw + (1 - b2) * gw * gw
        mb = b1 * mb + (1 - b1) * gb
        vb = b2 * vb + (1 - b2) * gb * gb
        w -= lr * 0.01 * (mw / (1 - b1**t)) / (np.sqrt(vw / (1 - b2**t)) + eps)
        b -= lr * 0.01 * (mb / (1 - b1**t)) / (np.sqrt(vb / (1 - b2**t)) + eps)
    return w * scale, b


def _stratified_folds(y: np.ndarray, k: int, seed: int) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    folds: list[list[int]] = [[] for _ in range(k)]
    for c in np.unique(y):
        idx = rng.permutation(np.flatnonzero(y == c))
        for i, j in enumerate(idx):
            folds[i % k].append(int(j))
    return [np.array(sorted(f)) for f in folds]


def macro_f1(y: np.ndarray, pred: np.ndarray, n_classes: int) -> float:
    scores = []
    for c in range(n_classes):
        tp = np.sum((pred == c) & (y == c))
        fp = np.sum((pred == c) & (y != c))
        fn = np.sum((pred != c) & (y == c))
        scores.append(0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn))
    return float(np.mean(scores))


def _split_classes(
    counts: Mapping[str, int], min_per_class: int
) -> tuple[list[str], dict[str, int]]:
    """(genres kept for training, genres dropped with their counts); refuses fewer than 2 kept."""
    kept = sorted(c for c, n in counts.items() if n >= min_per_class)
    dropped = {c: n for c, n in sorted(counts.items()) if n < min_per_class}
    if len(kept) < 2:
        raise ValueError(
            f"need at least 2 genres with >= {min_per_class} labeled, embedded tracks; "
            f"have {dict(counts)}"
        )
    return kept, dropped


def _cross_validate(
    x: np.ndarray, y: np.ndarray, n_classes: int, k: int, seed: int
) -> tuple[float, float]:
    """(accuracy, macro F1) with every track predicted by a head that never saw it."""
    pred = np.empty_like(y)
    for test in _stratified_folds(y, k, seed):
        train_mask = np.ones(len(y), dtype=bool)
        train_mask[test] = False
        w, b = fit_softmax(x[train_mask], y[train_mask], n_classes)
        pred[test] = np.argmax(x[test] @ w.T + b, axis=1)
    return float(np.mean(pred == y)), macro_f1(y, pred, n_classes)


def train(
    vectors: Mapping[str, Sequence[float]],
    labels: Mapping[str, str],
    *,
    embedding_model: str,
    embedding_revision: str,
    min_per_class: int = 8,
    folds: int = 5,
    seed: int = 0,
) -> GenreModel:
    """Fit on every track that has both a vector and a label; cross-validate first."""
    ids = sorted(set(vectors) & set(labels))
    counts = Counter(labels[i] for i in ids)
    kept, dropped = _split_classes(counts, min_per_class)
    ids = [i for i in ids if labels[i] in kept]
    index = {c: k for k, c in enumerate(kept)}
    x = _normalize(np.asarray([vectors[i] for i in ids], dtype=np.float64))
    y = np.asarray([index[labels[i]] for i in ids])

    k = min(folds, int(np.bincount(y).min()))
    cv_acc, cv_f1 = _cross_validate(x, y, len(kept), k, seed) if k >= 2 else (None, None)

    w, b = fit_softmax(x, y, len(kept))
    digest = hashlib.sha256(
        json.dumps([[i, labels[i]] for i in ids], separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return GenreModel(
        classes=kept,
        weights=w,
        bias=b,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        train_counts={c: counts[c] for c in kept},
        dropped_classes=dropped,
        cv_accuracy=cv_acc,
        cv_macro_f1=cv_f1,
        trainset_digest=digest,
        meta={"folds": k, "seed": seed, "min_per_class": min_per_class},
    )


def suggest(
    model: GenreModel, vectors: Mapping[str, Sequence[float]], *, top_k: int = 3
) -> dict[str, Suggestion]:
    """One suggestion per track: the top genre, its probability, and the runners-up."""
    ids = sorted(vectors)
    if not ids:
        return {}
    p = model.proba(_normalize(np.asarray([vectors[i] for i in ids], dtype=np.float64)))
    out: dict[str, Suggestion] = {}
    for i, row in zip(ids, p, strict=True):
        order = np.argsort(-row)[:top_k]
        top = [(model.classes[j], round(float(row[j]), 4)) for j in order]
        out[i] = Suggestion(top[0][0], top[0][1], top)
    return out


__all__ = ["GenreModel", "Suggestion", "fit_softmax", "macro_f1", "suggest", "train"]
