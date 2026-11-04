"""Where the local music-backup manifest lives (issue #5639).

The manifest maps each mirrored track to its asset-store path and is the route
back for a restore. Its local name is ``(private path)``
because the store host is config (``MDT_B2STORE_SSH_HOST``). The remote copy is
always ``<store root>/manifest.json``.

A leftover ``(private path)`` is renamed by hand when
the current name is absent (``(private path)``).

Stdlib only: imported by the PEP 723 backup scripts.
"""

from __future__ import annotations

from pathlib import Path

#: Repo-relative path of the local manifest.
MUSIC_MANIFEST_REL: Path = Path("(private path)")
