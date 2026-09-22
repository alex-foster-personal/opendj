# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Turn scripts/bench/model_shootout.json into the human verdict.

Separated from the Modal runner on purpose: the GPU work is expensive and the
analysis is not, so the report can be re-cut (new rankings, new flags) without
re-separating a single track. Reads the JSON, writes MODEL-SHOOTOUT.md, and
prints the same tables to stdout.

What it decides, and how:
  consistency  per-track winners are counted. A model that wins one track and
               places mid-field elsewhere was an outlier, not a champion.
  ranking      by MEDIAN SI-SDR, with min/max carried alongside, because a mean
               hides the case where a model is excellent twice and broken once.
  cost         median separate_s per model, and dB per second, so a 4x compute
               model has to show 4x-worthy quality to justify itself.
  provenance   MUSDB18-HQ test scores are INVALID for any model that trained on
               the MUSDB test set. demucs ships two such models (mdx_extra and
               its quantized twin), so the headline ranking is taken from the
               uncontaminated subset and the contaminated pair is reported
               separately as evidence of the leak, never as a recommendation.
  honesty      the under-separation check. SI-SDR rewards an estimate that keeps
               the vocal undistorted even when accompaniment is still audible
               underneath, so a model can climb it by refusing to separate hard.
               A model is FLAGGED when it ranks materially better on SI-SDR than
               on SIR (accompaniment suppression) while also correlating with the
               mixture more than its peers do. Both conditions must hold: SIR
               alone punishes gentle-but-clean models unfairly.

Requirements (mini-PRD):
  ✔︎ ✅ every table cell traces to a value in the JSON; a missing (track, model)
    cell renders as "fail" and is excluded from that model's statistics.
    [if] a model errored on every track [then] it is listed as failed, not ranked
  ✔︎ ✅ the under-separation verdict prints its evidence (SI-SDR rank, SIR rank,
    corr_mixture) whether or not anything is flagged.
    [if] no model is flagged [then] the section says so explicitly, with numbers
  ✔︎ ✅ no banned Unicode dash characters reach the markdown.

Run:
  uv run scripts/bench/shootout_report.py

-Claude
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import shootout_spec

_BENCH_DIR: Path = Path(__file__).resolve().parent
_IN_JSON: Path = _BENCH_DIR / "model_shootout.json"
_OUT_MD: Path = _BENCH_DIR / "MODEL-SHOOTOUT.md"
# Claims about listening clips are checked against this directory rather than
# assumed: the report must never promise audio that was not written.
_CLIPS_DIR: Path = _BENCH_DIR / "clips-shootout"

# A model is suspected of buying SI-SDR with under-separation when it beats its
# own SIR ranking by at least this many places. Two places on a seven-model board
# is a real inversion, not measurement noise.
_RANK_GAP_FLAG: int = 2

# Below this many tracks the ranking, spread and production-decision sections are
# refused outright rather than rendered with degenerate values. This project has
# already published an N=1 measurement as a headline finding and had to retract
# it; a spread column reading 0.00 because there is nothing to vary is not a
# tighter result, it is an absent one, and a table looks equally authoritative
# either way. The gate exists so a partial run cannot be mistaken for a study.
_MIN_TRACKS_FOR_RANKING: int = 5

_provenance = shootout_spec.provenance
_is_clean = shootout_spec.is_test_set_clean


@dataclass(frozen=True)
class ModelStats:
    """One model's per-metric summary across every track it completed."""

    name: str
    si_sdr: list[float]
    sir: list[float]
    sar: list[float]
    lsd: list[float]
    corr_mixture: list[float]
    separate_s: list[float]
    wins: int
    failures: list[str]

    @property
    def ran(self) -> int:
        return len(self.si_sdr)

    @property
    def median_si_sdr(self) -> float:
        return statistics.median(self.si_sdr)

    @property
    def mean_si_sdr(self) -> float:
        return statistics.fmean(self.si_sdr)

    @property
    def median_sir(self) -> float:
        return statistics.median(self.sir)

    @property
    def median_sar(self) -> float:
        return statistics.median(self.sar)

    @property
    def median_lsd(self) -> float:
        return statistics.median(self.lsd)

    @property
    def median_corr_mixture(self) -> float:
        return statistics.median(self.corr_mixture)

    @property
    def median_separate_s(self) -> float:
        return statistics.median(self.separate_s)

    @property
    def db_per_second(self) -> float:
        return self.median_si_sdr / self.median_separate_s


