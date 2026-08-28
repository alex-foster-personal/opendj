"""Select derived media roots without mixing local and remote libraries.

The ordinary Mac application owns ``data/state/stems``.  A remote library is
a replica and keeps its derived media below ``MDT_CRATE_ROOT``.  Keeping this
decision in one module prevents agentbox paths from leaking into stem parsing,
the browser API, or local development.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apps.shared.library_mode import crate_root, library_mode
from apps.shared.paths import STATE_DIR


@dataclass(frozen=True)
class StemStorage:
    """Ordered read roots plus the root used for newly generated bundles."""

    roots: tuple[Path, ...]
    write_root: Path
    remote: bool


def stem_storage(
    *,
    environ: Mapping[str, str] | None = None,
    state_dir: Path = STATE_DIR,
) -> StemStorage:
    """Return stem storage for the explicitly selected library mode."""

    if library_mode(environ=environ) == "local":
        demucs = Path(state_dir) / "stems"
        return StemStorage(
            roots=(demucs, Path(state_dir) / "stems-roformer-spike"),
            write_root=demucs,
            remote=False,
        )

    remote_root = crate_root(environ=environ) / "derived" / "stems"
    demucs = remote_root / "demucs4"
    return StemStorage(
        roots=(demucs, remote_root / "roformer2"),
        write_root=demucs,
        remote=True,
    )


def ensure_stem_storage(storage: StemStorage) -> None:
    """Materialise the selected cache roots, never an alternative mode's roots."""

    for root in storage.roots:
        root.mkdir(parents=True, exist_ok=True)


__all__ = ["StemStorage", "ensure_stem_storage", "stem_storage"]
