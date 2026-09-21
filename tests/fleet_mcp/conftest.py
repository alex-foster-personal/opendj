"""Fake runners shared by the dispatch-connector tests."""

from __future__ import annotations

from collections.abc import Sequence

from apps.fleet_mcp.runner import Completed


def fake_runner(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    """A runner that always reports the same measured outcome."""

    def runner(argv: Sequence[str], *, timeout: float) -> Completed:
        return Completed(tuple(argv), returncode, stdout, stderr)

    return runner


def unknown_runner(reason: str = "nothing was measured"):
    """A runner that never manages to measure anything."""

    def runner(argv: Sequence[str], *, timeout: float) -> Completed:
        return Completed(tuple(argv), None, "", "", reason)

    return runner


def recording_runner(*, stdout: str = "", returncode: int = 0):
    """A measured runner that keeps every argv it was handed."""
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], *, timeout: float) -> Completed:
        calls.append(tuple(argv))
        return Completed(tuple(argv), returncode, stdout, "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner
