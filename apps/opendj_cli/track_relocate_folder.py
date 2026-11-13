"""``opendj track relocate-folder OLD NEW [apply]``: folder relocate, preview first (LIBM-72).

``opendj track relocate-nearby <stable_id>`` is the twin of ``GET /api/v1/relocate-folder/nearby/{id}``
(LIBM-73): the folder move one located track implies for its missing siblings. It writes nothing;
apply the proposal with ``relocate-folder <from_prefix> <to_prefix> apply``.

The CLI twin of ``POST /api/v1/relocate-folder/preview`` and ``.../apply``. Without ``apply``
it prints the preview and writes nothing. With ``apply`` it previews, then applies THAT plan by
its ``plan_id``, so the engine refuses (409) if the library changed in between.
Words, not ``--options``: the top-level parser refuses options it does not know.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

PREVIEW_PATH = "/api/v1/relocate-folder/preview"
APPLY_PATH = "/api/v1/relocate-folder/apply"
NEARBY_PATH = "/api/v1/relocate-folder/nearby/{stable_id}"


class RelocateFolderError(Exception):
    """The engine refused or failed a relocate-folder call."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _post(client: httpx.Client, path: str, body: dict[str, str]) -> dict[str, Any]:
    return _checked(client.post(path, json=body), f"POST {path}")


def _checked(response: httpx.Response, call: str) -> dict[str, Any]:
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        code = detail["code"] if isinstance(detail, dict) and "code" in detail else "relocate_folder_failed"
        raise RelocateFolderError(code, f"{call} returned {response.status_code}: {response.text}")
    return response.json()


def relocate_folder(client: httpx.Client, from_prefix: str, to_prefix: str, *, apply: bool) -> dict[str, Any]:
    """Preview a folder relocate and, when ``apply`` is set, apply exactly that preview."""
    prefixes = {"from_prefix": from_prefix, "to_prefix": to_prefix}
    preview = _post(client, PREVIEW_PATH, prefixes)
    if not apply:
        return {"preview": preview, "applied": None}
    applied = _post(client, APPLY_PATH, {**prefixes, "plan_id": preview["plan_id"]})
    return {"preview": preview, "applied": applied}


def relocate_nearby(client: httpx.Client, stable_id: str) -> dict[str, Any]:
    """The nearby-missing proposal for one located track; writes nothing."""
    path = NEARBY_PATH.format(stable_id=quote(stable_id, safe=""))
    return _checked(client.get(path), f"GET {path}")
