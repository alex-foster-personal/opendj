"""Rich-formatted CLI replay for a recorded session.

Plan 12-03 Step 1. Reads ``manifest.json``, runs
:func:`apps.sets.classify.classify_session` if ``transitions.jsonl`` is
missing, then prints a summary + per-transition table.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.table import Table

from . import paths as sets_paths
from .classify import classify_session, read_transitions
from .sessions import get_session


# Human duration parser for ``--since 45m`` / ``--since 1h30m`` / ``--since 30s``.
_SINCE_RE = re.compile(r"^\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s*$", re.IGNORECASE)


def parse_since(value: str | None) -> float | None:
    """Convert ``45m``/``1h30m``/``90s`` into seconds.

    Returns ``None`` when ``value`` is ``None`` or empty. Raises
    :class:`ValueError` on unparseable input.
    """
    if value is None or value == "":
        return None
    m = _SINCE_RE.match(value)
    if not m or not any(m.groups()):
        # Accept raw integer seconds too.
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"unparseable --since {value!r}") from exc
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return float(hours * 3600 + minutes * 60 + seconds)


def replay(
    session_id: str,
    *,
    output_format: str = "rich",
    since: str | None = None,
    class_filter: str | None = None,
    sets_root: Path | None = None,
    console: Console | None = None,
    ensure_transitions: bool = True,
) -> int:
    """Render a session's transitions; returns a shell exit code."""
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    session = get_session(session_id, sets_root=root)
    if session is None:
        (console or Console()).print(
            f"[red]no session {session_id!r} under {root}[/red]"
        )
        return 2

    transitions = session.transitions
    if not transitions and ensure_transitions:
        classify_session(session_id, sets_root=root)
        transitions = read_transitions(session_id, sets_root=root)

    since_s = parse_since(since)
    if since_s is not None:
        # Keep transitions whose t_change_s is within the last ``since_s``.
        if transitions:
            latest = max(t.get("t_change_s", 0.0) for t in transitions)
            cutoff = latest - since_s
            transitions = [t for t in transitions if t.get("t_change_s", 0.0) >= cutoff]
    if class_filter:
        transitions = [
            t for t in transitions if t.get("predicted_class") == class_filter
        ]

    if output_format == "jsonl":
        for t in transitions:
            print(json.dumps(t, separators=(",", ":"), sort_keys=True))
        return 0

    cons = console or Console()
    _render_rich(cons, session, transitions)
    return 0


def _render_rich(console: Console, session, transitions: list[dict[str, Any]]) -> None:
    summary = session.summary
    duration_s = summary.duration_s or 0.0
    mins, secs = divmod(int(duration_s), 60)
    hours, mins = divmod(mins, 60)
    console.print(
        f"[bold]Session {summary.session_id}[/bold] "
        f"started={summary.started_at} ended={summary.ended_at} "
        f"duration={hours}h{mins:02d}m{secs:02d}s"
    )
    console.print(
        f"{summary.event_count} events, {summary.transition_count} transitions, "
        f"share={summary.share_state}"
    )

    table = Table(title=f"Transitions ({len(transitions)})")
    table.add_column("#", justify="right")
    table.add_column("t_change (s)", justify="right")
    table.add_column("from -> to")
    table.add_column("decks")
    table.add_column("class")
    table.add_column("overlap_s", justify="right")
    table.add_column("conf", justify="right")
    for t in transitions:
        feats = t.get("features", {})
        table.add_row(
            str(t.get("idx", "?")),
            f"{t.get('t_change_s', 0.0):.1f}",
            f"{t.get('from_track') or '-'} -> {t.get('to_track') or '-'}",
            f"{t.get('from_deck') or '?'} -> {t.get('to_deck') or '?'}",
            t.get("predicted_class", "?"),
            f"{feats.get('overlap_s', 0.0):.2f}",
            f"{t.get('confidence', 0.0):.2f}",
        )
    console.print(table)


__all__ = ["parse_since", "replay"]
