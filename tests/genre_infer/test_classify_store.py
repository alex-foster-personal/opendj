"""Genre head: learns separable genres, refuses to learn noise, and never mixes embedding spaces.

Acceptance lines (GENRE-01):
  [if] embeddings separate genres [then] held-out accuracy is high ⛔️ a head that cannot fit
  [if] labels are shuffled [then] held-out accuracy falls to chance ⛔️ a CV that leaks
       training rows into its test folds and flatters every model
  [if] a genre has fewer labeled tracks than min_per_class [then] it is dropped and named
       ⛔️ predicted from a handful of examples
  [if] embeddings from two model revisions are on disk [then] only the newest revision is
       loaded ⛔️ vectors from different spaces trained together
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from apps.genre_infer import store
from apps.genre_infer.classify import GenreModel, suggest, train


def _blobs(n_per: int = 30, k: int = 4, dim: int = 64, noise: float = 2.0, seed: int = 1):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(k, dim))
    vec, lab = {}, {}
    for c in range(k):
        for i in range(n_per):
            sid = f"g{c}-{i}"
            vec[sid] = (centers[c] + rng.normal(scale=noise, size=dim)).tolist()
            lab[sid] = "abcdefgh"[c]
    return vec, lab


def test_separable_genres_are_learned_and_held_out_accuracy_says_so():
    vec, lab = _blobs()
    m = train(vec, lab, embedding_model="m", embedding_revision="r")
    assert m.cv_accuracy is not None and m.cv_accuracy > 0.9
    assert m.classes == ["a", "b", "c", "d"] and m.meta["folds"] == 5


def test_shuffled_labels_fall_to_chance():
    vec, lab = _blobs()
    keys = sorted(lab)
    perm = np.random.default_rng(0).permutation([lab[k] for k in keys])
    shuffled = dict(zip(keys, perm, strict=True))
    m = train(vec, shuffled, embedding_model="m", embedding_revision="r")
    assert m.cv_accuracy < 0.45  # chance is 0.25 across 4 balanced classes


def test_small_classes_are_dropped_and_named():
    vec, lab = _blobs()
    for i in range(25):  # leave 5 of class d
        lab.pop(f"g3-{i}")
    m = train(vec, lab, embedding_model="m", embedding_revision="r", min_per_class=8)
    assert "d" not in m.classes and m.dropped_classes == {"d": 5}


def test_fewer_than_two_classes_is_refused():
    vec, lab = _blobs(k=1)
    with pytest.raises(ValueError, match="at least 2 genres"):
        train(vec, lab, embedding_model="m", embedding_revision="r")


def test_model_round_trips_and_suggests_the_same_way():
    vec, lab = _blobs()
    m = train(vec, lab, embedding_model="m", embedding_revision="r")
    m2 = GenreModel.from_json(m.to_json())
    probe = {k: vec[k] for k in ("g0-0", "g2-3")}
    s1, s2 = suggest(m, probe), suggest(m2, probe)
    assert (
        {k: v.genre for k, v in s1.items()}
        == {k: v.genre for k, v in s2.items()}
        == {"g0-0": "a", "g2-3": "c"}
    )
    assert s1["g0-0"].top[0][0] == "a" and len(s1["g0-0"].top) == 3
    assert 0.0 < s1["g0-0"].confidence <= 1.0


def test_a_zero_vector_is_refused_not_classified():
    vec, lab = _blobs()
    m = train(vec, lab, embedding_model="m", embedding_revision="r")
    with pytest.raises(ValueError, match="zero norm"):
        suggest(m, {"z": [0.0] * 64})


def test_only_the_newest_embedding_revision_is_loaded_and_last_line_wins(tmp_path: Path):
    store.append_embeddings(
        tmp_path,
        [
            {
                "stable_id": "a",
                "model": "clap",
                "revision": "old",
                "status": "ok",
                "vector": [1.0, 0.0],
            },
            {
                "stable_id": "b",
                "model": "clap",
                "revision": "new",
                "status": "failed",
                "reason": "boom",
            },
            {
                "stable_id": "b",
                "model": "clap",
                "revision": "new",
                "status": "ok",
                "vector": [0.0, 1.0],
            },
            {
                "stable_id": "c",
                "model": "clap",
                "revision": "new",
                "status": "failed",
                "reason": "short",
            },
        ],
    )
    vectors, failures, model, revision = store.load_embeddings(tmp_path)
    assert (model, revision) == ("clap", "new")
    assert vectors == {"b": [0.0, 1.0]} and failures == {"c": "short"}


def test_no_embeddings_file_is_empty_not_an_error(tmp_path: Path):
    assert store.load_embeddings(tmp_path) == ({}, {}, None, None)
    store.write_json(store.model_path(tmp_path), {"x": 1})
    assert json.loads(store.model_path(tmp_path).read_text()) == {"x": 1}
