"""Bradley-Terry fit of the calibration session (TRYER-23), on synthetic sessions with known weights.

[if] a session is fitted [then] weights, held-out agreement, tau and the post-screen are measured, [else stop].

- if the fit cannot recover the signs and order of known weights then broken
- if random picks score well above chance held-out agreement then broken
- if a session with the anchor rated Good on 20% of pairs is not VOID then broken
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scripts.bench.tryer_calibration.fit as fit_mod
from apps.webui.server.routes import bench
from scripts.bench.tryer_calibration.fit import FitError, fit_session, kendall_tau, main
from tests.bench_tryer_calibration.synthetic import make_factorial_session, make_session

pytestmark = pytest.mark.requirement("TRYER-23")

KNOWN = {"cue_phrase_aligned": 2.0, "overlap_bars_log": -1.0, "style_bass_swap": 0.5}


def test_recovers_sign_and_order_of_known_weights() -> None:
    """[if] rankings come from known weights [then] fitted signs and |order| match them, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=200, seed=7)
    report = fit_session(ratings, key)
    assert not report.void
    for name, truth in KNOWN.items():
        assert report.weights[name] * truth > 0, (name, report.weights)
    by_size = sorted(report.weights, key=lambda n: -abs(report.weights[n]))
    assert by_size == sorted(KNOWN, key=lambda n: -abs(KNOWN[n]))
    assert report.heldout_agreement is not None and report.heldout_agreement > 0.7
    assert report.kendall_tau_composite is not None and report.kendall_tau_composite > 0.4


def test_random_picks_give_near_chance_agreement() -> None:
    """[if] picks are random [then] held-out agreement sits near 0.5, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=200, seed=11, random_picks=True)
    report = fit_session(ratings, key)
    assert report.heldout_agreement is not None
    assert 0.4 < report.heldout_agreement < 0.6, report.heldout_agreement


def test_anchor_rated_good_too_often_voids_the_session(tmp_path) -> None:
    """[if] the low anchor is Good on more than 15% of pairs [then] the report is VOID and the CLI exits 2, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=20, seed=3, anchor_good_pairs=4)
    assert fit_session(ratings, key).void
    ok_ratings, ok_key = make_session(KNOWN, pairs=20, seed=3, anchor_good_pairs=3)
    assert not fit_session(ok_ratings, ok_key).void
    (tmp_path / "r.json").write_text(json.dumps(ratings), encoding="utf-8", newline="\n")
    (tmp_path / "k.json").write_text(json.dumps(key), encoding="utf-8", newline="\n")
    assert main(["--ratings", str(tmp_path / "r.json"), "--key", str(tmp_path / "k.json")]) == 2


def test_unknown_candidate_and_missing_anchor_fail_loud() -> None:
    """[if] a rated id is not in the key, or a pair has no anchor [then] FitError, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=10, seed=1)
    del key["candidates"]["p000-top"]
    with pytest.raises(FitError, match="not in the key"):
        fit_session(ratings, key)
    ratings, key = make_session(KNOWN, pairs=10, seed=1)
    ratings["ratings"] = [r for r in ratings["ratings"] if r["id"] != "p000-anchor"]
    with pytest.raises(FitError, match=r"anchor|!= key"):
        fit_session(ratings, key)


def test_kendall_tau_bounds() -> None:
    """[if] scores agree or invert the human ranks [then] tau is +1 or -1, [else stop]."""
    assert kendall_tau([3.0, 2.0, 1.0], [1, 2, 3]) == 1.0
    assert kendall_tau([1.0, 2.0, 3.0], [1, 2, 3]) == -1.0
    assert kendall_tau([1.0], [1]) is None


def test_ratings_endpoint_accepts_a_session_artifact(tmp_path, monkeypatch) -> None:
    """[if] the rater's artifact is POSTed to /bench/ratings [then] it is written and reads back for the fit, [else stop]."""
    monkeypatch.setattr(bench, "RATINGS_DIR", tmp_path)
    app = FastAPI()
    app.include_router(bench.router)
    ratings, key = make_session(KNOWN, pairs=12, seed=5)
    response = TestClient(app).post("/bench/ratings", json=ratings)
    assert response.status_code == 201, response.text
    saved = json.loads((tmp_path / response.json()["filename"]).read_text(encoding="utf-8"))
    assert fit_session(saved, key).comparisons == 36


