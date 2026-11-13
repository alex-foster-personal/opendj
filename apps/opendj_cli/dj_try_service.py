"""The transition try-er over a running engine (TRYER-10..12).

The client half of ``routes/dj_try.py``: ``opendj dj try`` and the MCP
``try_transitions`` / ``pick_transition`` / ``undo_transition_pick`` /
``get_transition_lanes`` tools all
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
#: Lanes read both track profiles and the kept deck renders (tens of MB) besides state.db.
LANES_TIMEOUT_S = 60.0
#: A candidate's audio is one kept mix render, streamed from the server's data dir.
AUDIO_TIMEOUT_S = 60.0
#: The media type the audio route serves; anything else is not the render the panel auditions.
WAV_MEDIA_TYPE = "audio/wav"
TRY_PATH = "/api/v1/dj/try"


def _malformed_success(method: str, path: str, response: httpx.Response, what: str) -> DjError:
    """The typed error for a 2xx answer the try-er contract cannot read (every try route answers one JSON object)."""
    return DjError(
        "upstream_error",
        f"{method} {path} -> {response.status_code} with {what}: {response.text[:300]}",
        path=path,
        status_code=response.status_code,
    )


@dataclass
class DjTryService:
    origin: EngineOrigin
    #: Tests hand an ``httpx.MockTransport`` or an ASGI transport here.
    transport: httpx.BaseTransport | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout_s: float,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self.origin.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout_s, trust_env=False, transport=self.transport) as client:
                response = client.request(method, url, json=None if body is None else dict(body), headers=headers)
        except httpx.TransportError as error:
            raise unreachable(self.origin, error) from error
        if response.is_success:
            return response
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

    def _send(
        self,
        method: str,
        path: str,
        *,
        timeout_s: float,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send one request and return its JSON object; a success whose body is not a JSON object is ``upstream_error``."""
        response = self._request(method, path, timeout_s=timeout_s, body=body, headers=headers)
        try:
            document = response.json()
        except ValueError as error:
            raise _malformed_success(method, path, response, "a body that is not JSON") from error
        if not isinstance(document, dict):
            raise _malformed_success(method, path, response, f"a JSON {type(document).__name__}, not an object")
        return document

    def _job_path(self, job_id: str) -> str:
        return f"{TRY_PATH}/{quote(job_id, safe='')}"

    # ----- tools ---------------------------------------------------------
    def try_transitions(self, out_id: str, in_id: str, n: int, context_bars: int) -> dict[str, Any]:
        body = {"out": out_id, "in": in_id, "n": n, "context_bars": context_bars}
        return self._send("POST", TRY_PATH, timeout_s=TRY_TIMEOUT_S, body=body)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._send("GET", self._job_path(job_id), timeout_s=PICK_TIMEOUT_S)

    def lanes(self, job_id: str, candidate_id: str, columns: int) -> dict[str, Any]:
        """One kept candidate's lane document (deck waveforms and knob lines), as the try-er panel draws it."""
        path = f"{self._job_path(job_id)}/candidates/{quote(candidate_id, safe='')}/lanes?columns={columns}"
        return self._send("GET", path, timeout_s=LANES_TIMEOUT_S)

    def candidate_audio(self, job_id: str, candidate_id: str) -> bytes:
        """One kept candidate's WAV bytes from the route the try-er panel auditions (its ``render_url``)."""
        path = f"{self._job_path(job_id)}/candidates/{quote(candidate_id, safe='')}/audio"
        response = self._request("GET", path, timeout_s=AUDIO_TIMEOUT_S)
        media_type = response.headers.get("content-type", "").split(";")[0].strip()
        if media_type != WAV_MEDIA_TYPE:
            raise DjError(
                "upstream_error",
                f"GET {path} -> {response.status_code} served {media_type or 'no content-type'}, not {WAV_MEDIA_TYPE}",
                path=path,
                status_code=response.status_code,
            )
        return response.content

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
