"""HTTP surface of Library Protection (LIBM-57, ``..library_protection``).

  GET /library/protection   the two scoped locks: {no_delete, no_edit}
  PUT /library/protection   change the named locks, e.g. {"no_delete": true}; answers the result

CLI twin: ``opendj library protection [--no-delete on|off] [--no-edit on|off]``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..deps import get_library_state_db
from ..library_protection import (
    LibraryProtection,
    LibraryProtectionPatch,
    ProtectionFileInvalid,
    invalid_setting_error,
    load_protection,
    protection_path,
    update_protection,
)

router = APIRouter(prefix="/library/protection", tags=["library-protection"])
MOUNT_UNDER_API_PREFIX = True


@router.get("", response_model=LibraryProtection, operation_id="get_library_protection")
def get_library_protection(request: Request) -> LibraryProtection:
    """The current Library Protection locks."""
    try:
        return load_protection(protection_path(get_library_state_db(request)))
    except ProtectionFileInvalid as exc:
        raise invalid_setting_error(exc) from exc


@router.put("", response_model=LibraryProtection, operation_id="put_library_protection")
def put_library_protection(body: LibraryProtectionPatch, request: Request) -> LibraryProtection:
    """Set the named locks and answer the stored result."""
    try:
        return update_protection(protection_path(get_library_state_db(request)), body)
    except ProtectionFileInvalid as exc:
        raise invalid_setting_error(exc) from exc
