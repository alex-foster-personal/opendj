"""Shared, dependency-free logic for the four-stem model bake-off.

Both runners need the same arm list, the same gate reading and the same paired
statistic, and they run in DIFFERENT environments: the CPU runner is a PEP 723
script that pulls torch, the GPU runner executes inside the repo venv next to
modal. Anything they share therefore has to import with nothing but the standard
library, which is why this module is separate rather than living in either one.

Two runners disagreeing about which model is the default, or about which cells
are gated, would be a silent confound rather than a visible error.

-Claude
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")

# The three tiers' models (apps/stems/tiers.py): LOCAL=htdemucs, M=hdemucs_mmi,
# L=htdemucs_ft. hdemucs_mmi is the incumbent default and is therefore the
# reference every paired delta is taken against.
DEFAULT_MODEL: str = "hdemucs_mmi"
MODELS: tuple[str, ...] = ("hdemucs_mmi", "htdemucs", "htdemucs_ft")
OVERLAP: float = 0.25
SHIFTS: int = 0

# RoFormer arm (2-stem MDXC); GPU matrix only -- see four_stem_gpu_matrix.py.
ROFORMER_VIPERX_1296: str = "bs-roformer-viperx1296"
ROFORMER_ARMS: frozenset[str] = frozenset({ROFORMER_VIPERX_1296})
KNOWN_MODELS: tuple[str, ...] = (*MODELS, ROFORMER_VIPERX_1296)

# How RoFormer fills the four-stem cube vs TRUE stems (instrumental scored
# against drums, bass, and other separately).
ROFORMER_STEM_ESTIMATE: dict[str, str] = {
    "vocals": "vocals",
    "drums": "instrumental",
    "bass": "instrumental",
    "other": "instrumental",
}

MIN_GATED_PER_GENRE: int = 3

# This project's audibility bar. A delta under it is not a reason to change
# anything, and one over it must not be hidden.
AUDIBILITY_DB: float = 0.2


def _leading_genre_token(genre_blurb: str) -> str:
    """First comma-separated token from a shootout_spec genre blurb."""
    return genre_blurb.split(",", 1)[0].strip().lower()


def _coarse_genre_from_blurb(genre_blurb: str) -> str:
    """Map shootout_spec's per-title blurb to a coarse family (parent #749)."""
    token = _leading_genre_token(genre_blurb)
    # Order matters for prefixes (e.g. electronic dance before electronic).
    if token.startswith(("hip-hop", "hip hop")):
        return "hip-hop"
    if token.startswith("metal"):
        return "metal"
    if "electronic" in token or token.startswith("edm"):
        return "electronic"
    # "pop rock" must classify as rock (genre n>=3); check rock before generic pop.
    if "rock" in token or token.startswith(("blues", "industrial")):
        return "rock"
    if "pop" in token and "hip-hop" not in token and "hip hop" not in token:
        return "pop"
    if token.startswith(("folk", "irish", "tamil")):
        return "folk"
    return token.replace(" ", "-")


def _load_shootout_spec():
    bench_dir = Path(__file__).resolve().parent
    name = "_four_stem_shootout_spec_bridge"
    spec = importlib.util.spec_from_file_location(name, bench_dir / "shootout_spec.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load shootout_spec.py for genre map")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_musdb_test_genre() -> dict[str, str]:
    """Every gated candidate title -> coarse genre; shootout_spec is the source
    for the twelve listed tracks; seven additional gated titles are explicit."""
    mod = _load_shootout_spec()

    out: dict[str, str] = {}
    for tr in mod.TRACKS:
        out[tr.title] = _coarse_genre_from_blurb(tr.genre)

    # Seven gated titles not in shootout_spec.TRACKS (exact strings = gate JSON).
    extra: dict[str, str] = {
        "Detsky Sad - Walkie Talkie": "pop",
        "The Easton Ellises - Falcon 69": "pop",
        "Triviul feat. The Fiend - Widow": "electronic",
        "Arise - Run Run Run": "rock",
        "BKS - Bulldozer": "electronic",
        "BKS - Too Much": "electronic",
        "Ben Carrigan - We'll Talk About It All Tonight": "pop",
        "Bobby Nobody - Stitch Up": "pop",
    }
    for title, g in extra.items():
        if title in out and out[title] != g:
            raise RuntimeError(
                f"MUSDB_TEST_GENRE conflict for {title!r}: shootout-derived {out[title]!r} vs explicit {g!r}"
            )
        out[title] = g
    return out


MUSDB_TEST_GENRE: dict[str, str] = _build_musdb_test_genre()
del _build_musdb_test_genre


