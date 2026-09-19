"""Trained transition classifier (scikit-learn GradientBoostingClassifier).

Plan 12-02 Step 5. Training requires >= :data:`MIN_TRAINING_SESSIONS`
labeled sessions; the macro-F1 > :data:`MIN_MACRO_F1_FOR_TRAINED` gate
decides whether the fresh model replaces the rules. Deterministic via
``random_state=42``.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths as sets_paths
from .. import transitions as transitions_mod

logger = logging.getLogger(__name__)


class ModelNotReadyError(RuntimeError):
    """Raised when predict() is called before a model has been trained."""


@dataclass
class TrainReport:
    """Returned by :func:`train` so the CLI can print + archive."""

    n_training_sessions: int
    n_labeled_transitions: int
    macro_f1: float
    classes: list[str]
    model_path: Path | None
    meta_path: Path | None
    feature_columns: list[str]
    accepted: bool


MIN_TRAINING_SESSIONS: int = 5


def _load_session_corpus(
    sessions_root: Path, session_id: str
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """Return (transitions, labels_by_idx) for a session."""
    sess_dir = sets_paths.session_dir(session_id, root=sessions_root)
    transitions: list[dict[str, Any]] = []
    labels: dict[int, str] = {}
    tpath = sess_dir / "transitions.jsonl"
    lpath = sess_dir / "labels.jsonl"
    if tpath.exists():
        with tpath.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    transitions.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if lpath.exists():
        with lpath.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                labels[int(row["idx"])] = str(row["class"])
    return transitions, labels


def train(
    labeled_sessions: Iterable[str],
    *,
    sessions_root: Path | None = None,
    models_dir: Path | None = None,
    random_state: int = 42,
) -> TrainReport:
    """Train a GradientBoostingClassifier over ``labeled_sessions``.

    Persists joblib + meta when macro-F1 > gate; returns a
    :class:`TrainReport` describing the outcome. If the corpus is too
    small, returns ``accepted=False`` without writing files.
    """
    root = Path(sessions_root) if sessions_root is not None else sets_paths.SETS_DIR
    models = Path(models_dir) if models_dir is not None else sets_paths.MODELS_DIR

    corpora = []
    session_ids_kept: list[str] = []
    total_labeled = 0
    for sid in labeled_sessions:
        transitions, labels = _load_session_corpus(root, sid)
        if not labels:
            continue
        session_ids_kept.append(sid)
        corpora.append((sid, transitions, labels))
        total_labeled += len(labels)

    feature_cols = list(transitions_mod.FEATURE_COLUMNS)
    if len(session_ids_kept) < MIN_TRAINING_SESSIONS:
        return TrainReport(
            n_training_sessions=len(session_ids_kept),
            n_labeled_transitions=total_labeled,
            macro_f1=0.0,
            classes=[],
            model_path=None,
            meta_path=None,
            feature_columns=feature_cols,
            accepted=False,
        )

    X: list[list[float]] = []
    y: list[str] = []
    groups: list[str] = []
    for sid, transitions, labels in corpora:
        for t in transitions:
            idx = int(t.get("idx", -1))
            if idx not in labels:
                continue
            feats = t.get("features", {})
            X.append([float(feats.get(col, 0.0)) for col in feature_cols])
            y.append(labels[idx])
            groups.append(sid)

    if not X:
        return TrainReport(
            n_training_sessions=len(session_ids_kept),
            n_labeled_transitions=0,
            macro_f1=0.0,
            classes=[],
            model_path=None,
            meta_path=None,
            feature_columns=feature_cols,
            accepted=False,
        )

    import joblib
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import f1_score
    from sklearn.model_selection import LeaveOneGroupOut

    X_arr = np.asarray(X, dtype=float)
    y_arr = np.asarray(y)
    g_arr = np.asarray(groups)
    classes = sorted(set(y_arr.tolist()))

    logo = LeaveOneGroupOut()
    fold_scores: list[float] = []
    for train_idx, test_idx in logo.split(X_arr, y_arr, g_arr):
        if len(set(y_arr[train_idx].tolist())) < 2:
            continue
        clf = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, random_state=random_state
        )
        clf.fit(X_arr[train_idx], y_arr[train_idx])
        y_pred = clf.predict(X_arr[test_idx])
        fold_scores.append(
            f1_score(y_arr[test_idx], y_pred, average="macro", zero_division=0)
        )
    macro_f1 = float(np.mean(fold_scores)) if fold_scores else 0.0
    logger.info("macro-F1 = %.3f over %d folds", macro_f1, len(fold_scores))

    from . import META_FILE_NAME, MIN_MACRO_F1_FOR_TRAINED, MODEL_FILE_NAME

    if macro_f1 < MIN_MACRO_F1_FOR_TRAINED:
        return TrainReport(
            n_training_sessions=len(session_ids_kept),
            n_labeled_transitions=len(y),
            macro_f1=macro_f1,
            classes=classes,
            model_path=None,
            meta_path=None,
            feature_columns=feature_cols,
            accepted=False,
        )

    final = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, random_state=random_state
    )
    final.fit(X_arr, y_arr)
    models.mkdir(parents=True, exist_ok=True)
    model_path = models / MODEL_FILE_NAME
    meta_path = models / META_FILE_NAME
    joblib.dump(final, model_path)
    meta = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "macro_f1": round(macro_f1, 4),
        "feature_columns": feature_cols,
        "class_list": classes,
        "n_training_sessions": len(session_ids_kept),
        "n_labeled_transitions": len(y),
        "algorithm": "sklearn.GradientBoostingClassifier",
    }
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    return TrainReport(
        n_training_sessions=len(session_ids_kept),
        n_labeled_transitions=len(y),
        macro_f1=macro_f1,
        classes=classes,
        model_path=model_path,
        meta_path=meta_path,
        feature_columns=feature_cols,
        accepted=True,
    )


def predict(
    transition: transitions_mod.Transition,
    *,
    models_dir: Path | None = None,
) -> tuple[str, float]:
    """Load the trained model and predict ``transition``.

    Raises :class:`ModelNotReadyError` if no trained model is present.
    """
    from . import META_FILE_NAME, MODEL_FILE_NAME

    models = Path(models_dir) if models_dir is not None else sets_paths.MODELS_DIR
    model_path = models / MODEL_FILE_NAME
    meta_path = models / META_FILE_NAME
    if not model_path.exists() or not meta_path.exists():
        raise ModelNotReadyError(
            f"no trained model at {model_path}; run `python -m apps.sets train`"
        )
    import joblib

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    clf = joblib.load(model_path)
    feature_cols = list(meta.get("feature_columns", transitions_mod.FEATURE_COLUMNS))
    row = [[float(transition.features.get(col, 0.0)) for col in feature_cols]]
    probs = None
    if hasattr(clf, "predict_proba"):
        probs = clf.predict_proba(row)[0]
    if probs is not None:
        classes = list(getattr(clf, "classes_", meta.get("class_list", [])))
        best = int(max(range(len(probs)), key=lambda i: probs[i]))
        return str(classes[best]), float(probs[best])
    return str(clf.predict(row)[0]), 1.0


__all__ = [
    "MIN_TRAINING_SESSIONS",
    "ModelNotReadyError",
    "TrainReport",
    "train",
    "predict",
]
