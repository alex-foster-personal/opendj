"""Tests for :mod:`apps.sets.classify.model` (sklearn GBC trainer)."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from apps.sets.classify import MIN_MACRO_F1_FOR_TRAINED, model as model_mod


def _make_labeled_session(
    sessions_root: Path,
    session_id: str,
    transitions: list[dict],
    labels: list[tuple[int, str]],
) -> None:
    """Write transitions.jsonl + labels.jsonl under ``sessions_root/session_id``."""
    sess_dir = sessions_root / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    with (sess_dir / "transitions.jsonl").open("w") as fh:
        for t in transitions:
            fh.write(json.dumps(t) + "\n")
    with (sess_dir / "labels.jsonl").open("w") as fh:
        for idx, cls in labels:
            fh.write(json.dumps({"idx": idx, "class": cls}) + "\n")


def _synthetic_corpus(sessions_root: Path, n_sessions: int = 5, seed: int = 0) -> list[str]:
    """Emit a corpus with clear class signals so GBC learns well."""
    rng = random.Random(seed)
    ids: list[str] = []
    for s in range(n_sessions):
        sid = f"session-{s:02d}"
        transitions = []
        labels = []
        per_class_samples = 4
        classes = ["cut", "blend", "quick_double"]
        idx = 0
        for cls in classes:
            for _ in range(per_class_samples):
                if cls == "cut":
                    overlap = rng.uniform(0.0, 0.3)
                    is_reload = 0.0
                    fade = rng.uniform(0.0, 0.2)
                elif cls == "blend":
                    overlap = rng.uniform(6.0, 20.0)
                    is_reload = 0.0
                    fade = rng.uniform(3.0, 8.0)
                else:  # quick_double
                    overlap = rng.uniform(0.5, 2.0)
                    is_reload = 1.0
                    fade = rng.uniform(0.0, 0.8)
                transitions.append(
                    {
                        "idx": idx,
                        "features": {
                            "overlap_s": overlap,
                            "fade_s": fade,
                            "incoming_preload_s": 0.0,
                            "outgoing_trail_s": fade,
                            "time_since_prev_transition_s": 60.0,
                            "is_same_deck_reload": is_reload,
                        },
                    }
                )
                labels.append((idx, cls))
                idx += 1
        _make_labeled_session(sessions_root, sid, transitions, labels)
        ids.append(sid)
    return ids


@pytest.mark.requirement("SET-02")
def test_train_aborts_with_too_few_sessions(tmp_path: Path):
    ids = _synthetic_corpus(tmp_path, n_sessions=2)
    report = model_mod.train(
        ids,
        sessions_root=tmp_path,
        models_dir=tmp_path / "models",
    )
    assert report.accepted is False
    assert report.n_training_sessions == 2
    assert report.model_path is None


@pytest.mark.requirement("SET-02")
def test_train_produces_joblib_and_meta_when_gate_passes(tmp_path: Path):
    ids = _synthetic_corpus(tmp_path, n_sessions=5)
    report = model_mod.train(
        ids,
        sessions_root=tmp_path,
        models_dir=tmp_path / "models",
    )
    assert report.accepted is True, f"expected acceptance, macro_f1={report.macro_f1}"
    assert report.macro_f1 > MIN_MACRO_F1_FOR_TRAINED
    assert report.model_path is not None and report.model_path.exists()
    assert report.meta_path is not None and report.meta_path.exists()
    meta = json.loads(report.meta_path.read_text())
    assert meta["algorithm"].startswith("sklearn")
    assert "overlap_s" in meta["feature_columns"]
    assert set(meta["class_list"]) >= {"cut", "blend", "quick_double"}


@pytest.mark.requirement("SET-02")
def test_predict_returns_class_and_confidence(tmp_path: Path):
    ids = _synthetic_corpus(tmp_path, n_sessions=5)
    report = model_mod.train(
        ids, sessions_root=tmp_path, models_dir=tmp_path / "models"
    )
    assert report.accepted
    from apps.sets.transitions import Transition

    t = Transition(
        idx=0, t_start_s=0.0, t_change_s=10.0, t_end_s=10.1,
        from_deck="A", to_deck="B", from_track=None, to_track=None,
    )
    t.features = {
        "overlap_s": 0.1,
        "fade_s": 0.0,
        "incoming_preload_s": 0.0,
        "outgoing_trail_s": 0.0,
        "time_since_prev_transition_s": 30.0,
        "is_same_deck_reload": 0.0,
    }
    cls, conf = model_mod.predict(t, models_dir=tmp_path / "models")
    assert cls in {"cut", "blend", "quick_double"}
    assert 0.0 < conf <= 1.0


@pytest.mark.requirement("SET-02")
def test_predict_raises_when_no_model(tmp_path: Path):
    from apps.sets.transitions import Transition

    t = Transition(
        idx=0, t_start_s=0.0, t_change_s=10.0, t_end_s=10.0,
        from_deck="A", to_deck="B", from_track=None, to_track=None,
    )
    with pytest.raises(model_mod.ModelNotReadyError):
        model_mod.predict(t, models_dir=tmp_path / "empty-models")


@pytest.mark.requirement("SET-02")
def test_train_skips_sessions_without_labels(tmp_path: Path):
    """A session with transitions but no labels is excluded from the count."""
    # Only 1 labeled session, 4 unlabeled -> below MIN_TRAINING_SESSIONS.
    _synthetic_corpus(tmp_path, n_sessions=1)  # labeled
    for i in range(1, 5):  # transitions only, no labels
        sess = tmp_path / f"unlabeled-{i}"
        sess.mkdir()
        (sess / "transitions.jsonl").write_text(
            json.dumps({"idx": 0, "features": {"overlap_s": 1.0}}) + "\n"
        )
    ids = [f"session-00"] + [f"unlabeled-{i}" for i in range(1, 5)]
    report = model_mod.train(ids, sessions_root=tmp_path, models_dir=tmp_path / "m")
    assert report.n_training_sessions == 1
    assert report.accepted is False
