"""Composition root for ``python -m apps.transition_score`` and the legacy bench module entry point."""

from __future__ import annotations

from collections.abc import Sequence

from apps.autodj.http import build_service
from apps.transition_score.bench import main as bench_main


def main(argv: Sequence[str] | None = None) -> int:
    return bench_main(build_service(), argv)


if __name__ == "__main__":
    raise SystemExit(main())
