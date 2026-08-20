"""Per-step summary of the iteration-metrics store - the human view of what check 5 judges.

Reads ~/.local/share/mdt-iteration-metrics/metrics.jsonl through the SAME parser the
watchdog uses (scripts.ci_health_check._read_metrics), so a line this report can read is a
line check 5 can read, and a malformed store fails here exactly as loudly as it does there.

HONEST DENOMINATORS
    'n' is the number of records actually parsed for that step, never a requested count.
    'median' is over the records PRECEDING the newest one, which is the denominator check 5
    compares against - quoting a median that included the newest sample would understate
    every regression. Steps with fewer than ITERATION_SPEED_MIN_SAMPLE prior records are
    printed with a '-' ratio rather than a number, because no ratio is computable yet.

USAGE
    uv run --no-sync python -m scripts.iteration_metrics_report
    just iteration-metrics-report

Run as a MODULE, not a standalone PEP 723 script: it imports the watchdog's parser rather
than reimplementing it, so it needs the repo on sys.path. Two parsers over one store is
exactly how a report and its watchdog start disagreeing about what the numbers say.

EXIT CODES
    0   report printed (including when the store is empty)
    10  the store exists but is unreadable or malformed

-Claude
"""

from __future__ import annotations

import statistics
import sys

from scripts.ci_health_check import (
    ITERATION_METRICS_PATH,
    ITERATION_SPEED_MEDIAN_WINDOW,
    ITERATION_SPEED_MIN_SAMPLE,
    ITERATION_SPEED_REGRESSION_FACTOR,
    PreconditionError,
    _read_metrics,
)

EXIT_OK = 0
EXIT_PRECONDITION = 10

ROW = "{step:<38} {count:>5} {median:>10} {newest:>10} {ratio:>7}"


def main() -> int:
    try:
        metrics = _read_metrics(ITERATION_METRICS_PATH)
    except PreconditionError as exc:
        print(f"[ERROR] iteration-metrics-report: {exc}", file=sys.stderr)
        return EXIT_PRECONDITION

    if not metrics:
        print(f"[OK] iteration-metrics-report: {ITERATION_METRICS_PATH} holds no records yet")
        return EXIT_OK

    by_step: dict[str, list[float]] = {}
    for metric in metrics:
        by_step.setdefault(metric.step, []).append(metric.seconds)

    print(f"[OK] iteration-metrics-report: {len(metrics)} records in {ITERATION_METRICS_PATH}")
    print(ROW.format(step="step", count="n", median="median(s)", newest="newest(s)", ratio="ratio"))
    print("-" * 74)

    for step, seconds in sorted(by_step.items()):
        history = seconds[:-1][-ITERATION_SPEED_MEDIAN_WINDOW:]
        newest = seconds[-1]
        if len(history) < ITERATION_SPEED_MIN_SAMPLE:
            median_text, ratio_text = "-", "-"
        else:
            median = statistics.median(history)
            median_text = f"{median:.2f}"
            ratio_text = f"{newest / median:.2f}x"
        print(
            ROW.format(
                step=step[:38],
                count=len(seconds),
                median=median_text,
                newest=f"{newest:.2f}",
                ratio=ratio_text,
            )
        )

    print("-" * 74)
    print(
        f"[OK] a step alerts when its ratio exceeds {ITERATION_SPEED_REGRESSION_FACTOR:.1f}x "
        f"over at least {ITERATION_SPEED_MIN_SAMPLE} prior records"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