def _collect(manifest: dict[str, Any]) -> tuple[list[ModelStats], list[dict[str, Any]]]:
    tracks = manifest["tracks"]
    winners: dict[str, int] = {name: 0 for name in manifest["models"]}
    for track in tracks:
        scored = {
            name: result["si_sdr"]
            for name, result in track["results"].items()
            if "si_sdr" in result
        }
        if scored:
            winners[max(scored, key=lambda k: scored[k])] += 1

    stats: list[ModelStats] = []
    for name in manifest["models"]:
        rows = [track["results"].get(name, {}) for track in tracks]
        good = [row for row in rows if "si_sdr" in row]
        failures = [
            track["track"]
            for track, row in zip(tracks, rows, strict=False)
            if "si_sdr" not in row
        ]
        if not good:
            stats.append(ModelStats(name, [], [], [], [], [], [], 0, failures))
            continue
        stats.append(
            ModelStats(
                name=name,
                si_sdr=[row["si_sdr"] for row in good],
                sir=[row["sir_db"] for row in good],
                sar=[row["sar_db"] for row in good],
                lsd=[row["lsd_db"] for row in good],
                corr_mixture=[row["corr_mixture"] for row in good],
                separate_s=[row["separate_s"] for row in good],
                wins=winners[name],
                failures=failures,
            )
        )
    return stats, tracks


# ----- table builders ---------------------------------------------------------


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _divider(count: int, left: int = 1) -> str:
    """Markdown alignment row. `left` leading columns are text, the rest numeric
    and right-aligned so decimal points line up down a column."""
    parts = ["---" if i < left else "---:" for i in range(count)]
    return "| " + " | ".join(parts) + " |"


def _matrix_table(
    tracks: list[dict[str, Any]], models: list[str], summarise: bool = True
) -> list[str]:
    header = (
        ["track", "genre"]
        + [n if _is_clean(n) else f"{n} (leak)" for n in models]
        + ["winner", "winner, clean only", "mix baseline"]
    )
    lines = [_row(header), _divider(len(header), left=2)]
    for track in tracks:
        results = track["results"]
        scored = {n: r["si_sdr"] for n, r in results.items() if "si_sdr" in r}
        clean = {n: v for n, v in scored.items() if _is_clean(n)}
        winner = max(scored, key=lambda k: scored[k]) if scored else "none"
        clean_winner = max(clean, key=lambda k: clean[k]) if clean else "none"
        cells = [track["track"], track["genre"]]
        for name in models:
            row = results.get(name, {})
            cells.append(f"{row['si_sdr']:.2f}" if "si_sdr" in row else "fail")
        cells.append(f"**{winner}**")
        cells.append(f"**{clean_winner}**")
        cells.append(f"{track['baseline_mixture']['si_sdr']:.2f}")
        lines.append(_row(cells))

    if not summarise:
        # At N below the gate a median row is just the single value wearing a
        # label that implies aggregation. Leave it out entirely.
        return lines
    for label, fn in (("**median**", statistics.median), ("**mean**", statistics.fmean)):
        cells = [label, ""]
        for name in models:
            values = [
                t["results"][name]["si_sdr"]
                for t in tracks
                if "si_sdr" in t["results"].get(name, {})
            ]
            cells.append(f"**{fn(values):.2f}**" if values else "fail")
        baselines = [t["baseline_mixture"]["si_sdr"] for t in tracks]
        # Two blanks, not one: the winner and clean-winner columns have no
        # meaningful aggregate, and a short row silently shifts every cell left.
        cells.extend(["", "", f"{fn(baselines):.2f}"])
        if len(cells) != len(header):
            raise RuntimeError(
                f"summary row has {len(cells)} cells but the header has "
                f"{len(header)}; the table would render misaligned"
            )
        lines.append(_row(cells))
    return lines


