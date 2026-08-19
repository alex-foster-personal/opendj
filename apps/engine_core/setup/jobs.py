"""Registration of the one job kind first-run setup owns.

The jobs chassis ships zero kinds on purpose -- "an enqueue for an
unregistered kind is a loud 400, not a job that sits queued forever" -- and
expects each kind to register itself. Importing this module is that
registration; :mod:`apps.engine_core.setup.api` imports it, and the engine
app imports the router.

The argv builder is the ONLY place the worker command line is written, so
an agent reading ``worker_argv`` sees exactly what the UI runs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from apps.engine_core.jobs.runner import register_worker

SETUP_IMPORT_KIND: str = "setup.import-rekordbox"


class SetupPayloadError(ValueError):
    """The job payload is not one this worker can be built from."""


def build_argv(payload: dict[str, Any]) -> list[str]:
    """``payload -> argv``. Validated here so a bad enqueue 400s, not hangs."""
    data_dir = payload.get("data_dir")
    if not isinstance(data_dir, str) or not data_dir.strip():
        raise SetupPayloadError(
            f"{SETUP_IMPORT_KIND} payload needs a 'data_dir' string, got "
            f"{data_dir!r}"
        )
    if not Path(data_dir).is_absolute():
        raise SetupPayloadError(
            f"{SETUP_IMPORT_KIND} payload 'data_dir' must be absolute, got "
            f"{data_dir!r}"
        )
    argv = [
        sys.executable,
        "-m",
        "apps.engine_core.setup.worker",
        "--data-dir",
        data_dir,
    ]

    source = payload.get("source")
    if source is not None:
        if not isinstance(source, str) or not source.strip():
            raise SetupPayloadError(
                f"{SETUP_IMPORT_KIND} payload 'source' must be a non-empty "
                f"string or absent, got {source!r}"
            )
        argv += ["--source", source]

    limit = payload.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise SetupPayloadError(
                f"{SETUP_IMPORT_KIND} payload 'limit' must be a positive "
                f"integer or absent, got {limit!r}"
            )
        argv += ["--limit", str(limit)]

    return argv


register_worker(SETUP_IMPORT_KIND, build_argv)


__all__ = ["SETUP_IMPORT_KIND", "SetupPayloadError", "build_argv"]
