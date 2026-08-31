"""The app's half of the relay contract: separate a track without a GPU secret.

THIS is what ships. It knows a relay URL and the tester's Google identity
token, and nothing whatsoever about Modal -- not a token, not an app name, not
an error string. Swapping the credential edge for a relay is the entire change
the maintainer's decision required; the job kind, the progress protocol, the bundle
writer and the UI are untouched by it.

Returns the SAME result shape the direct Modal path returns, so
``stems_modal_worker.run`` cannot tell which transport produced a bundle and
the bundle contract stays single-sourced.

Polling rather than a socket: a separation takes tens of seconds, there is one
in flight per track, and a poll loop has no reconnect semantics to get wrong.
The interval backs off so a long track does not cost hundreds of requests.

-Claude
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from apps.stems.relay import contract as api

# ----- CFG -------------------------------------------------------------------
POLL_START_S: float = 1.0
POLL_MAX_S: float = 5.0
POLL_BACKOFF: float = 1.4
# A track that has not finished in this long is not going to; the relay's own
# Modal timeout is 900s, so this is that plus room for transfer either side.
SEPARATION_DEADLINE_S: float = 1200.0
UPLOAD_TIMEOUT_S: float = 300.0
DOWNLOAD_TIMEOUT_S: float = 300.0


class RelayUnavailable(RuntimeError):
    """The relay could not be reached or is not configured. Not the track's fault."""


class RelayRefused(RuntimeError):
    """The relay answered, and the answer was no. Carries the machine code."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def relay_base_url() -> str:
    base = os.environ.get(api.RELAY_BASE_ENV, "").strip().rstrip("/")
    if not base:
        raise RelayUnavailable(
            f"{api.RELAY_BASE_ENV} is not set, so there is no stems relay to "
            "call. This build separates stems through a relay that holds the "
            "GPU credential server-side; it has no credential of its own and "
            "will not invent one."
        )
    return base


def identity_token() -> str:
    token = os.environ.get(api.IDENTITY_TOKEN_ENV, "").strip()
    if not token:
        raise RelayUnavailable(
            f"{api.IDENTITY_TOKEN_ENV} is not set: separating stems needs you "
            "to be signed in with Google, because the GPUs are metered per "
            "tester."
        )
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _raise_for_refusal(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        detail = response.json().get("detail", {})
        code = detail.get("code", "unknown")
        message = detail.get("message", response.text[:200])
    except (ValueError, AttributeError):
        # A proxy's HTML error page is still a refusal, and reporting it as
        # one beats reporting a JSON decode error the tester cannot act on.
        code, message = "unknown", response.text[:200]
    raise RelayRefused(code, message, response.status_code)


def fetch_quota(client: httpx.Client, base: str, token: str) -> api.QuotaState:
    """What this tester has left. The UI states it before asking to spend."""
    response = client.get(f"{base}{api.PATH_QUOTA}", headers=_headers(token))
    _raise_for_refusal(response)
    return api.QuotaState.model_validate(response.json())


def separate_one(
    client: httpx.Client,
    base: str,
    token: str,
    *,
    audio_bytes: bytes,
    stable_id: str,
    source_name: str,
    tier_key: str,
    deadline_s: float = SEPARATION_DEADLINE_S,
) -> dict[str, Any]:
    """One track, start to bundle-ready result dict."""
    started = time.monotonic()
    response = client.post(
        f"{base}{api.PATH_SEPARATIONS}",
        content=audio_bytes,
        timeout=UPLOAD_TIMEOUT_S,
        headers={
            **_headers(token),
            "Content-Type": api.CONTENT_TYPE_AUDIO,
            api.HEADER_TIER: tier_key,
            api.HEADER_STABLE_ID: stable_id,
            api.HEADER_SOURCE_NAME: source_name,
        },
    )
    _raise_for_refusal(response)
    created = api.SeparationCreated.model_validate(response.json())

    state = _poll_until_terminal(
        client, base, token, created.separation_id, started, deadline_s
    )
    if state.status != "succeeded":
        raise RuntimeError(state.error or "the relay reported a failed separation")
    if state.audio is None:
        raise RuntimeError("the relay reported success with no audio alignment")

    stems: dict[str, bytes] = {}
    for part in api.STEM_PARTS:
        if part not in state.parts:
            raise RuntimeError(f"the relay reported no {part!r} part")
        blob = client.get(
            f"{base}"
            + api.PATH_SEPARATION_PART.format(
                separation_id=created.separation_id, part=part
            ),
            headers=_headers(token),
            timeout=DOWNLOAD_TIMEOUT_S,
        )
        _raise_for_refusal(blob)
        stems[part] = blob.content

    return {
        "stable_id": stable_id,
        "stems": stems,
        "stem_ext": "flac",
        "source_sha256": state.source_sha256 or "0" * 64,
        "audio": state.audio.model_dump(),
        "relay_model": state.model,
        "relay_preset": state.preset,
    }


def _poll_until_terminal(
    client: httpx.Client,
    base: str,
    token: str,
    separation_id: str,
    started: float,
    deadline_s: float,
) -> api.SeparationState:
    interval = POLL_START_S
    while True:
        response = client.get(
            f"{base}" + api.PATH_SEPARATION.format(separation_id=separation_id),
            headers=_headers(token),
        )
        _raise_for_refusal(response)
        state = api.SeparationState.model_validate(response.json())
        if state.status in {"succeeded", "failed"}:
            return state
        if time.monotonic() - started > deadline_s:
            raise RuntimeError(
                f"separation {separation_id} was still {state.status} after "
                f"{deadline_s:.0f}s"
            )
        time.sleep(interval)
        interval = min(POLL_MAX_S, interval * POLL_BACKOFF)


def relay_separator(
    tracks: Sequence[Any], tier_key: str
) -> Iterator[dict[str, Any]]:
    """The worker's separator, over the relay. Same shape as the direct one.

    Serial, one track at a time, because the relay meters per track and the
    UI wants each bundle the moment it exists. Fan-out lives on the relay's
    side of the wire, where the GPU budget is.
    """
    base = relay_base_url()
    token = identity_token()
    with httpx.Client(timeout=60.0) as client:
        for track in tracks:
            try:
                yield separate_one(
                    client,
                    base,
                    token,
                    audio_bytes=track.audio_path.read_bytes(),
                    stable_id=track.stable_id,
                    source_name=track.audio_path.name,
                    tier_key=tier_key,
                )
            except RelayRefused as exc:
                if exc.code in {
                    api.CODE_QUOTA_SPENT,
                    api.CODE_NOT_ALLOWED,
                    api.CODE_NO_TOKEN,
                    api.CODE_BAD_TOKEN,
                }:
                    # These are about the ACCOUNT, not the track. Retrying the
                    # rest of the batch would produce one identical failure per
                    # track and bury the one sentence that matters.
                    raise
                yield {"stable_id": track.stable_id, "error": str(exc)}
            except (httpx.HTTPError, RuntimeError) as exc:
                yield {
                    "stable_id": track.stable_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }


__all__ = [
    "POLL_MAX_S",
    "POLL_START_S",
    "SEPARATION_DEADLINE_S",
    "RelayRefused",
    "RelayUnavailable",
    "fetch_quota",
    "identity_token",
    "relay_base_url",
    "relay_separator",
    "separate_one",
]