def test_untouched_pairs_are_skipped_but_half_rated_pairs_fail() -> None:
    """[if] a partial save leaves whole pairs unrated [then] they are skipped, while a half-rated pair fails loud, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=12, seed=9)
    for entry in ratings["ratings"]:
        note = json.loads(entry["note"])
        if note["pair_id"] in {"p000", "p001"}:
            entry["note"] = json.dumps({**note, "rank": None})
            entry["verdict"] = None
    assert fit_session(ratings, key).pairs == 10
    note = json.loads(ratings["ratings"][8]["note"])
    ratings["ratings"][8]["note"] = json.dumps({**note, "rank": None})
    with pytest.raises(FitError, match="no rank or verdict"):
        fit_session(ratings, key)


def test_factorial_sessions_recover_family_and_exit_effects_independently() -> None:
    """[if] only family or only exit bar drives the picks [then] the fit recovers that effect and keeps the other near zero, [else stop]."""
    family_only = make_factorial_session({"blend": 1.5, "crossfade": 0.0, "filter": -1.5}, 0.0, pairs=300, seed=21)
    effects = fit_session(*family_only).effects
    assert list(effects["style_family"]) == ["blend", "crossfade", "filter"]
    assert abs(effects["exit_bar_per_phrase"]) < 0.05
    exit_only = make_factorial_session({"blend": 0.0, "crossfade": 0.0, "filter": 0.0}, 0.3, pairs=300, seed=22)
    effects = fit_session(*exit_only).effects
    assert effects["exit_bar_per_phrase"] > 0.15
    assert max(abs(w) for w in effects["style_family"].values()) < 0.3


def test_touched_pair_must_match_the_key_with_unique_ranks() -> None:
    """[if] a touched pair drops a candidate or repeats a rank [then] FitError, never a fit, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=12, seed=13)
    ratings["ratings"] = [r for r in ratings["ratings"] if r["id"] != "p003-mid"]
    with pytest.raises(FitError, match="!= key"):
        fit_session(ratings, key)
    ratings, key = make_session(KNOWN, pairs=12, seed=13)
    first, second = ratings["ratings"][0], ratings["ratings"][1]
    second["note"] = json.dumps({**json.loads(second["note"]), "rank": json.loads(first["note"])["rank"]})
    with pytest.raises(FitError, match="not exactly"):
        fit_session(ratings, key)


def test_ratings_with_no_rated_pair_fail_loud() -> None:
    """[if] a save carries no rank or verdict at all [then] FitError, never a division by zero, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=4, seed=2)
    for entry in ratings["ratings"]:
        entry["note"] = json.dumps({**json.loads(entry["note"]), "rank": None})
        entry["verdict"] = None
    with pytest.raises(FitError, match="no pair is rated"):
        fit_session(ratings, key)


def test_heldout_scaling_comes_from_training_pairs_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] held-out deltas are scaled up [then] fold training never sees them: scaling uses the training rows only, [else stop]."""
    seen: list[np.ndarray] = []
    real = fit_mod.fit_weights

    def spy(deltas: np.ndarray, ridge: float = fit_mod.CFG.RIDGE) -> np.ndarray:
        seen.append(deltas.copy())
        return real(deltas, ridge)

    monkeypatch.setattr(fit_mod, "fit_weights", spy)
    rng = np.random.default_rng(0)
    deltas = rng.normal(size=(40, 2))
    owners = [f"p{i // 4}" for i in range(40)]
    deltas[np.array([o == "p0" for o in owners])] *= 1000.0  # p0 lands in fold 0's held-out set
    fit_mod.heldout_agreement(deltas, owners)
    assert seen[0].std(axis=0).max() == pytest.approx(1.0, rel=1e-6)


def test_model_tau_never_imputes_a_missing_feature() -> None:
    """[if] a feature is missing on one candidate [then] tau scores it as training does (no evidence), never as 0, [else stop]."""

    def cand(cid: str, rank: int, features: dict[str, float]) -> fit_mod.RatedCandidate:
        return fit_mod.RatedCandidate(cid, "p0", "taste", rank, "ok", features, None)

    # y is missing on the human's winner. Imputing y = 0 would rank the loser (y = 9) first; the trained model
    # drops y from that comparison and keeps x, which agrees with the human.
    grouped = {"p0": [cand("w", 1, {"x": 2.0}), cand("l", 2, {"x": 1.0, "y": 9.0})]}
    assert fit_mod._mean_model_tau(grouped, {"x": 1.0, "y": 1.0}) == 1.0
    assert fit_mod.pairwise_tau([-1.0], [1]) == -1.0


def test_ratings_endpoint_accepts_a_null_machine_score(tmp_path, monkeypatch) -> None:
    """[if] a rater with no machine score posts machine_score null [then] it is saved as null, never a placeholder, [else stop]."""
    monkeypatch.setattr(bench, "RATINGS_DIR", tmp_path)
    app = FastAPI()
    app.include_router(bench.router)
    ratings, _ = make_session(KNOWN, pairs=2, seed=4)
    response = TestClient(app).post("/bench/ratings", json=ratings)
    assert response.status_code == 201, response.text
    saved = json.loads((tmp_path / response.json()["filename"]).read_text(encoding="utf-8"))
    assert {r["machine_score"] for r in saved["ratings"]} == {None}