def gate_flags(*gate_jsons: Path) -> dict[str, dict[str, Any]]:
    """Per (track, stem) pass/fail, READ from the gate's own verdict files.

    Read, not recomputed: the gate chose these windows before any model score
    existed, and re-deriving the flags here would let a runner move a bar after
    seeing a result.

    Several files are accepted because the sample was widened in rounds, and a
    round-2 track's verdict lives in its own file. A track appearing twice is an
    error rather than a last-one-wins merge: two gate rows for one track means
    two different windows, and silently picking one would decide which window a
    score belongs to by file order.
    """
    out: dict[str, dict[str, Any]] = {}
    for gate_json in gate_jsons:
        path = Path(gate_json)
        if not path.is_file():
            continue
        for candidate in json.loads(path.read_text())["candidates"]:
            if candidate["track"] in out:
                raise RuntimeError(
                    f"{candidate['track']} has a gate row in more than one file; "
                    "refusing to guess which window its scores belong to"
                )
            _add_flag(out, candidate)
    return out


def _add_flag(out: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    failures = candidate["failures"]
    out[candidate["track"]] = {
        "window_start_s": candidate["start_s"],
        "window_length_s": candidate["length_s"],
        "track_passed": candidate["passed"],
        "stem_passed": {s: s not in failures for s in STEMS},
        "stem_failures": {s: failures.get(s, []) for s in STEMS},
        "mixture_floor_si_sdr": candidate["mixture_floor_si_sdr"],
        "stem_energy_share": {s["stem"]: s["energy_share"] for s in candidate["stems"]},
        "stem_spectral_centroid_hz": {s["stem"]: s["spectral_centroid_hz"] for s in candidate["stems"]},
    }


def parse_models(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated model list; default is the three demucs arms."""
    if raw is None or not raw.strip():
        return MODELS
    parts = [p.strip() for p in raw.split(",")]
    names = [p for p in parts if p]
    if not names:
        return MODELS
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n in seen:
            raise RuntimeError(f"duplicate model in --models: {n!r}")
        seen.add(n)
        ordered.append(n)
    unknown = [m for m in ordered if m not in KNOWN_MODELS]
    if unknown:
        raise RuntimeError(f"unknown model name(s) {unknown}; known: {list(KNOWN_MODELS)}")
    if DEFAULT_MODEL not in ordered:
        raise RuntimeError(f"default model {DEFAULT_MODEL!r} must appear in --models for paired deltas")
    return tuple(ordered)


def track_genres(titles: Iterable[str]) -> dict[str, str]:
    """Map track titles to coarse genre; missing title raises."""
    out: dict[str, str] = {}
    for t in titles:
        if t not in MUSDB_TEST_GENRE:
            raise RuntimeError(f"no coarse genre mapping for track {t!r}; extend MUSDB_TEST_GENRE")
        out[t] = MUSDB_TEST_GENRE[t]
    return out


def assert_cube_complete(
    cube: dict[str, dict[str, dict[str, float]]],
    models: tuple[str, ...],
    stems: tuple[str, ...] = STEMS,
) -> None:
    """Every track has every model and every stem; else RuntimeError."""
    for track, by_model in cube.items():
        for model in models:
            if model not in by_model:
                raise RuntimeError(f"cube hole: {track!r} missing model {model!r}")
            for stem in stems:
                if stem not in by_model[model]:
                    raise RuntimeError(f"cube hole: {track!r}/{model!r} missing stem {stem!r}")


def _model_order_for_deltas(cube: dict[str, dict[str, dict[str, float]]], models: tuple[str, ...] | None) -> list[str]:
    if models is not None:
        return list(models)
    found: set[str] = set()
    for by_m in cube.values():
        found.update(by_m.keys())
    rest = sorted(m for m in found if m != DEFAULT_MODEL)
    if DEFAULT_MODEL in found:
        return [DEFAULT_MODEL, *rest]
    raise RuntimeError(f"cube missing default model {DEFAULT_MODEL!r}")


def paired_deltas(
    cube: dict[str, dict[str, dict[str, float]]],
    flags: dict[str, dict[str, Any]],
    models: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Per (model, stem) paired delta against DEFAULT_MODEL, gated cells only.

    PAIRED because every model separated the identical window, so track
    difficulty -- which spans roughly 15 dB across this sample, far more than any
    gap between models -- cancels. An unpaired difference of medians would be
    dominated by that spread and can reverse the per-track sign.

    Ungated cells are carried in ``per_track_ungated`` rather than dropped: a
    stem buried in its own window scores toward the mixture floor, so its delta
    is not evidence, but hiding it would be a silent scope cut.
    """
    model_order = _model_order_for_deltas(cube, models)
    rows: list[dict[str, Any]] = []
    for model in model_order:
        if model == DEFAULT_MODEL:
            continue
        for stem in STEMS:
            gated: dict[str, float] = {}
            ungated: dict[str, float] = {}
            for track in sorted(cube):
                if model not in cube[track] or DEFAULT_MODEL not in cube[track]:
                    raise RuntimeError(f"{track} is missing a model, the cube has a hole")
                delta = round(cube[track][model][stem] - cube[track][DEFAULT_MODEL][stem], 3)
                target = gated if flags[track]["stem_passed"][stem] else ungated
                target[track] = delta
            values = list(gated.values())
            if not values:
                raise RuntimeError(f"no gated track for stem {stem}, cannot conclude")
            mean = statistics.fmean(values)
            sd = statistics.stdev(values) if len(values) > 1 else 0.0
            rows.append(
                {
                    "model": model,
                    "vs": DEFAULT_MODEL,
                    "stem": stem,
                    "n_gated": len(values),
                    "mean_delta_db": round(mean, 3),
                    "median_delta_db": round(statistics.median(values), 3),
                    "sd": round(sd, 3),
                    "t": round(mean / (sd / len(values) ** 0.5), 2) if sd > 0 else None,
                    "min_delta_db": round(min(values), 3),
                    "max_delta_db": round(max(values), 3),
                    "tracks_favouring_challenger": sum(1 for v in values if v > 0),
                    "beats_audibility_bar": abs(mean) >= AUDIBILITY_DB,
                    "per_track_gated": gated,
                    "per_track_ungated": ungated,
                }
            )
    return rows


def paired_deltas_by_genre(
    cube: dict[str, dict[str, dict[str, float]]],
    flags: dict[str, dict[str, Any]],
    track_genre: dict[str, str],
    models: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Paired deltas vs DEFAULT_MODEL per (challenger, stem, genre), gated only.

    If a (model, stem, genre) has fewer than MIN_GATED_PER_GENRE gated tracks,
    emit status insufficient_n instead of mean/median/t.
    """
    model_order = _model_order_for_deltas(cube, models)
    genres = sorted(set(track_genre[t] for t in cube))
    rows: list[dict[str, Any]] = []
    for model in model_order:
        if model == DEFAULT_MODEL:
            continue
        for stem in STEMS:
            for genre in genres:
                gated: dict[str, float] = {}
                ungated: dict[str, float] = {}
                for track in sorted(cube):
                    if track_genre.get(track) != genre:
                        continue
                    if model not in cube[track] or DEFAULT_MODEL not in cube[track]:
                        raise RuntimeError(f"{track} is missing a model, the cube has a hole")
                    delta = round(cube[track][model][stem] - cube[track][DEFAULT_MODEL][stem], 3)
                    target = gated if flags[track]["stem_passed"][stem] else ungated
                    target[track] = delta
                n_g = len(gated)
                base: dict[str, Any] = {
                    "model": model,
                    "vs": DEFAULT_MODEL,
                    "stem": stem,
                    "genre": genre,
                    "n_gated": n_g,
                }
                if n_g < MIN_GATED_PER_GENRE:
                    base["status"] = "insufficient_n"
                    base["per_track_ungated"] = ungated
                    rows.append(base)
                    continue
                values = list(gated.values())
                mean = statistics.fmean(values)
                sd = statistics.stdev(values) if len(values) > 1 else 0.0
                rows.append(
                    {
                        **base,
                        "mean_delta_db": round(mean, 3),
                        "median_delta_db": round(statistics.median(values), 3),
                        "sd": round(sd, 3),
                        "t": round(mean / (sd / len(values) ** 0.5), 2) if sd > 0 else None,
                        "min_delta_db": round(min(values), 3),
                        "max_delta_db": round(max(values), 3),
                        "tracks_favouring_challenger": sum(1 for v in values if v > 0),
                        "beats_audibility_bar": abs(mean) >= AUDIBILITY_DB,
                        "per_track_gated": gated,
                        "per_track_ungated": ungated,
                    }
                )
    return rows


def iter_gate_candidate_tracks(*gate_jsons: Path) -> list[str]:
    """All candidate track titles from gate JSON files."""
    titles: list[str] = []
    for gate_json in gate_jsons:
        path = Path(gate_json)
        if not path.is_file():
            continue
        titles.extend(c["track"] for c in json.loads(path.read_text())["candidates"])
    return titles
