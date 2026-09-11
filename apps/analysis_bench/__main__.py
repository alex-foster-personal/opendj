"""`python -m apps.analysis_bench` -- see `cli.main` for the command surface."""

from __future__ import annotations

import sys

from apps.analysis_bench.cli import main

if __name__ == "__main__":
    sys.exit(main())
