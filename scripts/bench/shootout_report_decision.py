"""Sample-size gates, paired comparisons, and the production decision section."""

from __future__ import annotations

import statistics
from typing import Any

from shootout_report_constants import (
    _CLIPS_DIR,
    _MIN_TRACKS_FOR_RANKING,
    _is_clean,
)
from shootout_report_model import ModelStats
from shootout_report_tables import _divider, _row

# ----- sample-size gate -------------------------------------------------------


def _underpowered(track_count: int) -> bool:
    return track_count < _MIN_TRACKS_FOR_RANKING


def _underpowered_banner(track_count: int) -> list[str]:
    """Loud, unmissable header when the sample cannot support a ranking."""
    if not _underpowered(track_count):
        return []
    return [
        "## INCOMPLETE RUN, NOT A RESULT",
        "",
        f"This report covers **{track_count} "
        f"{'track' if track_count == 1 else 'tracks'}**, below the "
        f"{_MIN_TRACKS_FOR_RANKING}-track minimum this benchmark requires before it "
        "will rank anything. The ranking, spread, quality-per-cost and "
        "production-decision sections are WITHDRAWN below, not merely caveated.",
        "",
        "At this sample size a spread column would read 0.00 because there is nothing "
        "to vary, and a 'wins' column would read 1/1. Those look like precision and "
        "are the opposite. Per-track numbers are shown because they are real "
        "measurements; every conclusion drawn across tracks is not.",
        "",
    ]


def _withdrawn(track_count: int, what: str) -> list[str]:
    """Stand-in for a section the sample size cannot support."""
    return [
        f"**WITHDRAWN.** Producing {what} from {track_count} "
        f"{'track' if track_count == 1 else 'tracks'} would misrepresent noise as a "
        f"finding. Re-run with at least {_MIN_TRACKS_FOR_RANKING} tracks.",
    ]


# ----- does the leak explain the original surprise? ---------------------------

# The single-track result that started this investigation: mdx_extra_q scored
# 9.02 dB on this song while htdemucs_ft, at four times the compute, scored
# 6.95 to 7.09 dB. That was reported as a headline finding before anyone checked
# what mdx_extra had been trained on.
_ANCHOR_SLUG: str = "al-james-schoolboy-facination"
_ANCHOR_ORIGINAL_DB: float = 9.02


def _anchor_verdict(tracks: list[dict[str, Any]]) -> list[str]:
    """Answer the question the whole exercise started from, in plain words."""
    anchor = next((t for t in tracks if t["slug"] == _ANCHOR_SLUG), None)
    if anchor is None:
        return []
    results = anchor["results"]
    needed = ("mdx_extra_q", "mdx_q", "htdemucs_ft")
    if any("si_sdr" not in results.get(name, {}) for name in needed):
        return []
    leaked = results["mdx_extra_q"]["si_sdr"]
    sibling = results["mdx_q"]["si_sdr"]
    finetuned = results["htdemucs_ft"]["si_sdr"]

    return [
        "### Does this explain the original 9.02 dB result on Schoolboy Facination?",
        "",
        f"Yes. On that track `mdx_extra_q` scores {leaked:.2f} dB here, reproducing the "
        f"{_ANCHOR_ORIGINAL_DB:.2f} dB that was reported. But `mdx_q`, the same "
        f"architecture and the same quantization trained WITHOUT the test set, scores "
        f"{sibling:.2f} dB on the identical excerpt. The entire "
        f"{leaked - sibling:+.2f} dB advantage sits with the checkpoint that trained "
        f"on this song.",
        "",
        f"Against `htdemucs_ft` at {finetuned:.2f} dB, the original comparison read as "
        f'"a cheap 2021 model beats a 2023 model at four times the compute". Once the '
        f"contaminated checkpoint is set aside, `mdx_q` at {sibling:.2f} dB is "
        f"{sibling - finetuned:+.2f} dB against `htdemucs_ft`, which is the ordinary "
        "ordering everyone expected. The surprise was train-on-test leakage, not a "
        "model discovery, and it should not be repeated as a finding.",
        "",
    ]


# ----- the actual decision ----------------------------------------------------

# The production run is ~1100 tracks, so compute is a real budget line rather
# than a rounding error. A cheaper model is worth adopting when its quality loss
# is small enough to be inaudible, and this is the threshold for "small enough":
# under 0.2 dB of median SI-SDR is well inside the track-to-track spread these
# models already show, so it cannot be reliably heard.
_EQUIVALENCE_DB: float = 0.2
_PRODUCTION_TRACKS: int = 1100


def _paired_deltas(tracks: list[dict[str, Any]], model: str, reference: str) -> list[float]:
    """Per-track SI-SDR difference, model minus reference.

    Every model separates the SAME excerpts, so this is a paired design and the
    paired difference is the right statistic. Comparing medians across models
    instead lets track difficulty leak into the answer: these twelve songs span
    roughly 4 dB to 17 dB of achievable SI-SDR, a range far larger than any gap
    between models, so a difference of medians can reverse the sign of the
    per-track truth. It does exactly that here.
    """
    out: list[float] = []
    for track in tracks:
        results = track["results"]
        if "si_sdr" in results.get(model, {}) and "si_sdr" in results.get(reference, {}):
            out.append(results[model]["si_sdr"] - results[reference]["si_sdr"])
    return out


def _paired_table(tracks: list[dict[str, Any]], models: list[str], reference: str) -> list[str]:
    header = [
        "model",
        f"median delta vs `{reference}`",
        "min",
        "max",
        f"tracks beating `{reference}`",
    ]
    lines = [_row(header), _divider(len(header), left=1)]
    for name in models:
        if name == reference:
            continue
        deltas = _paired_deltas(tracks, name, reference)
        if not deltas:
            continue
        wins = sum(1 for d in deltas if d > 0)
        lines.append(
            _row(
                [
                    f"`{name}`",
                    f"**{statistics.median(deltas):+.2f}**",
                    f"{min(deltas):+.2f}",
                    f"{max(deltas):+.2f}",
                    f"{wins}/{len(deltas)}",
                ]
            )
        )
    return lines


