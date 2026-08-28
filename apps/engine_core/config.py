"""Engine configuration + boot preflight.

No module-level paths and no import-time app construction: every path is
derived from an :class:`EngineConfig` the caller builds. The env contract
(``MDT_DATA_DIR``, ``MUSIC_DJ_STATE_BACKEND``) is applied through the single
choke point :func:`apply_env_contract`, which MUST run before the first
``apps.webui`` import -- a documented temporary shim that dies when the
legacy modules stop reading env at import time.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

ENGINE_VERSION: str = "0.1.0"

DATA_DIR_ENV: str = "MDT_DATA_DIR"
STATE_BACKEND_ENV: str = "MUSIC_DJ_STATE_BACKEND"
CONCURRENCY_ENV: str = "WEB_CONCURRENCY"

# Lane A's fail-closed state backend reads this; branches without it ignore
# the value. ``sqlite`` matches the legacy default factory, which prefers a
# SqliteBackend whenever state.db exists.
DEFAULT_STATE_BACKEND: str = "sqlite"

# The decoy-ledger guard: the engine refuses to serve a data dir that still
# carries the fan-out ledger.
PROGRESS_LEDGER_NAME: str = "progress-tree.yaml"


class EngineBootError(RuntimeError):
    """Refusal to boot. The message always names the offending condition."""


@dataclass(frozen=True)
class EngineConfig:
    """Everything the chassis needs in order to place its own files."""

    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8585

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def jobs_db(self) -> Path:
        """Jobs live in their OWN file, never inside state.db."""
        return self.state_dir / "jobs.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".engine.lock"


def build_config(data_dir: str | Path, host: str, port: int) -> EngineConfig:
    """Validate CLI input into a config. Relative data dirs are rejected."""
    resolved = Path(data_dir).expanduser()
    if not resolved.is_absolute():
        raise EngineBootError(
            f"--data-dir must be an absolute path, got {str(data_dir)!r}"
        )
    if not 1 <= port <= 65535:
        raise EngineBootError(f"--port must be 1..65535, got {port}")
    return EngineConfig(data_dir=resolved, host=host, port=port)


def assert_single_worker(
    workers: int, *, environ: MutableMapping[str, str] | None = None
) -> None:
    """The engine owns a singleton lock, so a second worker is a bug.

    A second uvicorn worker is a second engine process racing for the same
    flock and the same jobs db. Refuse loudly rather than let the loser
    crash-loop after the socket is already bound.
    """
    env = os.environ if environ is None else environ
    if workers != 1:
        raise EngineBootError(
            f"engine is single-worker by design; --workers={workers} refused"
        )
    raw = env.get(CONCURRENCY_ENV)
    if raw is not None and str(raw).strip() != "":
        raise EngineBootError(
            f"{CONCURRENCY_ENV}={raw!r} is set; the engine is single-worker by "
            "design. Unset it before booting."
        )


def assert_no_progress_ledger(data_dir: Path) -> None:
    """Refuse to serve a data dir that still holds the fan-out ledger.

    Top-level check only: a recursive scan of a real library data dir costs
    unbounded IO at boot, and the ledger has exactly one canonical location.
    """
    ledger = data_dir / PROGRESS_LEDGER_NAME
    if ledger.exists():
        raise EngineBootError(
            f"{ledger} exists; the engine does not serve the fan-out ledger. "
            "Point --data-dir elsewhere or remove the file."
        )


def apply_env_contract(
    cfg: EngineConfig, *, environ: MutableMapping[str, str] | None = None
) -> None:
    """Set the env the legacy modules read at import time.

    ``MDT_DATA_DIR`` comes from ``--data-dir`` and always wins.
    ``MUSIC_DJ_STATE_BACKEND`` is only defaulted when unset, so an operator
    who chose a backend keeps it.
    """
    env = os.environ if environ is None else environ
    env[DATA_DIR_ENV] = str(cfg.data_dir)
    if not str(env.get(STATE_BACKEND_ENV, "")).strip():
        env[STATE_BACKEND_ENV] = DEFAULT_STATE_BACKEND


def prepare_layout(cfg: EngineConfig) -> None:
    """Create ONLY what the chassis itself owns: the state dir for jobs.db.

    Library data is never fabricated here -- an empty data dir stays empty
    apart from the lock file and the jobs db.
    """
    cfg.state_dir.mkdir(parents=True, exist_ok=True)


__all__ = [
    "CONCURRENCY_ENV",
    "DATA_DIR_ENV",
    "DEFAULT_STATE_BACKEND",
    "ENGINE_VERSION",
    "PROGRESS_LEDGER_NAME",
    "STATE_BACKEND_ENV",
    "EngineBootError",
    "EngineConfig",
    "apply_env_contract",
    "assert_no_progress_ledger",
    "assert_single_worker",
    "build_config",
    "prepare_layout",
]
