"""Protocol + dataclasses shared by every open-dj vendor adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


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


@runtime_checkable
class OpenDjAdapter(Protocol):
    """Structural interface every module-family vendor adapter implements.

    Adapters are invoked as modules (``apps.open_dj.adapters.rekordbox``)
    rather than as classes so the static module-level namespace matches the
    CLI's ``--adapter`` arg. Each module exposes at least
    :func:`export_library`; write-capable adapters also expose
    :func:`import_library`.

    That last paragraph was the whole contract and none of it was checkable:
    the Protocol declared ``name`` alone, so a module missing
    ``export_library`` satisfied it and ``apps.open_dj.registry`` would still
    hand it to the CLI, which fails at the call. ``export_library`` is
    declared here so the claim is structural, and the Protocol is
    ``runtime_checkable`` so ``tests/open_dj/test_adapter_registry.py`` can
    assert every registry entry against the family it declares.

    This is the module half of the two-family split the phase-4 gate wants
    collapsed onto the class Protocol (:class:`apps.open_dj.Adapter`). It is
    not the collapse: that needs ``read``/``write`` against real vendor files
    for rekordbox and djay, and their write path is deliberately deferred to
    the Phase 4 6-rail harness. Declaring the module family honestly is what
    makes the remaining gap visible instead of implied.

    Keyword-only, because both implementations are and the CLI calls them
    that way.
    """

    name: str

    def export_library(
        self,
        *,
        source_path: Path | None = ...,
        out_path: Path | None = ...,
        include_cues: bool = ...,
    ) -> ExportResult: ...
