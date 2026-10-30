"""Re-score a calibration session's kept clips with the current scorer, without re-rendering (round 14).

A session keeps each candidate's played clip (``clips/<cid>.flac``: the 16-bit render from 2 bars before the plan's
first event to 2 bars after the incoming side's last, a bit-exact slice) and, when built with deck slices, the two
deck outputs for the same window (``decks/<cid>/{out,in}.flac``). That window is exactly a render made with
``context_bars = 2``, so the plan, the track profiles and :func:`apps.autodj.render_plan.layout_of` at 2 bars give
the clip's own clock (overlap, outgoing downbeats, steps, seams).

Re-scores per candidate:
  - ``full_m``: :func:`score_render` on the clip and its deck slices (only when the deck slices are kept). The solos
    are 2 bars, not the render's 4, so its true-peak reference is lower; a cross-check, never the table.
  - ``factor_m``: the RECORDED factors' composite under the current weights (0 on a recorded veto). The only change
    to those factors at 0.6.0 is the weight, so this is 0.6.0 on the full render.
  - ``veto_m``: ``factor_m``, or 0 when the loudness-loss veto would fire on the clip (:func:`loudness_checks`), as
    0.6.0 scores with ``LOUDNESS_LOSS_VETO_ON``. The loss is read against the outgoing track's own audio over the clip
    (``outgoing_natural`` on the profile's file path, rewritten by ``--path-map OLD=NEW`` when the library lives
    elsewhere on this machine); a file that is not here leaves the loss ``unknown``.

Requirements:
  ✔︎ ✅ the clip clock is the context-2 render clock: a clip whose length differs from the layout's by more than
      one outgoing beat is refused, never scored on a guessed clock
  ✔︎ ✅ tempo telemetry is rebuilt from the layout and checked against the recorded stretch_pct
  ✔︎ ✅ the recorded loudness gap is printed beside the clip-measured one (the 2-bar solo span is a different
      measurement window; the difference is the control)
Acceptance:
  [if] a profile for a planned track is missing [then ⛔️] RescoreError naming it
  [if] a clip's length disagrees with its layout [then ⛔️] RescoreError naming the clip
  [if] a recorded stretch_pct disagrees with the rebuilt tempo ratio by more than 0.01 % [then ⛔️] RescoreError

Usage: uv run --locked python -m scripts.bench.tryer_calibration.rescore --profiles <dir> --session <dir> \
    [--session <dir> ...] [--reviews <oltf-r1.json>] --out <rows.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from apps.autodj.plan import TransitionPlan, parse_plan
from apps.autodj.profile import TrackProfile, profile_from_payloads
from apps.autodj.propose import parse_intent
from apps.autodj.render_plan import RenderLayout, layout_of
from apps.transition_score.contract import SCORER_VERSION, ScoreResult
from apps.transition_score.scorer import PRIOR_WEIGHTS, RenderContext, composite, loudness_checks, score_render
from apps.transition_score.tryer import incoming_on_render_clock, outgoing_natural, rendered_steps

PLAYED_CONTEXT_BARS = 2
RATE = 48000


class RescoreError(RuntimeError):
    """A kept clip cannot be put back on its render clock."""


@dataclass(frozen=True)
class Row:
    session: str
    cid: str
    pair_id: str
    role: str
    style: str
    old_m: float | None
    old_vetoes: list[str]
    factor_m: float | None
    veto_m: float | None
    full_m: float | None
    full_vetoes: list[str] | None
    full_low: dict[str, Any] | None
    would_veto: bool
    loudness_loss_bars: float | None
    loudness_loss_depth_lu: float | None
    loudness_factor: float | None
    old_gap_lu: float | None
    clip_gap_lu: float | None
    verdict: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ----- the clip's clock -------------------------------------------------------------------------------------------


class Profiles:
    """Profile snapshots (``build_manifest --profiles``), each built on first use: a snapshot of a track no plan uses
    (one with no grid, say) is never parsed."""

    def __init__(self, directory: Path) -> None:
        self._paths = {path.stem: path for path in directory.glob("*.json")}
        self._built: dict[str, TrackProfile] = {}

    def file_path(self, stable_id: str) -> str:
        return str(json.loads(self._paths[stable_id].read_text(encoding="utf-8"))["track"]["file_path"])

    def get(self, stable_id: str) -> TrackProfile:
        if stable_id not in self._paths:
            raise RescoreError(f"no profile snapshot for {stable_id}")
        if stable_id not in self._built:
            document = json.loads(self._paths[stable_id].read_text(encoding="utf-8"))
            self._built[stable_id] = profile_from_payloads(document["anlz"], document["track"])
        return self._built[stable_id]


def _render_s(bar: float, plan: TransitionPlan, out_p: TrackProfile, layout: RenderLayout) -> float:
    return out_p.time_of_bar(plan.out.start_bar + bar) - layout.start_out_s


def _pitch_follows_tempo(entry: dict[str, Any]) -> bool:
    """Master Tempo as the original render had it: the recorded key_shift is either measured or unknown because of it."""
    if "key_shift_st" in entry["metrics"]:
        return True
    if "Master Tempo" in entry["unknown"].get("key_shift", ""):
        return False
    raise RescoreError(f"{entry['pair_id']} {entry['candidate_id']}: recorded key_shift says neither way")


def _bass_hit_from(
    entry: dict[str, Any], plan: TransitionPlan, out_p: TrackProfile, layout: RenderLayout
) -> float | None:
    hit = (entry["plan"].get("bass_entry") or {}).get("hit_plan_bar")
    return None if hit is None else _render_s(float(hit), plan, out_p, layout)


def clip_context(
    entry: dict[str, Any], profiles: Profiles, frames: int, path_map: tuple[str, str] | None = None
) -> RenderContext:
    plan = parse_plan(entry["plan"])
    out_p, in_p = profiles.get(plan.out.track), profiles.get(plan.in_.track)
    layout = layout_of(plan, out_p, in_p, PLAYED_CONTEXT_BARS)
    window = entry["played_window_s"]
    length_s = frames / RATE
    if abs(length_s - (window[1] - window[0])) > 1.0 / RATE * 2:
        raise RescoreError(f"{entry['candidate_id']}: clip is {length_s:.4f} s, its window {window}")
    end_s = _render_s(float(plan.bars), plan, out_p, layout)
    if end_s >= length_s:
        raise RescoreError(f"{entry['candidate_id']}: overlap ends at {end_s:.2f} s past the {length_s:.2f} s clip")
    recorded = entry["metrics"].get("stretch_pct")
    if recorded is not None and abs(abs(layout.in_ratio - 1.0) * 100.0 - recorded) > 0.01:
        raise RescoreError(f"{entry['candidate_id']}: stretch {recorded} % recorded, layout says {layout.in_ratio}")
    steps, seams = rendered_steps(plan, out_p, layout)
    source = Path(profiles.file_path(plan.out.track))
    if path_map is not None and str(source).startswith(path_map[0]):
        source = Path(path_map[1] + str(source)[len(path_map[0]) :])
    natural = outgoing_natural(source, layout.start_out_s, frames, RATE) if source.is_file() else None
    in_source = Path(profiles.file_path(plan.in_.track))
    if path_map is not None and str(in_source).startswith(path_map[0]):
        in_source = Path(path_map[1] + str(in_source)[len(path_map[0]) :])
    entry_s, in_natural = (
        incoming_on_render_clock(plan, out_p, in_p, layout, in_source, frames, RATE)
        if in_source.is_file()
        else (None, None)
    )
    return RenderContext(
        sample_rate=RATE,
        beat_s=layout.out_bar_s / 4.0,
        overlap_s=(_render_s(0.0, plan, out_p, layout), end_s),
        out_downbeats_s=tuple(layout.out_downbeats_render_s(out_p, length_s)),
        tempo_ratios=(1.0, layout.in_ratio),
        intent=None if plan.intent is None else parse_intent(plan.intent),
        shape=entry["plan"].get("shape"),
        out_natural=natural,
        in_natural=in_natural,
        in_bass_entry_s=entry_s,
        bass_hit_s=_bass_hit_from(entry, plan, out_p, layout),
        out_natural_unknown_reason=f"outgoing source {source.name} is not on this machine",
        steps=steps,
        seams_s=seams,
        pitch_follows_tempo=_pitch_follows_tempo(entry),
    )


# ----- re-scoring -------------------------------------------------------------------------------------------------


def _read(path: Path) -> np.ndarray:
    import soundfile  # the 'analysis' extra, as build_manifest uses

    samples, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
    if rate != RATE:
        raise RescoreError(f"{path}: {rate} Hz, expected {RATE}")
    return samples


def factor_m(entry: dict[str, Any]) -> float | None:
    """M from the recorded factors and vetoes under the current weights."""
    if entry["vetoes"]:
        return 0.0
    if entry["score"] is None:
        return None
    return round(composite(entry["factors"], PRIOR_WEIGHTS), 4)


def rescore_entry(
    session: Path, cid: str, entry: dict[str, Any], profiles: Profiles, path_map: tuple[str, str] | None
) -> Row:
    mix = _read(session / "clips" / f"{cid}.flac")
    ctx = clip_context(entry, profiles, len(mix), path_map)
    loud = loudness_checks(mix, ctx)
    full: ScoreResult | None = None
    plan = entry["plan"]
    for out_path, in_path in (
        (session / "decks" / cid / "out.flac", session / "decks" / cid / "in.flac"),
        (
            session / "clips" / cid / "decks" / f"deck{plan['out']['deck']}.flac",
            session / "clips" / cid / "decks" / f"deck{plan['in']['deck']}.flac",
        ),
    ):
        if out_path.is_file():
            full = score_render(mix, _read(out_path), _read(in_path), ctx)
            break
    would_veto = bool(loud.metrics.get("loudness_loss_would_veto"))
    weighted = factor_m(entry)
    return Row(
        session=session.name,
        cid=cid,
        pair_id=entry["pair_id"],
        role=entry["role"],
        style=entry["style"],
        old_m=entry["score"],
        old_vetoes=[v["name"] for v in entry["vetoes"]],
        factor_m=weighted,
        veto_m=0.0 if would_veto else weighted,
        full_m=None if full is None else full.score,
        full_vetoes=None if full is None else [v.name for v in full.vetoes],
        full_low=None
        if full is None
        else {
            k: full.metrics.get(k)
            for k in (
                "bass_overlap_bars_v0",
                "bass_both_full_beats",
                "low_gap_db",
                "early_handover_bars",
                "low_hole_beats",
                "low_hole_depth_db",
            )
        },
        would_veto=would_veto,
        loudness_loss_bars=loud.metrics.get("loudness_loss_bars"),  # type: ignore[arg-type]
        loudness_loss_depth_lu=loud.metrics.get("loudness_loss_depth_lu"),  # type: ignore[arg-type]
        loudness_factor=entry["factors"].get("loudness_continuity"),
        old_gap_lu=entry["metrics"].get("loudness_gap_lu"),
        clip_gap_lu=loud.metrics.get("loudness_gap_lu"),  # type: ignore[arg-type]
        verdict=None,
    )


def rescore_session(
    session: Path, profiles: Profiles, reviews: dict[str, Any], path_map: tuple[str, str] | None
) -> list[Row]:
    key = json.loads((session / "key.json").read_text(encoding="utf-8"))
    rows = []
    for cid, entry in key["candidates"].items():
        if entry["role"] == "anchor":
            continue  # a deliberately flammed copy of taste1: not a candidate the proposer made
        row = rescore_entry(session, cid, entry, profiles, path_map)
        rows.append(Row(**{**row.__dict__, "verdict": reviews.get(cid)}))
    return rows


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--session", type=Path, action="append", required=True)
    parser.add_argument("--reviews", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--path-map", default=None, help="OLD=NEW prefix rewrite for the profiles' file paths")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    profiles = Profiles(args.profiles)
    reviews: dict[str, Any] = {}
    for path in args.reviews:
        reviews.update(json.loads(path.read_text(encoding="utf-8"))["verdicts"])
    path_map = None if args.path_map is None else tuple(args.path_map.split("=", 1))
    if path_map is not None and len(path_map) != 2:
        raise RescoreError(f"--path-map must be OLD=NEW, got {args.path_map!r}")
    rows = [row for session in args.session for row in rescore_session(session, profiles, reviews, path_map)]
    args.out.write_text(
        json.dumps({"scorer_version": SCORER_VERSION, "rows": [r.to_json() for r in rows]}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] {len(rows)} candidates re-scored with {SCORER_VERSION} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
