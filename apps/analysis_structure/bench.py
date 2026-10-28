"""Score own-fast sections against All-In-One on the same tracks and the same grid.

Usage (app venv, from the repo root):

    python -m apps.analysis_structure.bench --allin1 DIR --peaks DIR [--json OUT]

``DIR`` for ``--allin1`` holds ``allin1_runner.py`` outputs (``<id>.json``);
``--peaks`` holds the 3-band waveform peaks of the same tracks as
``<id>.npy`` (``apps.analysis_waveform.decode.decode_peaks``, uint8, 150
columns a second).

THE GRID. Both sides are scored on All-In-One's own downbeats, because a bench
corpus has no rekordbox or own beatgrid record. Both producers snap to that one
grid, so a boundary disagreement is a disagreement about WHERE the section
changes, never about where the bar lines are. In the app both use the track's
own beatgrid lane instead.

WHAT IS MEASURED, per track and pooled:

- boundary precision, recall and F at 0.5 s (same bar), 2.0 s and 3.0 s,
  ignoring the first and last second of the file (track edges, not structure);
- section count on each side;
- DJ label agreement, per 0.5 s frame where both sides labeled the frame,
  POOLED over every scored track's frames (the denominator is printed as
  ``label_frames``); the per-track mean is printed beside it, labeled as such;
- drop F1 per frame, the label a DJ acts on, from pooled tp/fp/fn;
- the LABELER alone: All-In-One's own sections labeled from band envelopes
  versus from its stem envelopes, so a label miss can be split between the
  boundaries and the proxies;
- own-fast wall time per track, from cached peaks;
- a CONTROL that must lose: a section start every 16 bars from the first
  downbeat (``base_f@*``). A segmenter that does not beat it is finding the
  phrase grid, not the structure.

All-In-One is a REFERENCE here, not ground truth: agreeing with it is the
target because the app shows its sections once they exist, and a source that
flips the display when the better one arrives is worse than none.

-Claude
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np

from .dj_labels import label_sections
from .fast_segment import band_envelopes, segment
from .pipeline import build_sidecar

TOLERANCES_S = (0.5, 2.0, 3.0)
FRAME_S = 0.5
EDGE_S = 1.0
GRID_ID = {"backend": "bench.allin1_downbeats", "backend_version": "0"}


def _inner(times: list[float], duration: float) -> list[float]:
    return [t for t in times if EDGE_S < t < duration - EDGE_S]


def boundary_prf(ref: list[float], est: list[float], tol: float) -> tuple[float, float, float]:
    """Maximum one-to-one matching within ``tol``; empty sides score as mir_eval does.

    On a line, walking both sorted lists and matching the earliest pair within
    ``tol`` is a maximum-cardinality matching (the same hit count mir_eval's
    bipartite matching finds); a nearest-pair-first greedy is not, since it can
    spend a boundary that two other pairs needed.
    """
    if not ref and not est:
        return 1.0, 1.0, 1.0
    if not ref or not est:
        return 0.0, 0.0, 0.0
    r_sorted, e_sorted = sorted(ref), sorted(est)
    i = j = hits = 0
    while i < len(r_sorted) and j < len(e_sorted):
        if abs(r_sorted[i] - e_sorted[j]) <= tol:
            hits += 1
            i += 1
            j += 1
        elif e_sorted[j] < r_sorted[i]:
            j += 1
        else:
            i += 1
    p, r = hits / len(est), hits / len(ref)
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def _frames(sections: list[dict[str, Any]], duration: float) -> list[str | None]:
    out: list[str | None] = []
    for k in range(int(duration / FRAME_S)):
        t = (k + 0.5) * FRAME_S
        lab = next((s.get("dj_label") for s in sections if s["start_s"] <= t < s["end_s"]), None)
        out.append(lab)
    return out


def frame_counts(a: list[str | None], b: list[str | None]) -> dict[str, int]:
    """Frame counts where BOTH sides labeled: frames, agreeing frames, and drop tp/fp/fn (a = reference)."""
    both = [(x, y) for x, y in zip(a, b, strict=False) if x and y]
    return {
        "frames": len(both),
        "agree": sum(x == y for x, y in both),
        "drop_tp": sum(x == "drop" and y == "drop" for x, y in both),
        "drop_fp": sum(x != "drop" and y == "drop" for x, y in both),
        "drop_fn": sum(x == "drop" and y != "drop" for x, y in both),
    }


def agreement_from_counts(c: dict[str, int]) -> tuple[float | None, float | None]:
    """Label agreement and drop F1 over the frames counted in ``c`` (a track, or pooled)."""
    acc = c["agree"] / c["frames"] if c["frames"] else None
    denom = 2 * c["drop_tp"] + c["drop_fp"] + c["drop_fn"]
    return acc, (2 * c["drop_tp"] / denom if denom else None)


def label_agreement(a: list[str | None], b: list[str | None]) -> tuple[float | None, float | None]:
    return agreement_from_counts(frame_counts(a, b))


def pooled_agreement(rows: list[dict[str, Any]], key: str) -> tuple[float | None, float | None, int]:
    """Agreement and drop F1 over the POOLED frames of every row's ``key`` counts, and that frame denominator.

    This is the "share of time where both label" figure: a long track weighs
    by its frames, and a track whose own drop F1 is undefined still adds its
    frames to the pool instead of dropping out.
    """
    total = {k: 0 for k in ("frames", "agree", "drop_tp", "drop_fp", "drop_fn")}
    for r in rows:
        for k, v in (r.get(key) or {}).items():
            total[k] += v
    acc, f1 = agreement_from_counts(total)
    rnd = lambda v: round(v, 3) if v is not None else None  # noqa: E731
    return rnd(acc), rnd(f1), total["frames"]


def score_track(raw: dict[str, Any], peaks: np.ndarray) -> dict[str, Any] | None:
    downbeats = raw.get("model_downbeats") or []
    duration = peaks.shape[0] / 150.0
    ref = build_sidecar("bench", raw, downbeats, GRID_ID)
    if ref.get("status") != "ok":
        return None
    t0 = time.perf_counter()
    est = segment(peaks, downbeats, duration)
    ms = (time.perf_counter() - t0) * 1000.0
    if est["status"] != "ok":
        return {"failed": est["reason"]}
    ref_b = _inner([s["start_s"] for s in ref["sections"]], duration)
    est_b = _inner([s["start_s"] for s in est["sections"]], duration)
    row: dict[str, Any] = {
        "ms": round(ms, 1),
        "ref_sections": len(ref["sections"]),
        "est_sections": len(est["sections"]),
        "ref_labeled": ref["dj_labels"]["status"] == "ok",
        "est_labeled": est["dj_labels"]["status"] == "ok",
        "est_label_reason": est["dj_labels"].get("reason"),
    }
    base_b = _inner([float(t) for t in downbeats[16::16]], duration)
    for tol in TOLERANCES_S:
        row[f"f@{tol}"] = round(boundary_prf(ref_b, est_b, tol)[2], 3)
        row[f"base_f@{tol}"] = round(boundary_prf(ref_b, base_b, tol)[2], 3)
    ref_frames = _frames(ref["sections"], duration)
    row["counts"] = frame_counts(ref_frames, _frames(est["sections"], duration))
    row["label_acc"], row["drop_f1"] = agreement_from_counts(row["counts"])
    # The labeler alone: the reference's own sections, band envelopes instead of stems.
    proxy = label_sections(ref["sections"], band_envelopes(peaks))
    if proxy["status"] == "ok":
        relabeled = [{**s, "dj_label": p["dj_label"]} for s, p in zip(ref["sections"], proxy["sections"], strict=True)]
        row["proxy_counts"] = frame_counts(ref_frames, _frames(relabeled, duration))
        row["proxy_label_acc"], row["proxy_drop_f1"] = agreement_from_counts(row["proxy_counts"])
    else:
        row["proxy_label_acc"] = row["proxy_drop_f1"] = None
        row["proxy_reason"] = proxy["reason"]
    return row


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.fmean(vals), 3) if vals else None


def _score_dir(allin1: Path, peaks: Path) -> dict[str, Any]:
    """One row per All-In-One output: its scores, or why it was skipped."""
    rows: dict[str, Any] = {}
    for path in sorted(allin1.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        peaks_path = peaks / f"{path.stem}.npy"
        if raw.get("status") != "ok" or not peaks_path.exists():
            rows[path.stem] = {"skipped": raw.get("status") or "no peaks"}
            continue
        rows[path.stem] = score_track(raw, np.load(peaks_path)) or {"skipped": "ref not ok"}
    return rows


def _median(scored: list[dict[str, Any]], key: str) -> float | None:
    return statistics.median(r[key] for r in scored) if scored else None


def _summary(rows: dict[str, Any]) -> dict[str, Any]:
    scored = [r for r in rows.values() if "ms" in r]
    return {
        "tracks": len(rows),
        "scored": len(scored),
        **{k: _mean(scored, k) for k in ("f@0.5", "f@2.0", "f@3.0")},
        **{k: _mean(scored, k) for k in ("base_f@0.5", "base_f@2.0", "base_f@3.0")},
        "ref_sections_median": _median(scored, "ref_sections"),
        "est_sections_median": _median(scored, "est_sections"),
        "ref_labeled": sum(r["ref_labeled"] for r in scored),
        "est_labeled": sum(r["est_labeled"] for r in scored),
        # Pooled over frames (the published figures), then the per-track means for comparison.
        **dict(zip(("label_acc", "drop_f1", "label_frames"), pooled_agreement(scored, "counts"), strict=True)),
        **dict(
            zip(
                ("proxy_label_acc", "proxy_drop_f1", "proxy_frames"),
                pooled_agreement(scored, "proxy_counts"),
                strict=True,
            )
        ),
        "label_acc_track_mean": _mean(scored, "label_acc"),
        "drop_f1_track_mean": _mean(scored, "drop_f1"),
        "ms_median": _median(scored, "ms"),
        "ms_max": max((r["ms"] for r in scored), default=None),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--allin1", type=Path, required=True)
    ap.add_argument("--peaks", type=Path, required=True)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    rows = _score_dir(args.allin1, args.peaks)
    summary = _summary(rows)
    for sid, r in rows.items():
        print(sid, json.dumps(r))
    print("SUMMARY", json.dumps(summary))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "tracks": rows}, indent=1), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
