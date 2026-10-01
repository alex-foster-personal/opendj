"""Child process for one auto-drain analysis job: one track, one worker.

Run by ``apps.webui.server.coverage_drain_analysis.analysis_job``. Lowers its
own scheduling priority, then IS ``python -m apps.analysis.run``: same
arguments, same exit codes, same rows. A wrapper rather than a ``nice``
prefix so the priority drop needs no platform binary.

    python -m apps.webui.server.coverage_analysis_job --backend librosa \
        --workers 1 --pairs-json P
"""
from __future__ import annotations

import os
import sys

from apps.analysis import run as analysis_run

NICENESS: int = 19


def main(argv: list[str] | None = None) -> int:
    os.nice(NICENESS)
    return analysis_run.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
