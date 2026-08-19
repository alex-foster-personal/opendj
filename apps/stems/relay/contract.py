"""THE app <-> relay contract for stems separation. One file, both sides.

WHY A RELAY AT ALL (the maintainer, Wed 19 Aug 2026, binding). Separation runs on
Modal, Modal is billed per GPU-second against the maintainer's account, and the app
ships to testers as a .dmg. A Modal token inside that .dmg is a token every
tester has, forever, with no cap and no revocation short of rotating it for
everyone. So NO MODAL CREDENTIAL EVER REACHES THE CLIENT. The app proves who
its user is with a Google identity token; a small server-side relay holds the
Modal secret, checks the tester against an allowlist, meters them, and calls
Modal on their behalf.

THE CLIENT NEVER SEES A MODAL TOKEN, A MODAL URL, OR A MODAL ERROR. If the
relay is unreachable the app says the relay is unreachable, which is true and
actionable; leaking Modal's own vocabulary into the client would teach a
tester to debug infrastructure they have no access to.

Both halves import THIS module: ``relay/client.py`` (in the worker) and
``relay/app.py`` (deployed). A path or header spelled twice is a path that
drifts, and the failure mode of drift here is a 404 that looks like an outage.

SHAPE. One separation per track, three calls:

    POST   /v1/stems/separations              start one, body = source audio
    GET    /v1/stems/separations/{id}         poll status, then read the parts
    GET    /v1/stems/separations/{id}/parts/{part}   one FLAC stem
    GET    /v1/stems/quota                    what this tester has left

Per TRACK rather than per batch on purpose: the app already streams one
bundle at a time so each finished track can light up in the library
immediately, and a batch endpoint would replace that with one long silence.
It also means a failure costs one track, and the rate cap counts the unit a
human recognises.

REFUSALS ARE STATUS CODES, and each means exactly one thing:

    401  the identity token is missing, malformed, expired, or not ours
    403  a valid Google user who is not on the tester allowlist
    404  no such separation for THIS user (never "exists but not yours")
    409  the separation is not in a state that permits the read
    429  the per-user rate cap is spent; body carries used/limit/resets_at
    503  the relay cannot reach Modal right now

-Claude
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ----- CFG -------------------------------------------------------------------
API_VERSION: str = "v1"
BASE_PATH: str = f"/{API_VERSION}/stems"

PATH_SEPARATIONS: str = f"{BASE_PATH}/separations"
PATH_SEPARATION: str = f"{BASE_PATH}/separations/{{separation_id}}"
PATH_SEPARATION_PART: str = f"{BASE_PATH}/separations/{{separation_id}}/parts/{{part}}"
PATH_QUOTA: str = f"{BASE_PATH}/quota"
PATH_HEALTH: str = f"/{API_VERSION}/health"

# The client sends the source audio as a raw body rather than multipart: it is
# one file, it is the whole request, and multipart would buy a boundary parser
# for nothing.
CONTENT_TYPE_AUDIO: str = "application/octet-stream"

HEADER_TIER: str = "X-Stems-Tier"
HEADER_STABLE_ID: str = "X-Stems-Stable-Id"
HEADER_SOURCE_SHA256: str = "X-Stems-Source-Sha256"
HEADER_SOURCE_NAME: str = "X-Stems-Source-Name"
"""Original filename, for the relay's logs only. NEVER a path: the relay has
no business knowing the shape of a tester's disk, and a full path in a server
log is the kind of incidental personal data that is easier to never collect
than to defend."""

# The env var the client reads. Set at build time; there is no default, and
# that is deliberate -- a hardcoded fallback URL is how a debug relay ends up
# serving production testers.
RELAY_BASE_ENV: str = "MDT_STEMS_RELAY_URL"
IDENTITY_TOKEN_ENV: str = "MDT_STEMS_IDENTITY_TOKEN"
"""Where the worker picks up the caller's Google identity token.

An env var because the worker is a subprocess spawned by the engine, and the
engine is the process that holds the signed-in session. The token is short
lived by construction (Google ID tokens last an hour), so this is a
short-lived value in a child process, not a credential at rest.
"""

STEM_PARTS: tuple[str, str, str, str] = ("vocals", "drums", "bass", "other")
SeparationStatus = Literal["queued", "running", "succeeded", "failed"]


# ----- wire models -----------------------------------------------------------
class AudioAlignment(BaseModel):
    """Shared across all four parts. Mirrors the bundle manifest's audio block."""

    model_config = ConfigDict(extra="forbid")

    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    bit_depth: int = Field(gt=0)
    codec: str


class SeparationCreated(BaseModel):
    """202 from POST /v1/stems/separations."""

    model_config = ConfigDict(extra="forbid")

    separation_id: str
    stable_id: str
    status: SeparationStatus


class SeparationState(BaseModel):
    """200 from GET /v1/stems/separations/{id}.

    ``parts`` is populated only once ``status`` is ``succeeded``; before that
    there is nothing to download and saying so with an empty mapping is
    clearer than a set of URLs that 409.
    """

    model_config = ConfigDict(extra="forbid")

    separation_id: str
    stable_id: str
    status: SeparationStatus
    progress: float = Field(ge=0.0, le=1.0)
    error: str | None = None
    parts: dict[str, int] = Field(
        default_factory=dict, description="part -> byte length"
    )
    audio: AudioAlignment | None = None
    model: dict[str, str] | None = None
    preset: dict[str, Any] | None = None
    source_sha256: str | None = None


class QuotaState(BaseModel):
    """200 from GET /v1/stems/quota.

    The app shows this before asking a tester to start a run, so "you have 40
    tracks left this week" is a fact the UI can state rather than a limit they
    discover by hitting it.
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    used: int
    limit: int
    resets_at: str
    tiers_allowed: list[str]


class RelayError(BaseModel):
    """Every non-2xx body. ``code`` is for machines, ``message`` for humans."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retry_after_s: float | None = None


# Machine-readable codes. The client branches on these, never on the prose.
CODE_NO_TOKEN: str = "identity_token_missing"
CODE_BAD_TOKEN: str = "identity_token_invalid"
CODE_NOT_ALLOWED: str = "tester_not_allowlisted"
CODE_QUOTA_SPENT: str = "rate_cap_reached"
CODE_UNKNOWN_SEPARATION: str = "separation_not_found"
CODE_NOT_READY: str = "separation_not_ready"
CODE_UPSTREAM: str = "separation_backend_unavailable"
CODE_BAD_REQUEST: str = "request_invalid"


__all__ = [
    "API_VERSION",
    "BASE_PATH",
    "CODE_BAD_REQUEST",
    "CODE_BAD_TOKEN",
    "CODE_NOT_ALLOWED",
    "CODE_NOT_READY",
    "CODE_NO_TOKEN",
    "CODE_QUOTA_SPENT",
    "CODE_UNKNOWN_SEPARATION",
    "CODE_UPSTREAM",
    "CONTENT_TYPE_AUDIO",
    "HEADER_SOURCE_NAME",
    "HEADER_SOURCE_SHA256",
    "HEADER_STABLE_ID",
    "HEADER_TIER",
    "IDENTITY_TOKEN_ENV",
    "PATH_HEALTH",
    "PATH_QUOTA",
    "PATH_SEPARATION",
    "PATH_SEPARATIONS",
    "PATH_SEPARATION_PART",
    "RELAY_BASE_ENV",
    "STEM_PARTS",
    "AudioAlignment",
    "QuotaState",
    "RelayError",
    "SeparationCreated",
    "SeparationState",
    "SeparationStatus",
]
