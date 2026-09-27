"""Turn a successful setup-import progress line into library invalidation.

The worker is a subprocess and cannot call ``events.publish``. The engine
registers :func:`on_setup_import_progress` as a progress observer so the web
UI's existing ``library.changed`` subscribers refetch without a reload.
"""

from __future__ import annotations

from typing import Any

from apps.shared.events import publish


def on_setup_import_progress(job: dict[str, Any], line: dict[str, Any]) -> None:
    """Publish full-library invalidation when the worker reports completion."""
    progress = line.get("progress")
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        return
    if float(progress) < 1.0:
        return

    publish("library.changed", {"kind": "tracks", "ids": []})

    payload = job.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("mode", "rekordbox") == "rekordbox":
        publish("library.changed", {"kind": "playlists", "ids": []})


__all__ = ["on_setup_import_progress"]
