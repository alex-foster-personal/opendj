"""Child process for one Re-analyze drain: ``queue_cli`` at the lowest priority.

Run by :mod:`apps.webui.server.backfill_drain`. Lowers its own scheduling
priority, then IS ``python -m apps.analysis.queue_cli``: same arguments, same
exit codes, same rows. A wrapper rather than ``preexec_fn``, because the drain
launches from a daemon thread and a ``preexec_fn`` callback in a threaded
parent can deadlock the forked child before ``exec`` (Python's own warning,
Codex review on PR #5054); the pool workers the runner spawns inherit the
lowered priority.

    python -m apps.webui.server.backfill_drain_job --db DB --json \
        run --batch-id B --backend own_beatgrid.backfill

-Claude
"""

from __future__ import annotations

import sys

from apps.analysis import queue_cli
from apps.shared.process_priority import lower_own_priority

NICENESS: int = 19


def main(argv: list[str] | None = None) -> int:
    lower_own_priority(NICENESS)
    return queue_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