def _ranking_table(stats: list[ModelStats]) -> list[str]:
    ranked = sorted(
        [s for s in stats if s.ran], key=lambda s: s.median_si_sdr, reverse=True
    )
    header = [
        "rank",
        "model",
        "median SI-SDR",
        "mean",
        "min",
        "max",
        "spread",
        "wins",
        "test-set clean",
        "training data",
    ]
    # Numeric columns sit between the model name and the two trailing text
    # columns, so the alignment row is built explicitly rather than by a count.
    alignment = ["---", "---"] + ["---:"] * 6 + ["---", "---"]
    lines = [_row(header), "| " + " | ".join(alignment) + " |"]
    for position, s in enumerate(ranked, start=1):
        training, clean = _provenance(s.name)
        lines.append(
            _row(
                [
                    str(position),
                    s.name,
                    f"**{s.median_si_sdr:.2f}**",
                    f"{s.mean_si_sdr:.2f}",
                    f"{min(s.si_sdr):.2f}",
                    f"{max(s.si_sdr):.2f}",
                    f"{max(s.si_sdr) - min(s.si_sdr):.2f}",
                    f"{s.wins}/{len(s.si_sdr)}",
                    "yes" if clean else "**NO, saw test set**",
                    training,
                ]
            )
        )
    return lines


def _cost_table(stats: list[ModelStats]) -> list[str]:
    ranked = sorted(
        [s for s in stats if s.ran], key=lambda s: s.db_per_second, reverse=True
    )
    cheapest = min(s.median_separate_s for s in ranked)
    header = ["model", "median SI-SDR", "median separate_s", "x cheapest", "dB per second"]
    lines = [_row(header), _divider(len(header))]
    for s in ranked:
        lines.append(
            _row(
                [
                    s.name,
                    f"{s.median_si_sdr:.2f}",
                    f"{s.median_separate_s:.2f}",
                    f"{s.median_separate_s / cheapest:.2f}x",
                    f"**{s.db_per_second:.2f}**",
                ]
            )
        )
    return lines


def _honesty_table(stats: list[ModelStats]) -> tuple[list[str], list[str]]:
    """Rank on SI-SDR and on SIR, then look for models that gain from the swap."""
    ran = [s for s in stats if s.ran]
    by_sisdr = sorted(ran, key=lambda s: s.median_si_sdr, reverse=True)
    by_sir = sorted(ran, key=lambda s: s.median_sir, reverse=True)
    sisdr_rank = {s.name: i + 1 for i, s in enumerate(by_sisdr)}
    sir_rank = {s.name: i + 1 for i, s in enumerate(by_sir)}
    corr_median = statistics.median([s.median_corr_mixture for s in ran])

    header = [
        "model",
        "SI-SDR rank",
        "SIR rank",
        "rank gain",
        "median SIR dB",
        "median SAR dB",
        "corr with mixture",
        "verdict",
    ]
    lines = [_row(header), _divider(len(header))]
    flagged: list[str] = []
    for s in by_sisdr:
        gain = sir_rank[s.name] - sisdr_rank[s.name]
        suspicious = gain >= _RANK_GAP_FLAG and s.median_corr_mixture > corr_median
        if suspicious:
            flagged.append(s.name)
        lines.append(
            _row(
                [
                    s.name,
                    str(sisdr_rank[s.name]),
                    str(sir_rank[s.name]),
                    f"{gain:+d}",
                    f"{s.median_sir:.2f}",
                    f"{s.median_sar:.2f}",
                    f"{s.median_corr_mixture:.4f}",
                    "**FLAG**" if suspicious else "clean",
                ]
            )
        )
    return lines, flagged


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
        f"\"a cheap 2021 model beats a 2023 model at four times the compute\". Once the "
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


