"""Pydantic models and OpenAPI response maps for stems routes.

Split from ``stems.py`` for issue #9676.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from apps.sync_hub.entitlement_gate import GateErrorResponse
from apps.webui.server.routes.sync_hub_route_errors import HubErrorResponse

STEM_HYDRATION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    429: {
        # A model, not just a description: description-only entries regenerate
        # into api-types.ts with no content, so a typed client cannot read
        # detail.code (Codex round 8). The 429 body is {"detail": {code, message}}.
        "model": GateErrorResponse,
        "description": (
            "CLOUDSTEMS_MONTHLY_CAP_REACHED: the sync hub refused this bundle "
            "because the owner has used every CloudStems track in the current "
            "UTC month. The allowance resets at the start of the next month; "
            "retrying before then only repeats the refusal. Bundles already "
            "taken this month keep serving."
        )
    },
    502: {
        "description": (
            "Index-dependent hydration failure: STEM_BUNDLE_HYDRATION_FAILED when "
            "the bundle is indexed but cannot be fetched, STEM_INDEX_CORRUPT when "
            "the local index cache is unreadable, STEM_HYDRATION_NOT_ARMED when "
            "this engine is configured for hydration but could not arm it at boot "
            "(for example boto3 is absent or the hub was unreachable). "
            "Transient boot failures such as HTTP 403/5xx may self-recover on the "
            "next throttled stems miss; structural failures such as missing boto3, "
            "unusable sync credential, or HTTP 401 STEM_HUB_AUTH_REFUSED stay "
            "terminal until operator action. STEM_HUB_AUTH_REFUSED when the hub "
            "rejects the sync credential, or STEM_HUB_INDEX_FAILED when the hub "
            "index or presign path fails with a non-unreachable error"
        )
    },
    503: {
        # Every 503 here is {"detail": {code, message[, endpoint]}}; a model lets
        # a typed client read detail.code (Codex round 9, Sol on 91035ad). The
        # unreachable-hub body and the relayed cap refusal are different models,
        # so the contract names both (Sol on 27f9acf396, P1).
        "model": HubErrorResponse | GateErrorResponse,
        "description": (
            "Configured sync hub unreachable (SYNC_HUB_UNREACHABLE with endpoint "
            "and underlying error), or the hub refused with "
            "CLOUDSTEMS_MONTHLY_CAP_UNSET because it meters CloudStems but was "
            "never given MDT_CLOUDSTEMS_MONTHLY_CAP"
        )
    },
}

STEM_PART_RESPONSES: dict[int | str, dict[str, Any]] = {
    **STEM_HYDRATION_ERROR_RESPONSES,
    503: {
        # MERGED, not replaced: the base mapping already declares the
        # unreachable-hub and CLOUDSTEMS_MONTHLY_CAP_UNSET refusals, and
        # overwriting the entry silently dropped the operator-fixable cap
        # refusal from the committed contract (Codex round 5).
        "model": HubErrorResponse | GateErrorResponse,
        "description": (
            f"{STEM_HYDRATION_ERROR_RESPONSES[503]['description']}; or "
            "STEM_BUNDLE_HYDRATING: a fresh R2 hydration did not finish within "
            "STEM_PART_HYDRATE_WAIT_S; retry the request"
        )
    },
}


class StemPartOut(BaseModel):
    """A validated standard part, available from the paired file endpoint."""

    model_config = ConfigDict(frozen=True)

    media_type: str


class StemManifestOut(BaseModel):
    """Frontend manifest projected from a stricter durable artifact manifest."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_version: int = Field(default=1, alias="schema")
    stable_id: str
    source: str = "demucs"
    model: str
    layout: str = "demucs4"
    sample_rate_hz: int
    frame_count: int
    channel_count: int
    parts: dict[str, StemPartOut]


class StemHydrationProgressOut(BaseModel):
    """How far an in-flight cloud fetch has got, read from its temp directory."""

    model_config = ConfigDict(frozen=True)

    files_total: int
    files_done: int
    bytes_done: int


StemTrackState = Literal["local", "cloud", "fetching", "error", "none"]


class StemStateOut(BaseModel):
    """One track's stem bundle, named: where it is and what is happening to it.

    ``local`` is on this machine and playable; ``cloud`` is only in the R2
    index and a hydrate will fetch it; ``fetching`` is being downloaded now;
    ``error`` is a bundle that should exist and could not be produced;
    ``none`` is no bundle anywhere this engine can see.
    """

    model_config = ConfigDict(frozen=True)

    stable_id: str
    state: StemTrackState
    message: str
    hydration_armed: bool
    progress: StemHydrationProgressOut | None = None
    error_code: str | None = None
    deck_open: bool = False


class StemUnavailableOut(BaseModel):
    """HTTP 200 empty-state: no stored bundle for this stable_id."""

    model_config = ConfigDict(frozen=True)

    status: Literal["unavailable"] = "unavailable"
    code: Literal["STEM_BUNDLE_NOT_FOUND", "STEM_BUNDLE_HYDRATING"] = "STEM_BUNDLE_NOT_FOUND"
    stable_id: str
    message: str
    #: True while a background R2 hydration for this bundle is in flight.
    #: Distinct from a genuinely absent bundle (``hydrating=False``): a
    #: client that cares can poll again shortly rather than treating this
    #: the same as "no stems exist for this track".
    hydrating: bool = False
    #: Set only while ``hydrating`` is true: files and bytes fetched so far.
    progress: StemHydrationProgressOut | None = None
