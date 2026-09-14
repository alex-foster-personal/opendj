"""Replay a real error-sink JSONL through the Sentry quota guard (the round scorer).

    uv run python -m scripts.sentry_budget_replay <error-sink.jsonl> --max-per-day 100

Recomputes each row's id with the CURRENT classifier, drops perf-event console
mirrors, and feeds the shipped SentryBudget a clock taken from the row
timestamps, so the printed per-day counts are what that host would have sent.
Exits 1 when any day exceeds --max-per-day. Rounds are logged in
specs/sentry-quota-spec.md; always score them on the same fixture.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from apps.shared.telemetry.budget import (
    SENTRY_EVENTS_PER_DAY,
    SENTRY_EVENTS_PER_ID_PER_HOUR,
    SentryBudget,
    is_perf_console_mirror,
)
from apps.shared.telemetry.error_id import stable_error_id


class _RowClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client_kind(source_site: str) -> str:
    parts = source_site.split(":")
    return parts[1] if parts[0] == "client" and len(parts) > 1 else ""


def replay(rows: list[dict[str, str]]) -> tuple[Counter[str], Counter[str], int]:
    clock = _RowClock()
    budget = SentryBudget(
        per_id_per_hour=SENTRY_EVENTS_PER_ID_PER_HOUR,
        per_day=SENTRY_EVENTS_PER_DAY,
        clock=clock,
    )
    seen: Counter[str] = Counter()
    sent: Counter[str] = Counter()
    ids: set[str] = set()
    for row in rows:
        day = row["timestamp"][:10]
        seen[day] += 1
        message = row["message"]
        if is_perf_console_mirror(_client_kind(row["source_site"]), message):
            continue
        error_id = stable_error_id(source_site=row["source_site"], message=message)
        ids.add(error_id)
        clock.now = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
        if budget.admit(error_id):
            sent[day] += 1
    return seen, sent, len(ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sink", type=Path)
    parser.add_argument("--max-per-day", type=int, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.sink.read_text().splitlines() if line.strip()]
    rows.sort(key=lambda row: row["timestamp"])
    seen, sent, id_count = replay(rows)
    print(f"rows={len(rows)} distinct_ids_forwardable={id_count}")
    for day in sorted(seen):
        print(f"{day} sink={seen[day]:>6} sentry={sent[day]:>5}")
    worst = max(sent.values(), default=0)
    if worst > args.max_per_day:
        print(f"[ERROR] worst day sent {worst} > --max-per-day {args.max_per_day}")
        return 1
    print(f"[OK] worst day sent {worst} <= {args.max_per_day}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
