"""Markdown table builders for the shootout report."""

from __future__ import annotations

import statistics
from typing import Any

from shootout_report_constants import _RANK_GAP_FLAG, _is_clean, _provenance
from shootout_report_model import ModelStats

# ----- table builders ---------------------------------------------------------


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _divider(count: int, left: int = 1) -> str:
    """Markdown alignment row. `left` leading columns are text, the rest numeric
    and right-aligned so decimal points line up down a column."""
    parts = ["---" if i < left else "---:" for i in range(count)]
    return "| " + " | ".join(parts) + " |"


def _matrix_table(tracks: list[dict[str, Any]], models: list[str], summarise: bool = True) -> list[str]:
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
            values = [t["results"][name]["si_sdr"] for t in tracks if "si_sdr" in t["results"].get(name, {})]
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
    ranked = sorted([s for s in stats if s.ran], key=lambda s: s.median_si_sdr, reverse=True)
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
    ranked = sorted([s for s in stats if s.ran], key=lambda s: s.db_per_second, reverse=True)
    cheapest = min(s.median_separate_s for s in ranked)
    header = ["model", "median SI-SDR", "median separate_s", "x cheapest", "dB per second"]
    lines = [_row(header), _divider(len(header))]
    lines.extend(
        _row(
            [
                s.name,
                f"{s.median_si_sdr:.2f}",
                f"{s.median_separate_s:.2f}",
                f"{s.median_separate_s / cheapest:.2f}x",
                f"**{s.db_per_second:.2f}**",
            ]
        )
        for s in ranked
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
