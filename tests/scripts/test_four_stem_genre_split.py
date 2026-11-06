"""Synthetic tests for four_stem_common genre split, cube completeness, and model parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.bench import four_stem_common as c

REPO_ROOT = Path(__file__).resolve().parents[2]


def _zeros() -> dict[str, float]:
    return {s: 0.0 for s in c.STEMS}


def _pair(v0: float, v1: float, v2: float, v3: float) -> dict[str, float]:
    return {"vocals": v0, "drums": v1, "bass": v2, "other": v3}


def test_full_cube_assert_and_genres() -> None:
    cube: dict[str, dict[str, dict[str, float]]] = {
        "T1": {
            "m1": _pair(1.0, 2.0, 3.0, 4.0),
            "m2": _pair(1.1, 2.1, 3.1, 4.1),
        },
        "T2": {
            "m1": _pair(2.0, 2.0, 3.0, 4.0),
            "m2": _pair(2.1, 2.1, 3.1, 4.1),
        },
        "T3": {
            "m1": _pair(3.0, 2.0, 3.0, 4.0),
            "m2": _pair(3.1, 2.1, 3.1, 4.1),
        },
    }
    c.assert_cube_complete(cube, ("m1", "m2"))
    with pytest.raises(RuntimeError, match="cube hole"):
        c.assert_cube_complete({"T1": {"m1": {s: 0.0 for s in c.STEMS}}}, ("m1", "m2"))
    with pytest.raises(RuntimeError, match="cube hole"):
        c.assert_cube_complete(
            {"T1": {"m1": {s: 0.0 for s in c.STEMS}, "m2": {s: 0.0 for s in c.STEMS if s != "vocals"}}},
            ("m1", "m2"),
        )


def test_missing_cell_raises_paired_by_genre() -> None:
    cube: dict[str, dict[str, dict[str, float]]] = {
        "T1": {"hdemucs_mmi": {s: 0.0 for s in c.STEMS}, "htdemucs": {s: 0.0 for s in c.STEMS}},
    }
    flags = {"T1": {"stem_passed": {s: True for s in c.STEMS}}}
    tg = {"T1": "pop"}
    with pytest.raises(RuntimeError, match="cube has a hole"):
        c.paired_deltas_by_genre(
            cube,
            flags,
            tg,
            models=("htdemucs", "hdemucs_mmi", "htdemucs_ft", "bs-roformer-viperx1296"),
        )


def test_n_ge_three_emits_delta_row() -> None:
    z = _zeros()
    cube: dict[str, dict[str, dict[str, float]]] = {
        "A": {"hdemucs_mmi": z.copy(), "htdemucs": {**z, "vocals": 1.0}},
        "B": {"hdemucs_mmi": z.copy(), "htdemucs": {**z, "vocals": 2.0}},
        "C": {"hdemucs_mmi": z.copy(), "htdemucs": {**z, "vocals": 3.0}},
    }
    flags = {t: {"stem_passed": {s: (s == "vocals") for s in c.STEMS}} for t in cube}
    tg = {t: "rock" for t in cube}
    rows = c.paired_deltas_by_genre(cube, flags, tg)
    vrows = [r for r in rows if r["stem"] == "vocals" and r["genre"] == "rock"]
    assert len(vrows) == 1
    assert vrows[0].get("status") != "insufficient_n"
    assert "mean_delta_db" in vrows[0]


def test_n_lt_three_insufficient() -> None:
    z = _zeros()
    cube: dict[str, dict[str, dict[str, float]]] = {
        "A": {"hdemucs_mmi": z.copy(), "htdemucs": {**z, "vocals": 1.0}},
        "B": {"hdemucs_mmi": z.copy(), "htdemucs": {**z, "vocals": 2.0}},
    }
    flags = {t: {"stem_passed": {s: (s == "vocals") for s in c.STEMS}} for t in cube}
    tg = {t: "metal" for t in cube}
    rows = c.paired_deltas_by_genre(cube, flags, tg)
    vrows = [r for r in rows if r["stem"] == "vocals" and r["genre"] == "metal"]
    assert len(vrows) == 1
    assert vrows[0]["status"] == "insufficient_n"
    assert vrows[0]["n_gated"] == 2
    assert "mean_delta_db" not in vrows[0]


def test_ungated_not_counted_toward_n() -> None:
    cube: dict[str, dict[str, dict[str, float]]] = {
        "A": {"hdemucs_mmi": {s: 0.0 for s in c.STEMS}, "htdemucs": {s: 0.0 for s in c.STEMS}},
        "B": {"hdemucs_mmi": {s: 0.0 for s in c.STEMS}, "htdemucs": {s: 0.0 for s in c.STEMS}},
        "C": {"hdemucs_mmi": {s: 0.0 for s in c.STEMS}, "htdemucs": {s: 0.0 for s in c.STEMS}},
    }
    flags = {
        "A": {"stem_passed": {s: (s == "drums") for s in c.STEMS}},
        "B": {"stem_passed": {s: (s == "drums") for s in c.STEMS}},
        "C": {"stem_passed": {s: False for s in c.STEMS}},
    }
    tg = {t: "electronic" for t in cube}
    rows = c.paired_deltas_by_genre(cube, flags, tg)
    drows = [r for r in rows if r["stem"] == "drums" and r["genre"] == "electronic"]
    assert len(drows) == 1
    assert drows[0]["status"] == "insufficient_n"
    assert drows[0]["n_gated"] == 2


def test_track_genres_missing_title_raises() -> None:
    with pytest.raises(RuntimeError, match="no coarse genre mapping"):
        c.track_genres(["Not A Real MUSDB Track Title For Sure"])


def test_parse_models_variants() -> None:
    assert c.parse_models(None) == c.MODELS
    acc = "htdemucs,hdemucs_mmi,htdemucs_ft,bs-roformer-viperx1296"
    assert c.parse_models(acc) == ("htdemucs", "hdemucs_mmi", "htdemucs_ft", "bs-roformer-viperx1296")
    with pytest.raises(RuntimeError, match="unknown"):
        c.parse_models("htdemucs,hdemucs_mmi,not-a-model")
    with pytest.raises(RuntimeError, match="default model"):
        c.parse_models("htdemucs,htdemucs_ft")


def test_zeno_signs_genre_is_rock() -> None:
    """Shootout blurb is 'pop rock'; coarse family must stay rock for gated counts."""
    assert c.MUSDB_TEST_GENRE["Zeno - Signs"] == "rock"


def test_gate_titles_in_genre_map() -> None:
    gate1 = REPO_ROOT / "scripts" / "bench" / "stem_content_gate.json"
    gate2 = REPO_ROOT / "scripts" / "bench" / "stem_content_gate_round2.json"
    titles: list[str] = []
    for path in (gate1, gate2):
        titles.extend(c["track"] for c in json.loads(path.read_text())["candidates"])
    for t in titles:
        assert t in c.MUSDB_TEST_GENRE, t


def test_roformer_stem_estimate() -> None:
    assert c.ROFORMER_STEM_ESTIMATE["vocals"] == "vocals"
    for s in ("drums", "bass", "other"):
        assert c.ROFORMER_STEM_ESTIMATE[s] == "instrumental"


def test_gpu_and_cpu_source_strings() -> None:
    gpu_src = (REPO_ROOT / "scripts" / "bench" / "four_stem_gpu_matrix.py").read_text()
    assert "modal_roformer_spike" in gpu_src
    assert "bs-roformer-viperx1296" in gpu_src
    cpu_src = (REPO_ROOT / "scripts" / "bench" / "four_stem_model_matrix.py").read_text()
    assert "four_stem_gpu_matrix" in cpu_src
    assert "bs-roformer-viperx1296" in cpu_src
