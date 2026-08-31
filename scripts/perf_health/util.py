"""Shared time and log-scanning helpers used by every perf-health check."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import LOG_DATE_RE
from .models import PreconditionError


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11, floor is 3.9


def _age_hours(moment: datetime, now: datetime) -> float:
    return (now - moment).total_seconds() / 3600.0


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)  # noqa: UP017


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _window_label(days: int) -> str:
    return f"last {_plural(days, 'day')}"


def _describe_age(hours: float) -> str:
    if hours < 48:
        return f"{hours:.1f}h old"
    return f"{hours / 24:.1f}d old"


def _read_lines(path: Path) -> list[str]:
    """Read a JSONL sink, failing loudly if the path is not a readable file.

    An unreadable sink is the quiet rot this check exists to surface, so the OSError is
    turned into a named [ERROR] by the caller rather than being swallowed into a pass.
    """
    if path.is_dir():
        raise PreconditionError(f"{path} is a directory, expected a JSONL file")
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _log_files(
    log_dirs: tuple[Path, ...], prefix: str, days: int, now: datetime
) -> tuple[list[Path], list[Path]]:
    """Daily logs inside the window, plus the log roots that exist at all.

    The date comes from the filename, which is how apps/webui/server/client_logs.py names
    them, so a file's day is known without reading it.
    """
    cutoff = (now - timedelta(days=days)).date()
    matched: list[Path] = []
    present_roots: list[Path] = []
    for directory in log_dirs:
        if not directory.is_dir():
            continue
        present_roots.append(directory)
        for path in sorted(directory.glob(f"{prefix}-*.log")):
            stamp = LOG_DATE_RE.search(path.name)
            if stamp is None:
                continue
            # A log FILENAME carries a bare local date with no zone to honor; only the
            # .date() is used, so attaching a zone here would invent precision.
            day = datetime.strptime(stamp.group(1), "%Y-%m-%d").date()  # noqa: DTZ007
            if day >= cutoff:
                matched.append(path)
    return matched, present_roots