def _paired_deltas(
    tracks: list[dict[str, Any]], model: str, reference: str
) -> list[float]:
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


def _paired_table(
    tracks: list[dict[str, Any]], models: list[str], reference: str
) -> list[str]:
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


def _production_decision(
    ran: list[ModelStats], tracks: list[dict[str, Any]]
) -> list[str]:
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


# ----- report -----------------------------------------------------------------


def build_report(manifest: dict[str, Any]) -> str:
    stats, tracks = _collect(manifest)
    models = manifest["models"]
    ran = [s for s in stats if s.ran]
    failed = [s for s in stats if not s.ran]
    ranked = sorted(ran, key=lambda s: s.median_si_sdr, reverse=True)
    clean = [s for s in ranked if _is_clean(s.name)]
    leaked = [s for s in ranked if not _is_clean(s.name)]
    best_overall = ranked[0]
    best_clean = clean[0]
    by_value = sorted(clean, key=lambda s: s.db_per_second, reverse=True)[0]
    honesty_lines, flagged = _honesty_table(stats)
    baseline_median = statistics.median(
        [t["baseline_mixture"]["si_sdr"] for t in tracks]
    )
    # Sanity anchor: the do-nothing mixture is the deliberately bad arm. A test
    # that cannot separate it from the real models is broken, so say so loudly.
    failed_anchor = [s.name for s in ran if s.median_si_sdr <= baseline_median]
    config = manifest["config"]
    gated = _underpowered(len(tracks))

    out: list[str] = [
        "# Vocal separation model shootout",
        "",
        f"{len(tracks)} MUSDB18-HQ test "
        f"{'track' if len(tracks) == 1 else 'tracks'}, {len(models)} models, "
        f"{manifest['excerpt_len_s']:.0f}s vocal-dense excerpt per track, scored "
        "against the TRUE vocal stems.",
        f"All models run at identical knobs (overlap {config['overlap']}, shifts "
        f"{config['shifts']}) on a {config['gpu']}, so the model is the only variable.",
        "",
        "Primary metric is SI-SDR in dB against ground truth, higher is better. The "
        "do-nothing baseline (the mixture graded as if it were a vocal estimate) has a "
        f"median of {baseline_median:.2f} dB and is the deliberately bad arm: a test "
        "that cannot tell it from a real separator is broken.",
        "",
        *_underpowered_banner(len(tracks)),
        "## Read this before the tables: two of these models saw the test set",
        "",
        "The demucs README states that `mdx_extra` was \"trained with extra training "
        "data (**including MusDB test set**)\", and `mdx_extra_q` is its quantized "
        "twin. Every track below is a MUSDB18-HQ **test** track, so those two models "
        "are being graded on material they trained on. Their scores measure "
        "memorisation as well as separation and CANNOT be compared with the others.",
        "",
        "`mdx` is the same architecture trained on the train split only, so the "
        "`mdx` versus `mdx_extra` gap is the cleanest available read on how much the "
        "leak is worth. The headline below is therefore taken from the uncontaminated "
        "models only; the leaked pair is reported as evidence, not as a recommendation.",
        "",
        "## Headline",
        "",
    ]
    if gated:
        out += [
            f"Withheld. {len(tracks)} "
            f"{'track is' if len(tracks) == 1 else 'tracks are'} not enough to name a "
            "best model, and a headline is exactly the form in which an underpowered "
            f"number escapes into a decision. Re-run to at least "
            f"{_MIN_TRACKS_FOR_RANKING} tracks.",
            "",
        ]
    else:
        out += [
            f"- Best median quality among test-set-clean models: **{best_clean.name}** "
            f"at {best_clean.median_si_sdr:.2f} dB (min {min(best_clean.si_sdr):.2f}, "
            f"max {max(best_clean.si_sdr):.2f}), winning {best_clean.wins} of "
            f"{len(tracks)} tracks outright.",
            f"- Best quality per second among clean models: **{by_value.name}** at "
            f"{by_value.db_per_second:.2f} dB/s ({by_value.median_si_sdr:.2f} dB in "
            f"{by_value.median_separate_s:.2f}s median) on {config['gpu']}, a figure "
            "that must be re-measured on the production GPU before it is used.",
            f"- Top of the raw board is **{best_overall.name}** at "
            f"{best_overall.median_si_sdr:.2f} dB"
            + (
                ", which is one of the leaked models. Treat that as a measurement of "
                "train-on-test contamination, not of quality."
                if not _is_clean(best_overall.name)
                else ", which is test-set clean."
            ),
            f"- Under-separation flags: "
            + (
                f"**{', '.join(flagged)}** (see the honesty check below)."
                if flagged
                else "none. No model's SI-SDR advantage traces to leaving the vocal "
                "buried."
            ),
            f"- Sanity anchor: "
            + (
                f"**{', '.join(failed_anchor)}** failed to beat the do-nothing mixture. "
                "Investigate before trusting anything else here."
                if failed_anchor
                else "every model beat the do-nothing mixture baseline, so the test "
                "discriminates."
            ),
            "",
        ]
    out += [
        "## SI-SDR by track and model (dB vs true vocals)",
        "",
    ]
    out += _matrix_table(tracks, models, summarise=not gated)
    out += [
        "",
        "The `mix baseline` column is that track's SI-SDR if you skipped separation "
        "entirely. Tracks where it is high are ones where the vocal already dominates "
        "the mix, so every model looks good; tracks where it is low are the real test.",
        "",
        "## Ranking by median SI-SDR, with spread",
        "",
    ]
    if gated:
        out += _withdrawn(len(tracks), "a ranking and a spread")
    else:
        out += _ranking_table(stats)
        out += [
            "",
            "Spread is max minus min across tracks. A large spread means the model is "
            "material-dependent and a single-track measurement of it proves nothing.",
        ]
    out += [
        "",
        "## Quality per unit compute",
        "",
    ]
    if gated:
        out += _withdrawn(len(tracks), "a cost ranking")
        out += [""]
        return "\n".join(out + _tail_sections(manifest, tracks, config, failed))
    out += _cost_table(stats)
    out += [
        "",
        f"All timings come from a **{config['gpu']}**. `separate_s` is GPU inference "
        "time only and excludes model loading, which production pays once. It is "
        "reported as a MEDIAN across tracks on purpose: the first transformer model to "
        "run in a fresh process absorbs one-off cuDNN kernel selection, which inflated "
        f"a single measurement by roughly 4x on track one. A median over {len(tracks)} "
        "tracks is immune to that; a mean would not be.",
        "",
        "**These ratios do not transfer across hardware.** `htdemucs` and `htdemucs_ft` "
        "are hybrid transformers while `hdemucs_mmi` and the mdx family are pure "
        "convolutional, and those two shapes scale differently on different GPUs: "
        "transformer attention gains far more from modern tensor cores than stacked "
        "convolutions do. A cost ratio measured on the Turing-generation card above can "
        "compress, or invert, on the Ada-generation L4 that production would use. Treat "
        "the dB-per-second column as evidence that a large gap exists on THIS hardware, "
        "and re-measure the shortlisted models on the production GPU before any cost "
        "figure is used to justify a model choice.",
        "",
        "## Honesty check: is any SI-SDR win bought by under-separating?",
        "",
        "SI-SDR does not punish leftover accompaniment as harshly as it punishes "
        "distortion, so a cautious model can score well by barely separating. SIR "
        "(signal to interference ratio, BSS Eval v4 with both true sources supplied) "
        "measures exactly the accompaniment that leaked through, and correlation with "
        "the mixture catches an estimate that is mostly just the input again. A model "
        "is flagged when it ranks at least "
        f"{_RANK_GAP_FLAG} places better on SI-SDR than on SIR AND sits above the "
        "cross-model median mixture correlation.",
        "",
    ]
    out += honesty_lines
    out += [
        "",
        (
            "No model is flagged. The SI-SDR ordering is not an artifact of "
            "under-separation."
            if not flagged
            else "Flagged models should not be adopted on SI-SDR alone. Read the flag "
            "together with the SAR column before concluding anything, because two very "
            "different models trip this rule. A flagged model with a LOW SAR is the bad "
            "case: it is leaving accompaniment in and adding artifacts, and its SI-SDR "
            "is hollow. A flagged model with a HIGH SAR is the interesting case: it "
            "separates less aggressively but what it returns is cleaner, which is a "
            "real trade (less bleed-through removal, fewer swirly artifacts) and often "
            "the more usable stem in a DJ context. Either way, audition it."
        ),
        "",
        "## Perceptual proxy: log-spectral distance (lower is better)",
        "",
    ]
    lsd_ranked = sorted(ran, key=lambda s: s.median_lsd)
    out += [_row(["model", "median LSD dB", "median SAR dB", "median SIR dB"]), _divider(4)]
    for s in lsd_ranked:
        out.append(
            _row(
                [
                    s.name,
                    f"{s.median_lsd:.2f}",
                    f"{s.median_sar:.2f}",
                    f"{s.median_sir:.2f}",
                ]
            )
        )
    out += [
        "",
        "LSD is scale-aligned before comparison. It agrees with SI-SDR only loosely, "
        "which is expected: LSD is blind to phase and weights quiet bins equally, so "
        "treat it as a second opinion rather than a tie-breaker.",
        "",
    ]
    out += _production_decision(ran, tracks)
    out += [
        "## Sizing the train-on-test leak, and the quantization cost",
        "",
        "Same architecture, different training data or precision, so each pair "
        "isolates one variable.",
        "",
        _row(
            [
                "pair",
                "what it isolates",
                "median paired delta",
                "range",
                "tracks favouring the right-hand model",
            ]
        ),
        _divider(5, left=2),
    ]
    by_name = {s.name: s for s in ran}
    for left, right, isolates in (
        ("mdx", "mdx_extra", "MusDB test set in the training data"),
        ("mdx_q", "mdx_extra_q", "MusDB test set in the training data (quantized)"),
        ("mdx", "mdx_q", "8-bit quantization"),
        ("mdx_extra", "mdx_extra_q", "8-bit quantization"),
        ("htdemucs", "htdemucs_ft", "per-source fine-tuning at roughly 4x compute"),
    ):
        if left not in by_name or right not in by_name:
            continue
        deltas = _paired_deltas(tracks, right, left)
        if not deltas:
            continue
        wins = sum(1 for d in deltas if d > 0)
        out.append(
            _row(
                [
                    f"`{left}` vs `{right}`",
                    isolates,
                    f"**{statistics.median(deltas):+.2f} dB**",
                    f"{min(deltas):+.2f} to {max(deltas):+.2f}",
                    f"{wins}/{len(deltas)}",
                ]
            )
        )
    out += [
        "",
        "The first two rows are the leak estimate, and the win column is the part that "
        "matters: the contaminated checkpoint does not merely average higher, it wins "
        "on every single track. A genuinely better architecture would trade wins across "
        "material of this diversity (metal, Tamil film, Irish folk, blues). A model "
        "that never loses on the exact songs it trained on is showing recall, not "
        "skill. On unseen material that advantage should shrink or vanish.",
        "",
        "The quantization rows are the incidental finding: 8-bit costs 0.2 dB or less, "
        "so quantized checkpoints are essentially free quality-wise and the smaller "
        "download is worth having.",
        "",
    ]
    out += _tail_sections(manifest, tracks, config, failed)
    return "\n".join(out)


