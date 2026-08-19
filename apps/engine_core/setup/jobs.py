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
    argv = [
        sys.executable,
        "-m",
        "apps.engine_core.setup.worker",
        "--data-dir",
        _data_dir(payload),
    ]
    argv += _source_args(payload)
    argv += _limit_args(payload)
    argv += _refresh_args(payload)
    return argv


def _reject(field: str, value: Any, requirement: str) -> SetupPayloadError:
    return SetupPayloadError(
        f"{SETUP_IMPORT_KIND} payload {field!r} {requirement}, got {value!r}"
    )


def _data_dir(payload: dict[str, Any]) -> str:
    """Absolute, or the worker imports into wherever the engine was started."""
    value = payload.get("data_dir")
    if not isinstance(value, str) or not value.strip():
        raise _reject("data_dir", value, "must be a non-empty string")
    if not Path(value).is_absolute():
        raise _reject("data_dir", value, "must be an absolute path")
    return value


def _source_args(payload: dict[str, Any]) -> list[str]:
    value = payload.get("source")
    if value is None:
        return []
    if not isinstance(value, str) or not value.strip():
        raise _reject("source", value, "must be a non-empty string or absent")
    return ["--source", value]


def _limit_args(payload: dict[str, Any]) -> list[str]:
    value = payload.get("limit")
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _reject(
            "limit", value, "must be a positive integer or absent"
        )
    return ["--limit", str(value)]


def _refresh_args(payload: dict[str, Any]) -> list[str]:
    value = payload.get("refresh_decrypt", False)
    if not isinstance(value, bool):
        raise _reject("refresh_decrypt", value, "must be a bool")
    return ["--refresh-decrypt"] if value else []


register_worker(SETUP_IMPORT_KIND, build_argv)


__all__ = ["SETUP_IMPORT_KIND", "SetupPayloadError", "build_argv"]
