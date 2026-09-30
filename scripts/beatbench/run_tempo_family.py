"""Octave candidates for fast genres (drum and bass, psytrance, hardcore, footwork).

Converter-only, like `run_grid_fit.py`: reads a scored round's committed Beat
This! beats and writes one harness candidate per octave-policy variant, so
`scripts/beatbench/report.py` scores them side by side against rekordbox. Every
variant is the served line fit (`grid_fit.fit_grid`, rounding on, served offset)
and differs only in the metrical level it renders at:

  line_round_offset  the served grid (#4368): model level, no fold.
  tf_policy          rendered at `bpm.estimate_bpm`'s octave (model level wins
                     ambiguous ties; the old 112 BPM centering is gone).
  tf_fold            `tempo_family.fold_half_time` first, then model level.
  tf_fold_policy     fold, then the policy's octave. What the lane serves in
                     line mode.
  tf_fold_genre      tf_fold_policy plus the genre tempo family, only when
                     `--genres` names a stable_id -> genre-tag JSON. The tags
                     are the user's own metadata; a machine without the library
                     has none, and this row is then not written rather than
                     guessed.

`--bands` prints octave agreement with rekordbox's stored BPM per rekordbox BPM
band from a report's results.json. The band is a SCORING slice (rekordbox's own
number), never an input to any candidate.

Usage:
  python -m scripts.beatbench.run_tempo_family \\
      --candidate ops/beatbench/round-0/candidate-beat_this.json \\
      --out-dir ops/beatbench/round-4-tempo-family [--genres genres.json]
  python -m scripts.beatbench.run_tempo_family \\
      --bands ops/beatbench/round-4-tempo-family/results.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import time
from typing import Any

from apps.analysis_beatgrid.bpm import estimate_bpm
from apps.analysis_beatgrid.grid_fit import DEFAULT_OFFSET_S, fit_grid
from apps.analysis_beatgrid.tempo_family import (
    TempoFamily,
    fold_half_time,
    tempo_family_for_genre,
)

#: Rekordbox stored-BPM bands the octave table is sliced by. Drum and bass,
#: jungle and footwork live in the top band, psytrance and dubstep in 134-150.
BANDS: tuple[tuple[str, float, float], ...] = (
    ("<100", 0.0, 100.0),
    ("100-134", 100.0, 134.0),
    ("134-150", 134.0, 150.0),
    ("150-160", 150.0, 160.0),
    ("160+", 160.0, 1000.0),
)


def _failed(reason: str) -> dict[str, Any]:
    return {"beats": [], "downbeats": [], "native_bpm": None, "error": reason}


def build(
    row: dict[str, Any], *, fold: bool, policy: bool, family: TempoFamily | None = None
) -> dict[str, Any]:
    """One fixture's grid under one variant; failures are recorded, never dropped."""
    if row.get("error"):
        return _failed(str(row["error"]))
    beats = [float(t) for t in row.get("beats") or []]
    n_filled = 0
    if fold:
        folded = fold_half_time(beats)
        beats, n_filled = folded.beats, folded.n_filled
    multiple = 1.0
    reason = None
    if policy:
        tempo = estimate_bpm(beats, family=family)
        if tempo is None:
            return _failed("no_tempo_fit")
        multiple, reason = tempo.octave_multiple, tempo.octave_reason
    fit = fit_grid(
        beats, row.get("downbeats") or [], offset_s=DEFAULT_OFFSET_S, octave_multiple=multiple
    )
    if fit.reason:
        return _failed(fit.reason)
    return {
        "beats": fit.beats,
        "downbeats": [t for t, n in zip(fit.beats, fit.beat_numbers, strict=True) if n == 1],
        "native_bpm": fit.lines[0].bpm if len(fit.lines) == 1 else None,
        "n_segments": len(fit.lines),
        "octave_multiple": multiple,
        "octave_reason": reason,
        "n_half_time_filled": n_filled,
        "error": None,
    }


def write_candidates(candidate: str, out_dir: str, genres_path: str | None) -> None:
    with open(candidate, encoding="utf-8") as fh:
        source = json.load(fh)
    rows: dict[str, dict[str, Any]] = source["results"]
    genres: dict[str, str] = {}
    if genres_path:
        with open(genres_path, encoding="utf-8") as fh:
            genres = json.load(fh)
    os.makedirs(out_dir, exist_ok=True)

    variants: dict[str, dict[str, Any]] = {
        "line_round_offset": {"fold": False, "policy": False},
        "tf_policy": {"fold": False, "policy": True},
        "tf_fold": {"fold": True, "policy": False},
        "tf_fold_policy": {"fold": True, "policy": True},
    }
    for name, kw in [*variants.items(), ("tf_fold_genre", None)]:
        if kw is None:
            if not genres:
                print("[tempo_family] tf_fold_genre: no --genres given, row not written")
                continue
            results = {
                sid: build(
                    row, fold=True, policy=True, family=tempo_family_for_genre(genres.get(sid))
                )
                for sid, row in rows.items()
            }
        else:
            results = {sid: build(row, **kw) for sid, row in rows.items()}
        payload = {
            "schema": 1,
            "candidate": name,
            "candidate_version": (
                f"tempo_family over {source['candidate']} ({source['candidate_version']})"
            ),
            "source_candidate": os.path.relpath(candidate),
            "license": source["license"],
            "shippable": True,
            "emits_downbeats": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workers": 1,
            "n_fixtures": len(results),
            "n_failed": sum(1 for r in results.values() if r["error"]),
            "results": results,
        }
        path = os.path.join(out_dir, f"candidate-{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        print(f"[tempo_family] {name}: {payload['n_failed']} failed of {len(results)} -> {path}")


def band_of(bpm: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= bpm < hi:
            return name
    raise ValueError(f"no band for {bpm}")


def band_table(results_path: str) -> str:
    """Markdown: per candidate, per rekordbox BPM band, how our BPM relates to rekordbox's."""
    with open(results_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    lines = [
        "| candidate | grids | band | n | same | half | double | other | failed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cand in doc["candidates"]:
        for dynamic in (False, True):
            counts: dict[str, collections.Counter[str]] = collections.defaultdict(
                collections.Counter
            )
            for track in cand["tracks"]:
                if track["is_dynamic"] != dynamic:
                    continue
                band = band_of(float(track["rb_bpm_stored"]))
                counts[band]["n"] += 1
                if track.get("error"):
                    counts[band]["failed"] += 1
                    continue
                relation = (track.get("bpm_vs_stored") or {}).get("relation", "none")
                key = relation if relation in ("same", "half", "double") else "other"
                counts[band][key] += 1
            for band, _lo, _hi in BANDS:
                c = counts.get(band)
                if not c:
                    continue
                lines.append(
                    f"| {cand['label']} | {'dynamic' if dynamic else 'fixed'} | {band} | "
                    f"{c['n']} | {c['same']} | {c['half']} | {c['double']} | {c['other']} | "
                    f"{c['failed']} |"
                )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", help="a beat_this candidate JSON with raw beats")
    ap.add_argument("--out-dir")
    ap.add_argument("--genres", help="JSON {stable_id: genre tag} from the user's library")
    ap.add_argument("--bands", help="a report results.json to slice by rekordbox BPM band")
    args = ap.parse_args(argv)
    if args.bands:
        print(band_table(args.bands), end="")
        return 0
    if not (args.candidate and args.out_dir):
        ap.error("--candidate and --out-dir are required unless --bands is given")
    write_candidates(args.candidate, args.out_dir, args.genres)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