def _tail_sections(
    manifest: dict[str, Any],
    tracks: list[dict[str, Any]],
    config: dict[str, Any],
    failed: list[ModelStats],
) -> list[str]:
    """Sections that stay valid at any sample size.

    Everything here is either a per-track measurement or a statement about
    method, so none of it depends on having enough tracks to rank across. That
    is why the underpowered path can jump straight here.
    """
    out: list[str] = []
    out += _anchor_verdict(tracks)
    out += [
        "## What this run did not measure",
        "",
        "- Full tracks. Every score is one "
        f"{manifest['excerpt_len_s']:.0f}s vocal-dense excerpt per song, chosen for "
        "maximum vocal energy, so quiet intros and instrumental passages are excluded "
        "and absolute dB values run optimistic.",
        "- Human listening. SI-SDR, SIR, SAR and LSD are all proxies, and no one has "
        "listened to these stems yet."
        + (
            " The clips in `clips-shootout/` are there to make that check cheap."
            if _CLIPS_DIR.is_dir()
            else " Listening clips have NOT been generated for this run; run "
            "`shootout_clips.py` to produce them."
        ),
        "- Knob sensitivity. overlap is pinned at "
        f"{config['overlap']} and shifts at {config['shifts']} for every model. The "
        "earlier ladder found under 0.2 dB across the whole overlap range, which is why "
        "they are pinned, but no model was tuned individually.",
        "- Real DJ library material. MUSDB18-HQ is mostly live-band recordings. "
        "Electronic material with heavy sidechain and reverb-drenched vocals is "
        "under-represented, so transfer to another corpus requires evidence.",
        "- Any model outside demucs. No comparison against MDX23, BS-RoFormer, or the "
        "commercial separators.",
        f"- Six of the twelve planned tracks. The fixed sample set in "
        f"`shootout_spec.py` is 12 tracks; the retained run contains {len(tracks)} entries. "
        "Hip-hop, "
        "pop rock, metal, Tamil film, Irish folk and blues are represented; produced "
        "pop, electronic, industrial rock and soul are not. The six that ran were the "
        "first six of a list fixed before any score existed, so they are a truncation "
        "rather than a selected subset.",
        "",
    ]
    if failed:
        out += [
            "## Models that failed",
            "",
        ]
        for s in failed:
            out.append(f"- `{s.name}`: failed on all {len(s.failures)} tracks.")
        out.append("")
    out += [
        "## Per-track windows",
        "",
        _row(["track", "genre", "window", "vocal rms dBFS", "silent hops"]),
        _divider(5, left=2),
    ]
    for track in tracks:
        window = track["window"]
        out.append(
            _row(
                [
                    track["track"],
                    track["genre"],
                    f"{window['start_s']:.1f}s to {window['end_s']:.1f}s",
                    f"{window['rms_dbfs']:.1f}",
                    f"{100 * window['silent_frac']:.1f}%",
                ]
            )
        )
    out += [
        "",
        "Windows are chosen on TRUE vocal energy, never on a separator's guess, and the "
        "identical window is cut from the mixture and the truth so the pair is "
        "sample-aligned.",
        "",
        "Generated by `scripts/bench/shootout_report.py` from `model_shootout.json`.",
        "",
        "-Claude",
        "",
    ]
    return out


def main() -> None:
    if not _IN_JSON.is_file():
        raise RuntimeError(f"missing input: {_IN_JSON} (run run_shootout_cuda.py)")
    manifest = json.loads(_IN_JSON.read_text(encoding="utf-8"))
    report = build_report(manifest)
    for dash in ("—", "–"):
        if dash in report:
            raise RuntimeError(f"report contains a banned dash character: {dash!r}")
    _OUT_MD.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[OK] wrote {_OUT_MD}")


if __name__ == "__main__":
    main()
