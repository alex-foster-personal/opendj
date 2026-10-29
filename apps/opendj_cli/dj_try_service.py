"""The transition try-er over a running engine (TRYER-10..12).

The client half of ``routes/dj_try.py``: ``opendj dj try`` and the MCP
``try_transitions`` / ``pick_transition`` / ``undo_transition_pick`` tools all
call :class:`DjTryService`, so a person, a shell agent and an MCP agent get the
same documents the try-er panel renders.

A pick or undo is a guarded write: the caller passes the etag of the job
document it reviewed (every try/pick/undo answer carries one), sent as
``If-Match``, so acting on a job someone else changed since is refused (412),
never silently applied to state the caller did not see.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from apps.opendj_cli.dj_service import DjError
from apps.opendj_cli.origin import EngineOrigin, unreachable

#: A try renders and scores every candidate offline before it answers: at the
#: contract's measured ~2 s per candidate, 6 candidates fit well inside this.
TRY_TIMEOUT_S = 300.0
#: Reads and picks touch only state.db.
PICK_TIMEOUT_S = 10.0
TRY_PATH = "/api/v1/dj/try"


@dataclass
class DjTryService:
    origin: EngineOrigin
    #: Tests hand an ``httpx.MockTransport`` or an ASGI transport here.
    transport: httpx.BaseTransport | None = None

    def _send(
        self,
        method: str,
        path: str,
        *,
        timeout_s: float,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.origin.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout_s, trust_env=False, transport=self.transport) as client:
                response = client.request(method, url, json=None if body is None else dict(body), headers=headers)
        except httpx.TransportError as error:
            raise unreachable(self.origin, error) from error
        if response.is_success:
            document: dict[str, Any] = response.json()
            return document
        try:
            detail = response.json()
        except ValueError:
            detail = {}
        code = detail.get("error") if isinstance(detail, dict) else None
        raise DjError(
            code if isinstance(code, str) else "upstream_error",
            f"{method} {path} -> {response.status_code}: {response.text[:300]}",
            path=path,
            status_code=response.status_code,
        )

    def _job_path(self, job_id: str) -> str:
        return f"{TRY_PATH}/{quote(job_id, safe='')}"

    # ----- tools ---------------------------------------------------------
    def try_transitions(self, out_id: str, in_id: str, n: int, context_bars: int) -> dict[str, Any]:
        body = {"out": out_id, "in": in_id, "n": n, "context_bars": context_bars}
        return self._send("POST", TRY_PATH, timeout_s=TRY_TIMEOUT_S, body=body)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._send("GET", self._job_path(job_id), timeout_s=PICK_TIMEOUT_S)

    def pick(self, job_id: str, candidate_id: str, etag: str) -> dict[str, Any]:
        """Pick against the etag of the job the caller actually reviewed: a job changed since is refused (412)."""
        return self._send(
            "POST",
            f"{self._job_path(job_id)}/pick",
            timeout_s=PICK_TIMEOUT_S,
            body={"candidate": candidate_id},
            headers={"If-Match": etag},
        )

    def undo(self, job_id: str, etag: str) -> dict[str, Any]:
        """Undo against the reviewed etag, so a pick the caller never saw is not deleted."""
        return self._send(
            "DELETE", f"{self._job_path(job_id)}/pick", timeout_s=PICK_TIMEOUT_S, headers={"If-Match": etag}
        )
