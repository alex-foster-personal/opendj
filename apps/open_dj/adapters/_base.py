"""Protocol + dataclasses shared by every open-dj vendor adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ExportResult:
    """Outcome of :meth:`OpenDjAdapter.export_library`."""

    document: dict
    tracks_count: int = 0
    playlists_count: int = 0
    cue_points_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    """Outcome of :meth:`OpenDjAdapter.import_library`.

    Dry-run runs populate :attr:`patch_csv_path` but leave the target DB
    untouched. Live runs populate :attr:`backup_path` + :attr:`reversal_path`
    per the 6-rail safety pattern.
    """

    dry_run: bool
    changes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    patch_csv_path: Path | None = None
    backup_path: Path | None = None
    reversal_path: Path | None = None


class OpenDjAdapter(Protocol):
    """Structural interface every vendor adapter implements.

    Adapters are invoked as modules (``apps.open_dj.adapters.rekordbox``)
    rather than as classes so the static module-level namespace matches the
    CLI's ``--adapter`` arg. Each module exposes at least
    :func:`export_library`; write-capable adapters also expose
    :func:`import_library`.
    """

    name: str
