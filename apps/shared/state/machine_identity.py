"""Stable per-machine identity for hub sync.

``machine_id`` is a uuid4 hex minted once per machine and stored in a plain
file at ``<data-dir>/machine-id`` (mode 0600), NOT in the state DB. That
split is the whole point: a DB restored from Litestream onto a different
machine must not inherit the old machine's identity and start impersonating
it in ``hub_changelog`` (specs/design_decision_05.md, section 1).

Nothing here guesses. An unknown platform, an unwritable data dir, a
corrupt id file or an ambiguous ``MDT_IS_HUB`` value all raise
:class:`MachineIdentityError` rather than falling back to a plausible value.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MACHINE_ID_FILENAME: str = "machine-id"
MACHINE_ID_MODE: int = 0o600
IS_HUB_ENV: str = "MDT_IS_HUB"

# sys.platform -> the machines.platform CHECK vocabulary.
_PLATFORM_BY_SYS_PLATFORM: dict[str, str] = {
    "darwin": "macos",
    "win32": "windows",
    "linux": "linux",
}


class MachineIdentityError(RuntimeError):
    """Machine identity could not be established. Never swallowed."""


@dataclass(frozen=True)
class MachineIdentity:
    """One row of ``machines`` as this process sees itself."""

    machine_id: str
    name: str
    platform: str
    is_hub: bool
    data_root: str


# ----- identity file -------------------------------------------------------


def machine_id_path(data_dir: Path) -> Path:
    """Location of the id file for ``data_dir``."""
    return Path(data_dir) / MACHINE_ID_FILENAME


def _validate_machine_id(raw: str, source: Path) -> str:
    candidate = raw.strip()
    if len(candidate) != 32 or any(c not in "0123456789abcdef" for c in candidate):
        raise MachineIdentityError(
            f"{source} does not contain a 32-char lowercase hex machine id "
            f"(got {raw!r}); refusing to guess. Delete the file to re-mint."
        )
    return candidate


def _write_machine_id(path: Path, machine_id: str) -> bool:
    """Create ``path`` with ``machine_id`` at 0600. False if it already exists."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, MACHINE_ID_MODE)
    except FileExistsError:
        return False
    except OSError as exc:
        raise MachineIdentityError(
            f"cannot write the machine id file at {path}: {exc}"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(machine_id)
    return True


def get_or_create_machine_id(data_dir: Path) -> str:
    """Return this machine's id, minting and persisting it on first call.

    Idempotent: a second call in any process on the same ``data_dir``
    returns the value already on disk. Raises
    :class:`MachineIdentityError` if the directory cannot be created or
    written, or if an existing file holds something that is not a uuid4 hex.
    """
    target_dir = Path(data_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MachineIdentityError(
            f"cannot create the data dir {target_dir} for the machine id: {exc}"
        ) from exc

    path = machine_id_path(target_dir)
    if _write_machine_id(path, uuid.uuid4().hex):
        return _validate_machine_id(path.read_text(encoding="utf-8"), path)
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MachineIdentityError(
            f"machine id file {path} exists but is unreadable: {exc}"
        ) from exc
    return _validate_machine_id(existing, path)


# ----- environment / platform ---------------------------------------------


def detect_platform() -> str:
    """This machine's ``machines.platform`` value. Fails on anything else."""
    platform = _PLATFORM_BY_SYS_PLATFORM.get(sys.platform)
    if platform is None:
        raise MachineIdentityError(
            f"unsupported sys.platform {sys.platform!r}; machines.platform "
            f"accepts only {sorted(_PLATFORM_BY_SYS_PLATFORM.values())}"
        )
    return platform


def is_hub_from_env(env: Optional[dict[str, str]] = None) -> bool:
    """True iff ``MDT_IS_HUB`` is exactly ``1``.

    Unset or ``0`` is False. Any other value raises rather than being read
    as falsey -- ``MDT_IS_HUB=true`` silently demoting the hub to a spoke is
    exactly the class of bug this project bans.
    """
    source = os.environ if env is None else env
    raw = source.get(IS_HUB_ENV)
    if raw is None or raw == "":
        return False
    elif raw == "1":
        return True
    elif raw == "0":
        return False
    raise MachineIdentityError(
        f"{IS_HUB_ENV}={raw!r} is not understood; use 1 or 0."
    )


def default_machine_name() -> str:
    """Friendly display name: the hostname, first label only."""
    hostname = socket.gethostname().strip()
    if not hostname:
        raise MachineIdentityError(
            "socket.gethostname() returned nothing; pass an explicit name "
            "to register_machine()."
        )
    return hostname.split(".")[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----- registration --------------------------------------------------------


def register_machine(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    name: Optional[str] = None,
    now: Optional[str] = None,
) -> MachineIdentity:
    """Upsert this machine into ``machines`` and return what was written.

    ``first_seen`` is stamped once and never rewritten; ``last_seen`` moves
    on every call, so this doubles as the liveness heartbeat. Idempotent.

    ``machines.name`` is UNIQUE: two machines sharing a hostname raise
    :class:`sqlite3.IntegrityError` here instead of quietly overwriting each
    other's fleet row.
    """
    identity = MachineIdentity(
        machine_id=get_or_create_machine_id(data_dir),
        name=name if name is not None else default_machine_name(),
        platform=detect_platform(),
        is_hub=is_hub_from_env(),
        data_root=str(Path(data_dir)),
    )
    stamp = now or _now_iso()
    conn.execute(
        """
        INSERT INTO machines(
            machine_id, name, platform, is_hub, data_root,
            first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(machine_id) DO UPDATE SET
            name      = excluded.name,
            platform  = excluded.platform,
            is_hub    = excluded.is_hub,
            data_root = excluded.data_root,
            last_seen = excluded.last_seen
        """,
        (
            identity.machine_id,
            identity.name,
            identity.platform,
            1 if identity.is_hub else 0,
            identity.data_root,
            stamp,
            stamp,
        ),
    )
    return identity


def load_machine(
    conn: sqlite3.Connection, machine_id: str
) -> Optional[MachineIdentity]:
    """Read one ``machines`` row back, or None if it was never registered."""
    row = conn.execute(
        "SELECT machine_id, name, platform, is_hub, data_root "
        "FROM machines WHERE machine_id = ?",
        (machine_id,),
    ).fetchone()
    if row is None:
        return None
    return MachineIdentity(
        machine_id=str(row[0]),
        name=str(row[1]),
        platform=str(row[2]),
        is_hub=bool(row[3]),
        data_root=str(row[4]),
    )


__all__ = [
    "IS_HUB_ENV",
    "MACHINE_ID_FILENAME",
    "MachineIdentity",
    "MachineIdentityError",
    "default_machine_name",
    "detect_platform",
    "get_or_create_machine_id",
    "is_hub_from_env",
    "load_machine",
    "machine_id_path",
    "register_machine",
]
