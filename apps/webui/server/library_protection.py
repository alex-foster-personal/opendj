"""Library Protection (LIBM-57): scoped write locks, "cannot delete" apart from "cannot edit".

Serato's single all-or-nothing guard covers renames too, so users switch it off to rename a
crate and forget to switch it back on. Two independent locks avoid that:

  * ``no_delete``: playlist delete answers 423 Locked with scope ``delete``.
  * ``no_edit``: playlist rename, membership add/remove, move, save-order, replace and
    transfer answer 423 Locked with scope ``edit``.

Create, duplicate, import and undelete stay open: they add, they never destroy or rewrite.

The setting is ``library-protection.json`` beside the state DB (``(private path)/``, next to
``ui-prefs.json``). The guard re-reads it on every guarded request, so a change made by
``PUT /api/v1/library/protection`` or ``opendj library protection`` applies at once.
A missing file means both locks are off; an unreadable one fails the request (no silent
unlock).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal, assert_never

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from apps.shared.atomic_text import write_atomic

from .deps import get_library_state_db

LockScope = Literal["delete", "edit"]
PROTECTION_FILENAME = "library-protection.json"
_WRITE_LOCK = threading.Lock()


class LibraryProtection(BaseModel):
    """The two scoped locks; both off by default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    no_delete: StrictBool = False
    no_edit: StrictBool = False

    def locks(self, scope: LockScope) -> bool:
        """True when ``scope`` writes are refused."""
        match scope:
            case "delete":
                return self.no_delete
            case "edit":
                return self.no_edit
            case _:
                assert_never(scope)


class LibraryProtectionPatch(BaseModel):
    """A PUT body: the named locks change, the rest keep their stored value."""

    model_config = ConfigDict(extra="forbid")

    no_delete: StrictBool | None = None
    no_edit: StrictBool | None = None


class ProtectionFileInvalid(ValueError):
    """The stored setting exists but cannot be read as a ``LibraryProtection``."""


def protection_path(state_db_path: Path) -> Path:
    """Where the setting lives for the library whose state DB is ``state_db_path``."""
    return state_db_path.parent / PROTECTION_FILENAME


def load_protection(path: Path) -> LibraryProtection:
    """The stored setting, both locks off when no file exists yet."""
    if not path.is_file():
        return LibraryProtection()
    try:
        return LibraryProtection.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ProtectionFileInvalid(f"{path} is not a valid library protection setting: {exc}") from exc


def update_protection(path: Path, patch: LibraryProtectionPatch) -> LibraryProtection:
    """Merge ``patch`` onto the stored setting, write it atomically and return the result."""
    with _WRITE_LOCK:
        merged = load_protection(path).model_copy(update=patch.model_dump(exclude_none=True))
        write_atomic(path, json.dumps(merged.model_dump(), indent=2, sort_keys=True) + "\n")
        return merged


def invalid_setting_error(exc: ProtectionFileInvalid) -> HTTPException:
    """500 for a corrupt setting file, so a guard never guesses the lock state."""
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "library_protection_invalid", "message": str(exc)},
    )


def require_unlocked(scope: LockScope) -> Callable[[Request], None]:
    """FastAPI dependency that answers 423 Locked while ``scope`` is protected."""

    def _guard(request: Request) -> None:
        try:
            protection = load_protection(protection_path(get_library_state_db(request)))
        except ProtectionFileInvalid as exc:
            raise invalid_setting_error(exc) from exc
        if protection.locks(scope):
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail={
                    "error": "library_locked",
                    "scope": scope,
                    "message": f"Library Protection is on for {scope}: turn off no_{scope} to allow this write",
                },
            )

    return _guard


__all__ = [
    "PROTECTION_FILENAME",
    "LibraryProtection",
    "LibraryProtectionPatch",
    "LockScope",
    "ProtectionFileInvalid",
    "invalid_setting_error",
    "load_protection",
    "protection_path",
    "require_unlocked",
    "update_protection",
]