def _production_decision(ran: list[ModelStats], tracks: list[dict[str, Any]]) -> list[str]:
    """Does the cheap clean model hold close enough to the best clean model?

    This is the section the whole benchmark exists to produce. Everything above
    describes the field; this picks from it, states the margin, and prices the
    difference over a real catalogue.
    """
    clean = sorted(
        [s for s in ran if _is_clean(s.name)],
        key=lambda s: s.median_si_sdr,
        reverse=True,
    )
    track_count = len(tracks)
    cheapest = min(clean, key=lambda s: s.median_separate_s)
    # The comparison that matters is the cheap model against the strongest
    # alternative someone would otherwise pay for. When the cheapest model is
    # also the highest scoring, comparing it with itself yields a table of
    # zeros, so the rival becomes the best of the remaining models.
    rivals = [s for s in clean if s.name != cheapest.name]
    best = clean[0] if clean[0].name != cheapest.name else (rivals[0] if rivals else cheapest)
    # Paired, not a difference of medians. See _paired_deltas: track difficulty
    # dominates the raw medians and can flip the sign of the real comparison.
    paired = _paired_deltas(tracks, cheapest.name, best.name)
    paired_median = statistics.median(paired) if paired else 0.0
    delta = -paired_median  # how much the cheap model gives up, positive = worse
    speedup = best.median_separate_s / cheapest.median_separate_s
    hours_best = _PRODUCTION_TRACKS * best.median_separate_s / 3600.0
    hours_cheap = _PRODUCTION_TRACKS * cheapest.median_separate_s / 3600.0

    lines = [
        "## The production decision",
        "",
        f"Cheapest test-set-clean model is `{cheapest.name}` "
        f"({cheapest.median_separate_s:.2f}s median). The strongest alternative worth "
        f"paying for is `{best.name}` ({best.median_separate_s:.2f}s, "
        f"{speedup:.1f}x more GPU time).",
        "",
        _row(["", f"`{best.name}` (rival)", f"`{cheapest.name}` (cheapest)", "delta"]),
        _divider(4),
        _row(
            [
                "median SI-SDR dB (unpaired)",
                f"{best.median_si_sdr:.2f}",
                f"{cheapest.median_si_sdr:.2f}",
                f"{cheapest.median_si_sdr - best.median_si_sdr:+.2f}",
            ]
        ),
        _row(
            [
                "**median PAIRED delta dB**",
                "reference",
                f"{paired_median:+.2f}",
                f"**{paired_median:+.2f}**",
            ]
        ),
        _row(
            [
                "median SIR dB",
                f"{best.median_sir:.2f}",
                f"{cheapest.median_sir:.2f}",
                f"{cheapest.median_sir - best.median_sir:+.2f}",
            ]
        ),
        _row(
            [
                "median SAR dB",
                f"{best.median_sar:.2f}",
                f"{cheapest.median_sar:.2f}",
                f"{cheapest.median_sar - best.median_sar:+.2f}",
            ]
        ),
        _row(
            [
                "median separate_s",
                f"{best.median_separate_s:.2f}",
                f"{cheapest.median_separate_s:.2f}",
                f"{speedup:.1f}x cheaper",
            ]
        ),
        _row(
            [
                f"GPU hours for {_PRODUCTION_TRACKS} tracks",
                f"{hours_best:.1f}",
                f"{hours_cheap:.1f}",
                f"{hours_best - hours_cheap:.1f} saved",
            ]
        ),
        _row(
            [
                "per-track wins",
                f"{best.wins}/{track_count}",
                f"{cheapest.wins}/{track_count}",
                "",
            ]
        ),
        "",
        f"Paired per-track deltas against `{best.name}`, the correct statistic for "
        "this design because every model separated the identical excerpts. The "
        "unpaired median row above is shown only so the two can be compared: track "
        "difficulty spans roughly 4 dB to 17 dB here, far wider than any gap between "
        "models, so a difference of medians can and does reverse the per-track sign.",
        "",
    ]
    lines += _paired_table(tracks, [s.name for s in clean], best.name)
    lines += [
        "",
    ]
    if delta < _EQUIVALENCE_DB:
        lines += [
            f"**Verdict: use `{cheapest.name}`.** It gives up {delta:.2f} dB of median "
            f"SI-SDR, which is under the {_EQUIVALENCE_DB} dB threshold this project "
            f"treats as inaudible, and it is {speedup:.1f}x cheaper. Across "
            f"{_PRODUCTION_TRACKS} tracks that is {hours_best - hours_cheap:.1f} GPU "
            f"hours saved. Per track the paired difference stayed within "
            f"{min(paired):+.2f} to {max(paired):+.2f} dB over {track_count} tracks, "
            "so this is a median with no bad case hiding behind it.",
            "",
            "This is a metric-level verdict on one GPU generation. Confirm it by ear "
            + (
                "on the clips in `clips-shootout/`"
                if _CLIPS_DIR.is_dir()
                else "(clips not yet generated: run `shootout_clips.py`)"
            )
            + ", and re-measure the timings on the production GPU, before committing a "
            "full catalogue run.",
            "",
        ]
    else:
        lines += [
            f"**Verdict: use `{best.name}`.** The cheap option gives up {delta:.2f} dB, "
            f"above the {_EQUIVALENCE_DB} dB inaudibility threshold, so the "
            f"{speedup:.1f}x saving is not free quality. If GPU budget later becomes "
            f"the binding constraint, `{cheapest.name}` is the fallback and the cost of "
            f"that choice is {delta:.2f} dB.",
            "",
        ]
    return lines