def test_coverage_counts_presence_not_nonzero_deltas() -> None:
    """[if] a feature is measured on both sides with equal values [then] it counts as covered but not as differing, [else stop]."""
    ratings, key = make_session({"x": 1.0, "tie": 0.0}, pairs=12, seed=8)
    for cid, meta in key["candidates"].items():
        meta["features"] = {**meta["features"], "tie": 2.0 if cid == "p000-top" else 1.0}
    report = fit_session(ratings, key)
    assert report.feature_coverage["tie"] == report.comparisons
    assert report.feature_nonzero_deltas["tie"] == 2  # only p000-top differs: its 2 comparisons


def test_a_constant_nonzero_delta_is_kept_and_carries_its_effect() -> None:
    """[if] every comparison has the same nonzero delta on a feature [then] it is kept, not dropped, and fits a positive weight, [else stop]."""
    deltas = np.array([[1.0, 0.0], [1.0, 0.5], [1.0, -0.5], [1.0, 0.25]])
    scale = fit_mod.feature_scale(deltas)
    assert scale[0] == pytest.approx(1.0), "a zero-spread nonzero column is scaled by its mean absolute delta"
    _, _, dropped, keep = fit_mod._usable_features(["family", "x"], deltas, np.ones_like(deltas, dtype=bool))
    assert dropped == {} and keep == [0, 1]
    assert fit_mod.fit_weights(deltas / scale)[0] > 0
    zero = np.array([[0.0, 1.0], [0.0, 2.0]])
    assert fit_mod._usable_features(["z", "x"], zero, np.ones_like(zero, dtype=bool))[2] == {
        "z": "no non-zero delta in any comparison"
    }


def test_untouched_pairs_still_validate_their_ids_against_the_key() -> None:
    """[if] an unrated pair carries an id missing from the key or a pair mismatch [then] FitError, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=12, seed=9)
    for entry in ratings["ratings"]:
        note = json.loads(entry["note"])
        if note["pair_id"] == "p000":
            entry["note"] = json.dumps({**note, "rank": None})
            entry["verdict"] = None
    assert fit_session(ratings, key).pairs == 11  # control: a clean partial save fits
    stale = next(e for e in ratings["ratings"] if json.loads(e["note"])["pair_id"] == "p000")
    original_id = stale["id"]
    stale["id"] = "ghost"
    with pytest.raises(FitError, match="not in the key"):
        fit_session(ratings, key)
    stale["id"] = original_id
    stale["note"] = json.dumps({**json.loads(stale["note"]), "pair_id": "p001"})
    with pytest.raises(FitError, match="key says pair"):
        fit_session(ratings, key)


@pytest.mark.parametrize("bad_rank", [True, "1", 1.5, 0, 99])
def test_a_rank_that_is_not_an_integer_in_range_is_refused_never_coerced(bad_rank: object) -> None:
    """[if] a rank is a bool, a string, a fraction or out of range [then] FitError, never int() coercion, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=12, seed=9)
    assert fit_session(ratings, key).pairs == 12  # control: integer ranks fit
    note = json.loads(ratings["ratings"][0]["note"])
    ratings["ratings"][0]["note"] = json.dumps({**note, "rank": bad_rank})
    with pytest.raises(FitError, match="not an integer"):
        fit_session(ratings, key)


def test_the_fit_reads_the_rating_verdict_field_and_refuses_a_note_copy() -> None:
    """[if] a verdict sits in the note as well [then] FitError, and the post-screen reads the rating's own verdict
    field, the one the endpoint counts, [else stop]."""
    ratings, key = make_session(KNOWN, pairs=12, seed=9)
    baseline = fit_session(ratings, key)  # control: the verdict field alone fits
    entry = ratings["ratings"][0]
    entry["note"] = json.dumps({**json.loads(entry["note"]), "verdict": entry["verdict"]})
    with pytest.raises(FitError, match="verdict field"):
        fit_session(ratings, key)
    entry["note"] = json.dumps({k: v for k, v in json.loads(entry["note"]).items() if k != "verdict"})
    for anchor in (e for e in ratings["ratings"] if key["candidates"][e["id"]]["role"] == "anchor"):
        anchor["verdict"] = "good"  # the field, not a note, decides the anchor share
    assert fit_session(ratings, key).anchor_good_share == 1.0 != baseline.anchor_good_share
