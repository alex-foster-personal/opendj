"""What a packaged build carries for telemetry, and how a tester turns it off.

A dmg ships its Sentry DSN in ``telemetry.json`` at the payload root
(``scripts/payload_telemetry.py`` writes it; the payload launcher exports
its path as ``OPENDJ_BUNDLED_TELEMETRY``). This module reads that file and
the opt-out marker, and hands both to :func:`decide_telemetry` as plain
values so the decision itself stays pure and testable.

Stdlib only, no SDK: it runs on every boot, including the ``off`` ones.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Set by the payload launcher to the baked file's path. Unset in a checkout.
BUNDLED_TELEMETRY_ENV: str = "OPENDJ_BUNDLED_TELEMETRY"

#: A file a tester creates in the engine data directory to switch telemetry
#: off without an env var they have no shell to set. Presence is the whole
#: signal; contents are ignored. Named so ``ls`` explains it.
OPT_OUT_MARKER: str = "telemetry-opt-out"


class BundledTelemetryError(RuntimeError):
    """The launcher named a bundled file that cannot be read as one."""


@dataclass(frozen=True)
class BundledTelemetry:
    dsn: str
    path: Path
    #: The open-dj-fe DSN the replay loader URL is derived from (OBS-06).
    frontend_dsn: str | None = None


def load_bundled_telemetry(environ: Mapping[str, str]) -> BundledTelemetry | None:
    """The baked DSN, or None when this boot has no bundle (every checkout).

    Raises :class:`BundledTelemetryError` when the launcher DID name a file
    and it is missing, unreadable, or carries no DSN: the payload build
    verifies the file, so that state is a damaged install, and the caller
    decides how loud to be about it.
    """
    raw = (environ.get(BUNDLED_TELEMETRY_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundledTelemetryError(
            f"{BUNDLED_TELEMETRY_ENV}={path} cannot be read as JSON: {exc}"
        ) from exc
    dsn = str(data.get("dsn") or "").strip() if isinstance(data, dict) else ""
    if not dsn:
        raise BundledTelemetryError(
            f"{BUNDLED_TELEMETRY_ENV}={path} carries no 'dsn'; it was not written "
            "by scripts/payload_telemetry.py"
        )
    frontend = str(data.get("frontend_dsn") or "").strip() if isinstance(data, dict) else ""
    return BundledTelemetry(dsn=dsn, path=path, frontend_dsn=frontend or None)


def opt_out_marker_path(data_dir: Path) -> Path:
    return data_dir / OPT_OUT_MARKER


def opt_out_reason(data_dir: Path | None) -> str | None:
    """The opt-out marker's path when it exists, else None."""
    if data_dir is None:
        return None
    marker = opt_out_marker_path(data_dir)
    if marker.exists():
        return f"{marker} exists"
    return None


__all__ = [
    "BUNDLED_TELEMETRY_ENV",
    "OPT_OUT_MARKER",
    "BundledTelemetry",
    "BundledTelemetryError",
    "load_bundled_telemetry",
    "opt_out_marker_path",
    "opt_out_reason",
]
