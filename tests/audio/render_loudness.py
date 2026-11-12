"""Integrated loudness match assertion for offline render tests.

Compares EBU R128 integrated loudness (LUFS) of a source excerpt and its offline
render. Uses :mod:`apps.loudness.scan` (ffmpeg ``ebur128``); there is no fallback
measurement path and unreadable inputs never receive a fabricated value.

Offline render hygiene (0.5 LU) is named in (private path)
quality signal context and transition parity scoring; this helper centralises the
same tolerance for pytest and CLI callers.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.loudness.scan import LoudnessError, scan_file

DEFAULT_TOLERANCE_LU: float = 0.5
# Float subtraction on measured LUFS can exceed the nominal tolerance by a few ULPs.
TOLERANCE_BOUNDARY_EPS_LU: float = 1e-9


@dataclass(frozen=True)
class LoudnessPair:
    """Measured integrated loudness for source and render."""

    source_lufs: float
    render_lufs: float

    @property
    def delta_lu(self) -> float:
        return abs(self.source_lufs - self.render_lufs)


class RenderLoudnessMismatchError(AssertionError):
    """Source and render integrated loudness differ by more than the tolerance."""

    def __init__(self, *, source_lufs: float, render_lufs: float, tolerance_lu: float) -> None:
        delta = abs(source_lufs - render_lufs)
        super().__init__(
            f"render loudness mismatch: source integrated {source_lufs:.2f} LUFS, "
            f"render integrated {render_lufs:.2f} LUFS, delta {delta:.2f} LU "
            f"(tolerance {tolerance_lu:.2f} LU)"
        )
        self.source_lufs = source_lufs
        self.render_lufs = render_lufs
        self.tolerance_lu = tolerance_lu
        self.delta_lu = delta


def measure_integrated_lufs(path: Path) -> float:
    """Return integrated loudness (LUFS) for one readable audio file."""
    return scan_file(path).integrated_lufs


def compare_render_loudness(source: Path, render: Path) -> LoudnessPair:
    """Measure both files and return the pair. Raises :class:`LoudnessError` when unreadable."""
    return LoudnessPair(
        source_lufs=measure_integrated_lufs(source),
        render_lufs=measure_integrated_lufs(render),
    )


def assert_render_loudness_match(
    source: Path,
    render: Path,
    *,
    tolerance_lu: float = DEFAULT_TOLERANCE_LU,
) -> LoudnessPair:
    """Pass when integrated loudness differs by at most ``tolerance_lu`` LU.

    Raises :class:`RenderLoudnessMismatchError` when the delta exceeds the tolerance
    (message includes both measured LUFS values). Raises :class:`LoudnessError` when
    either file cannot be measured.
    """
    pair = compare_render_loudness(source, render)
    if pair.delta_lu > tolerance_lu + TOLERANCE_BOUNDARY_EPS_LU:
        raise RenderLoudnessMismatchError(
            source_lufs=pair.source_lufs,
            render_lufs=pair.render_lufs,
            tolerance_lu=tolerance_lu,
        )
    return pair


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="source audio file")
    parser.add_argument("render", type=Path, help="offline render of the source")
    parser.add_argument(
        "--tolerance-lu",
        type=float,
        default=DEFAULT_TOLERANCE_LU,
        help=f"maximum allowed |source-render| integrated loudness delta (default {DEFAULT_TOLERANCE_LU})",
    )
    return parser


def run_compare_command(args: argparse.Namespace) -> int:
    """Exit 0 on match, 1 on loudness mismatch, 2 on unreadable input."""
    try:
        pair = assert_render_loudness_match(args.source, args.render, tolerance_lu=args.tolerance_lu)
    except RenderLoudnessMismatchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except LoudnessError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(f"ok source={pair.source_lufs:.2f} LUFS render={pair.render_lufs:.2f} LUFS delta={pair.delta_lu:.2f} LU")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_compare_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
