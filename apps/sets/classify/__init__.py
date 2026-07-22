"""Transition classifier (rules + optional trained model).

Plan 12-02. ``classify_session`` is the single entry point; it picks
the trained model if a sufficiently-accurate joblib file is present,
falling back to the rules-based classifier otherwise.

v1 classes per CONTEXT D4::

    cut, blend, filter_sweep, fx, quick_double, unknown

Rules output a subset (``cut, blend, quick_double, unknown``); a
trained model trained on hand labels can reach any of the five.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .. import paths as sets_paths
from .. import transitions as transitions_mod
from ..state import SetsState
from . import rules as _rules

logger = logging.getLogger(__name__)

CLASS_LIST: tuple[str, ...] = (
    "cut",
    "blend",
    "filter_sweep",
    "fx",
    "quick_double",
    "unknown",
)

MIN_MACRO_F1_FOR_TRAINED: float = 0.6

MODEL_FILE_NAME = "transition_classifier.joblib"
META_FILE_NAME = "transition_classifier.meta.json"


def _load_trained_model(models_dir: Path) -> tuple[Any | None, dict[str, Any] | None]:
    """Return ``(clf, meta)`` only if a model passes the macro-F1 gate."""
    model_path = models_dir / MODEL_FILE_NAME
    meta_path = models_dir / META_FILE_NAME
    if not model_path.exists() or not meta_path.exists():
        return None, None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    macro_f1 = float(meta.get("macro_f1", 0.0))
    if macro_f1 < MIN_MACRO_F1_FOR_TRAINED:
        logger.info(
            "trained model macro_f1=%.3f below gate %.2f; rules fallback",
            macro_f1,
            MIN_MACRO_F1_FOR_TRAINED,
        )
        return None, meta
    try:
        import joblib
    except ImportError as exc:  # pragma: no cover -- soft dep
        logger.warning("joblib unavailable (%s); rules fallback", exc)
        return None, meta
    try:
        clf = joblib.load(model_path)
    except Exception as exc:  # pragma: no cover
        logger.warning("trained model load failed (%s); rules fallback", exc)
        return None, meta
    return clf, meta


def classify_session(
    session_id: str,
    *,
    state: SetsState | None = None,
    transitions: list[transitions_mod.Transition] | None = None,
    models_dir: Path | None = None,
    force_rules: bool = False,
    output_path: Path | None = None,
    sets_root: Path | None = None,
) -> Path:
    """Classify every transition and write ``transitions.jsonl``.

    Returns the output path. If ``force_rules`` is True, the
    rules-based classifier is used even when a trained model is
    present.
    """
    if transitions is None:
        transitions = transitions_mod.find_transitions(session_id, state=state)

    models = Path(models_dir) if models_dir is not None else sets_paths.MODELS_DIR
    trained_clf = None
    meta: dict[str, Any] | None = None
    model_version = "rules-v0"
    if not force_rules:
        trained_clf, meta = _load_trained_model(models)
        if trained_clf is not None and meta is not None:
            model_version = f"sklearn-gbc-{meta.get('trained_at', '?')}"

    rows: list[dict[str, Any]] = []
    for t in transitions:
        if trained_clf is not None:
            predicted_class, confidence = _predict_with_model(trained_clf, t, meta)
        else:
            predicted_class, confidence = _rules.classify(t)
        rows.append(_transition_to_row(t, predicted_class, confidence, model_version))

    target = _resolve_output_path(session_id, output_path, sets_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True))
            fh.write("\n")
    return target


def _predict_with_model(
    clf: Any,
    transition: transitions_mod.Transition,
    meta: dict[str, Any] | None,
) -> tuple[str, float]:
    feature_cols: list[str] = list(
        (meta or {}).get("feature_columns", transitions_mod.FEATURE_COLUMNS)
    )
    row = [[float(transition.features.get(col, 0.0)) for col in feature_cols]]
    probs = None
    if hasattr(clf, "predict_proba"):
        try:
            probs = clf.predict_proba(row)[0]
        except Exception:
            probs = None
    if probs is not None:
        classes = list(getattr(clf, "classes_", CLASS_LIST))
        best_idx = int(max(range(len(probs)), key=lambda i: probs[i]))
        return str(classes[best_idx]), float(probs[best_idx])
    return str(clf.predict(row)[0]), 1.0


def _transition_to_row(
    t: transitions_mod.Transition,
    predicted_class: str,
    confidence: float,
    model_version: str,
) -> dict[str, Any]:
    row = asdict(t)
    row["predicted_class"] = predicted_class
    row["confidence"] = round(float(confidence), 4)
    row["model_version"] = model_version
    return row


def _resolve_output_path(
    session_id: str,
    output_path: Path | None,
    sets_root: Path | None,
) -> Path:
    if output_path is not None:
        return Path(output_path)
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    return sets_paths.session_dir(session_id, root=root) / "transitions.jsonl"


def read_transitions(session_id: str, *, sets_root: Path | None = None) -> list[dict[str, Any]]:
    """Read a previously-written transitions.jsonl for ``session_id``."""
    path = _resolve_output_path(session_id, None, sets_root)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


__all__ = [
    "CLASS_LIST",
    "MIN_MACRO_F1_FOR_TRAINED",
    "MODEL_FILE_NAME",
    "META_FILE_NAME",
    "classify_session",
    "read_transitions",
]
