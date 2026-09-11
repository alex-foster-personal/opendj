"""Human-readable report printing for amdahl_report.py.

A separate module on purpose: amdahl_report.py sits at the 600-line
file_size ratchet floor (ops/quality/baseline.json), so the text-report
formatting (no decision logic, just `print()` calls over already-computed
data) lives here instead of tripping file_size.over_limit_python.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

try:
    from scripts.perf.perf_log_model import amdahl_speedup
except ImportError:
    from perf_log_model import amdahl_speedup  # type: ignore[import-not-found,no-redef]

if TYPE_CHECKING:
    from scripts.perf.amdahl_report import LoadRecord


def _ceiling(parallel_fraction: float) -> float:
    return amdahl_speedup(parallel_fraction, math.inf)


def print_per_load(records: list[LoadRecord]) -> None:
    print()
    print("Per load (wall ms; p = parallelizable fraction of the load's own wall)")
    print(
        "  deck  sid           total   stems   parallel   serial      p   ceiling  "
        "fetch-concurrency"
    )
    print("  " + "-" * 92)
    for record in records:
        fraction = record.parallel_fraction
        concurrency = record.fetch_concurrency
        ceil = (
            "UNKNOWN"
            if record.anomalous or record.ceiling_unknown
            else f"{_ceiling(fraction):.2f}x"
        )
        print(
            "  {deck:>4}  {sid:<12} {total:>7.0f} {stems:>7} {par:>10.0f} "
            "{ser:>8.0f} {frac:>6.3f}  {ceil:>7}  {conc}{anom}".format(
                deck=record.deck if record.deck is not None else "-",
                sid=(record.sid or "-")[:12],
                total=record.combined_total_ms,
                stems=f"{record.stem_total_ms:.0f}" if record.stem_total_ms is not None else "-",
                par=record.combined_parallel_ms,
                ser=record.combined_total_ms - record.combined_parallel_ms,
                frac=fraction,
                ceil=ceil,
                conc=f"{concurrency:.2f}x" if concurrency is not None else "-",
                anom="  ANOMALOUS, excluded from aggregate"
                if record.anomalous
                else "  contested fetch group, ceiling withheld"
                if record.ceiling_unknown
                else "",
            )
        )


def print_aggregate(summary: dict[str, Any], notes: dict[str, Any]) -> None:
    print()
    print("Aggregate (pooled wall time, not an average of per-load fractions)")
    print(
        f"  loads modeled          {summary['loads']} of {notes['deck_load_rows']} "
        f"deck-load rows ({notes['incomplete_loads']} never reached `total`, "
        f"{summary['anomalous_loads_excluded']} anomalous and excluded)"
    )
    print(f"  total wall             {summary['total_ms']:.0f} ms")
    print(f"  median load            {summary['median_load_ms']:.0f} ms")
    print(
        f"  parallelizable         {summary['parallelizable_ms']:.0f} ms "
        f"({summary['parallel_fraction'] * 100:.1f}%)"
    )
    # Contested ms is pooled into `serial_ms` (R5), but printing it bare would
    # read as a proven verdict over ms this model never measured a floor for
    # (Codex P1/BLOCKING, #705, perf_log_model.py:451). `contested_ms` names
    # the unproven slice explicitly instead of withholding it.
    contested_caveat = (
        f"  ({summary['contested_ms']:.0f} ms of it from "
        f"{summary['ceiling_unknown_loads']} contested load(s), unproven -- #705)"
        if summary["ceiling_unknown_loads"]
        else ""
    )
    print(
        f"  serial                 {summary['serial_ms']:.0f} ms "
        f"({summary['serial_fraction'] * 100:.1f}%){contested_caveat}"
    )
    print()
    print("  Amdahl ceiling on END-TO-END load time")
    if summary["speedup"] is None:
        print(
            f"    UNKNOWN -- {summary['ceiling_unknown_loads']} of {summary['loads']} clean "
            "load(s) have a contested fetch group (#705): no ceiling is printed "
            "rather than a guessed one."
        )
    else:
        for label, value in summary["speedup"].items():
            print(f"    {label:<22} {value:.3f}x")
        print(
            f"    parallel work free   {summary['ceiling_speedup']:.3f}x   "
            f"(floor {summary['floor_ms_if_parallel_were_free']:.0f} ms, the serial part)"
        )
    if summary["median_fetch_concurrency"] is not None:
        print()
        print(
            "  median fetch concurrency "
            f"{summary['median_fetch_concurrency']:.2f}x  "
            "(sum of the fetch group's members over its wall; 1.00x would mean "
            "no overlap at all)"
        )


def print_notes(notes: dict[str, Any], records: list[LoadRecord]) -> None:
    print()
    print("Notes")
    ceiling_withheld = sum(
        1 for record in records if record.ceiling_unknown and not record.anomalous
    )
    if ceiling_withheld:
        print(
            f"  {ceiling_withheld} load(s) have a contested fetch group (#705): "
            "per-load AND pooled ceiling withheld, still counted in total/serial ms."
        )
    if notes["stem_rows"] == 0:
        if notes["stem_rows_failed_excluded"]:
            print(
                f"  {notes['stem_rows_failed_excluded']} stem-upgrade attempt(s) "
                "in this ring FAILED (deck-stems-fail) and were EXCLUDED, not "
                "counted as measured. Stem work was attempted and may account "
                "for most of the observed time -- not a verified 'no stem work'."
            )
        if notes["stem_rows_no_stems"]:
            print(
                f"  {notes['stem_rows_no_stems']} load(s) confirmed no stem "
                "bundle exists (deck-stems-none) and are counted as clean "
                "mix-only data, not excluded -- only the manifest probe ran, "
                "no stem work is hiding in these numbers."
            )
        if not (notes["stem_rows_failed_excluded"] or notes["stem_rows_no_stems"]):
            print(
                "  NO stem-upgrade rows in this ring, so every load here is "
                "MIX-ONLY. Stem fetch/decode/create work is ABSENT and "
                "UNMEASURED here, so these numbers are a ceiling for the mix "
                "path only, not for a stemmed load."
            )
    if notes["stem_rows"]:
        print(
            f"  {notes['loads_with_stems']} of {len(records)} modeled loads joined "
            f"to a stem-upgrade row ({notes['stem_rows']} stem rows in the ring)."
        )
    if notes["pending_stem_loads_excluded"]:
        print(
            f"  {notes['pending_stem_loads_excluded']} load(s) still had a stem "
            "upgrade PENDING when this ring ended, or were superseded by a later "
            "load of the same track/deck, and were EXCLUDED: an unmatched entry "
            "does not prove no stem work happened."
        )
    if notes["nonzero_residual_loads"]:
        print(
            f"  {notes['nonzero_residual_loads']} loads have a phase residual over "
            "0.5ms: the phases do not exactly re-add to `total`, which is stage "
            "rounding unless it is large."
        )
    if notes["unclassified_stages"]:
        print(
            "  UNCLASSIFIED stage names (present in the ring, no declared meaning "
            "in perf_log_model.py): " + ", ".join(notes["unclassified_stages"])
        )
    else:
        print("  every stage name in this ring is classified by perf_log_model.py.")
