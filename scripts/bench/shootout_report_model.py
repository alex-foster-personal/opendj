"""Per-model aggregate statistics across shootout tracks."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any


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
        scored = {name: result["si_sdr"] for name, result in track["results"].items() if "si_sdr" in result}
        if scored:
            winners[max(scored, key=lambda k: scored[k])] += 1

    stats: list[ModelStats] = []
    for name in manifest["models"]:
        rows = [track["results"].get(name, {}) for track in tracks]
        good = [row for row in rows if "si_sdr" in row]
        failures = [track["track"] for track, row in zip(tracks, rows, strict=False) if "si_sdr" not in row]
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
