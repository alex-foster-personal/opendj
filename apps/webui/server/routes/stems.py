"""Read-only endpoints for validated precomputed Demucs stem bundles.

The application integrator mounts :data:`router` at ``/api/v1``.  The router
does not run Demucs or mutate files: every request reloads and validates the
stored artifact before it exposes either the manifest or a container-typed audio response.
The manifest GET answers HTTP 200 unavailable when no bundle exists; the
part GET still 404s when there is no file to stream.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BufferedReader
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import stem_cache_budget, stem_hydration, stem_index
from apps.cloud.hub_stem_client import (
    STEM_BUNDLE_PRESIGN_PATH,
    STEM_BUNDLE_SIZE_PATH,
    STEM_INDEX_PATH,
)
from apps.cloud.stem_source import (
    STEM_BUNDLE_NOT_INDEXED,
    STEM_HUB_INDEX_FAILED,
    STEM_HUB_UNREACHABLE,
    STEM_MONTHLY_CAP_REACHED,
    STEM_MONTHLY_CAP_UNSET,
    DirectR2Source,
    StemHydrationSource,
    StemSourceError,
    hub_transport_failure_kind,
)
from apps.shared.shared_read_open import open_read_shared
from apps.stems.artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundle,
    StemBundleNotFoundError,
    load_stem_bundle,
    stem_roots,
)
from apps.stems.bundle_lock import (
    held_for_read,
    is_shared_store_bundle,
    readers_held_off,
    restore_orphaned_swap,
    swap_backup_path,
)
from apps.stems.recompress import restore_orphaned_recompress_swap, swap_journal_path
from apps.sync_hub.entitlement_gate import GateErrorResponse
from apps.webui.server.routes.sync_hub_route_errors import (
    HubErrorResponse,
    raise_sync_hub_unreachable,
)

router = APIRouter(prefix="/tracks", tags=["stems"])

log = logging.getLogger(__name__)

#: How long the PART route waits for an in-flight hydration before answering
#: "still fetching" instead of the bytes. The MANIFEST route never waits at
#: all (D2): it only enqueues and returns, so a deck load's probe is never
#: held for a 4-part R2 fetch. A part request means the caller already
#: decided it wants stem bytes for THIS track, so waiting here (bounded) is
#: the right tradeoff -- see PR body "which request waits on what".
STEM_PART_HYDRATE_WAIT_S: float = 30.0

_HYDRATE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stem-hydrate")
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, Future] = {}
#: stable_id -> number of files the index lists for the bundle being fetched,
#: recorded at enqueue so a progress read never has to re-parse the index.
_INFLIGHT_FILE_TOTALS: dict[str, int] = {}
#: stable_id -> reason, for a bundle the index says exists but whose last
#: hydration attempt failed. Consulted BEFORE re-enqueueing so a hot loop of
#: part requests does not hammer R2 with the same doomed fetch, and so a
#: request can fail loud immediately instead of waiting out the full timeout
#: again for an error already known.
@dataclass(frozen=True)
class _HydrateError:
    code: str
    message: str
    #: Epoch seconds after which this record is forgotten; None never expires.
    #: A time-bound refusal (a spent month, an unset allowance) must not be
    #: cached forever: an app left open across the UTC reset would keep
    #: answering 429 for a track the hub serves again (Codex round 4).
    expires_at: float | None = None


_LAST_HYDRATE_ERROR: dict[str, _HydrateError] = {}

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


def _stems_dir(request: Request) -> Path:
    """Use an injected directory in isolated apps, else the canonical state dir."""
    configured = getattr(request.app.state, "stems_dir", DEFAULT_STEMS_DIR)
    return Path(configured)


def _stem_roots(request: Request) -> tuple[Path, ...] | None:
    """Return an explicit mode boundary when the production app configured one."""

    configured = getattr(request.app.state, "stem_roots", None)
    if configured is None:
        return None
    return tuple(Path(root) for root in configured)


def _hold_for_deck(stable_id: str, request: Request, *, deck_open: bool = False) -> Callable[[], None]:
    """Protect ``stable_id`` from eviction BEFORE this route reads its bundle.

    Marks the deck registry, then waits out any eviction claim already
    under way (:func:`stem_cache_budget.hold_against_eviction`; never a
    hydration's R2 fetch),
    so a claim can never remove a bundle this request goes on to read
    (Codex P1 on #6820). Only an app built on a data dir runs eviction
    (``stem_cache_data_dir``); a bare router app has none to wait for.

    Returns the undo for a read that then fails: a request that served no
    bytes must not keep a corrupt bundle from eviction for the served TTL
    (Codex on #6820). A deck-open is the caller's own explicit hold and has
    nothing to undo.
    """
    registry = stem_hydration.OPEN_DECKS
    withdraw: list[Callable[[], None]] = []

    def mark() -> None:
        if deck_open:
            registry.mark_open(stable_id)
        else:
            withdraw.append(registry.mark_served_for_read(stable_id))

    data_dir = getattr(request.app.state, "stem_cache_data_dir", None)
    if data_dir is None:
        mark()
    else:
        stem_cache_budget.hold_against_eviction(mark, Path(data_dir), stable_id)
    return withdraw[0] if withdraw else (lambda: None)


def _read_barrier(stable_id: str, request: Request) -> AbstractContextManager[None]:
    """Hold off swaps and eviction claims while this request resolves and opens its bundle.

    ``_hold_for_deck`` only waits out a claim already under way, then lets
    go; a swap that starts after it and before the route opens its files
    would empty the bundle path under the read (Codex on #6820). See
    :func:`stem_cache_budget.read_barrier`. Never held across an R2 wait.
    """
    data_dir = getattr(request.app.state, "stem_cache_data_dir", None)
    if data_dir is None:
        return stem_cache_budget.read_barrier(None, stable_id)
    return _restoring_read_barrier(Path(data_dir), stable_id, request)


@contextmanager
def _restoring_read_barrier(data_dir: Path, stable_id: str, request: Request) -> Iterator[None]:
    """The read barrier, after putting back a bundle an interrupted swap left aside.

    A publisher killed between a swap's two renames leaves the only good
    copy at its swap backup, and the track read as missing until another
    publish (Codex on #6820). The root stays lexical: a stem root symlinked
    to another volume resolves outside ``<data>/state``, where the swap's
    backup is not looked for (Codex on #6820). The backup helper resolves
    only where it must.
    """
    roots = _stem_roots(request)
    for root in roots if roots is not None else stem_roots(_stems_dir(request)):
        _restore_interrupted_swap(Path(root) / stable_id, data_dir)
    with stem_cache_budget.read_barrier(data_dir, stable_id):
        yield


def _restore_interrupted_swap(dest: Path, data_dir: Path) -> None:
    """Put back ``dest`` from a dead swap's backup, as a writer: bundle lock, then the barrier.

    The restore is a rename into the live path, so it takes the bundle lock
    every writer holds, then the claim lock, in the order a swap takes
    them. Under the barrier alone it could land while a publisher holding
    the bundle lock had already seen no bundle there, and that publish then
    replaced what was restored without carrying its files over (Sol on
    #6820). With both held no live swap is mid-rename, so a backup with no
    bundle in place is a dead writer's.
    """
    if dest.exists():
        return  # the common case: one stat, no lock
    if not is_shared_store_bundle(dest):
        return  # outside <data>/state no swap holds the store's locks, and none leaves a backup to restore
    if not swap_backup_path(dest).is_dir() and not swap_journal_path(dest).is_file():
        # Nothing a dead swap left. A missing bundle is usually one a
        # hydration is fetching under the bundle lock, and the manifest
        # route must answer that poll at once (Codex P1 on #6821).
        return
    with held_for_read(data_dir, dest.name), readers_held_off(dest):
        restore_orphaned_swap(dest, data_dir)
        # A recompression keeps its backup under its own journal, not the
        # swap backup above (Codex on #6820).
        restore_orphaned_recompress_swap(dest, data_dir)


def _load_or_http_error(stable_id: str, request: Request) -> StemBundle:
    try:
        return load_stem_bundle(
            stable_id,
            stems_dir=_stems_dir(request),
            roots=_stem_roots(request),
        )
    except StemBundleNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_BUNDLE_NOT_FOUND", "message": str(exc)},
        ) from exc
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc


def _unavailable_out(stable_id: str, exc: StemBundleNotFoundError) -> StemUnavailableOut:
    return StemUnavailableOut(stable_id=stable_id, message=str(exc))


# ---------------------------------------------------------------------------
# On-demand R2 hydration (ADR-0024). Wiring is OPTIONAL: a machine with no
# CloudConfig / S3 client bound to app.state (local mode, or no R2 creds)
# simply never enqueues -- the routes fall back to the pre-existing
# unavailable/404 behavior unchanged. See apps.webui.server.app_wiring.
# ---------------------------------------------------------------------------


def _hydration_deps(request: Request) -> tuple[StemHydrationSource, Path] | None:
    unarmed_reason = getattr(request.app.state, "stem_hydration_unarmed_reason", None)
    if unarmed_reason is not None:
        unarmed_kind = getattr(request.app.state, "stem_hydration_unarmed_kind", None)
        if unarmed_kind == "transient":
            from apps.webui.server.stem_hydration_rearm import maybe_rearm_stem_hydration

            if maybe_rearm_stem_hydration(request.app):
                unarmed_reason = None
            else:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "STEM_HYDRATION_NOT_ARMED",
                        "message": unarmed_reason,
                    },
                )
        else:
            raise HTTPException(
                status_code=502,
                detail={"code": "STEM_HYDRATION_NOT_ARMED", "message": unarmed_reason},
            )
    source = getattr(request.app.state, "stem_hydration_source", None)
    data_dir = getattr(request.app.state, "stem_hydration_data_dir", None)
    if source is None and getattr(request.app.state, "stem_hydration_cfg", None) is not None:
        cfg = request.app.state.stem_hydration_cfg
        s3 = getattr(request.app.state, "stem_hydration_s3", None)
        if s3 is not None:
            source = DirectR2Source(cfg=cfg, s3=s3)
    if source is None or data_dir is None:
        return None
    return source, Path(data_dir)


def _hub_endpoint_from_message(message: str) -> str:
    lowered = message.casefold()
    if "bundle size" in lowered:
        return STEM_BUNDLE_SIZE_PATH
    if "bundle presign" in lowered or "presign" in lowered:
        return STEM_BUNDLE_PRESIGN_PATH
    return STEM_INDEX_PATH


def _stem_source_error_from_hub_reason(reason: str) -> StemSourceError:
    for code in (STEM_HUB_UNREACHABLE, STEM_HUB_INDEX_FAILED):
        exc = StemSourceError(code, reason)
        if hub_transport_failure_kind(exc) is not None:
            return exc
    return StemSourceError(STEM_HUB_INDEX_FAILED, reason)


def _raise_hydrate_error(error: _HydrateError) -> None:
    exc = StemSourceError(error.code, error.message)
    _raise_monthly_cap_refusal(exc)
    if hub_transport_failure_kind(exc) == "unreachable":
        raise_sync_hub_unreachable(_hub_endpoint_from_message(error.message), exc)
    if hub_transport_failure_kind(exc) is not None:
        raise HTTPException(
            status_code=502,
            detail={"code": error.code, "message": error.message},
        ) from exc
    _raise_hydration_failed(error)


def _raise_monthly_cap_refusal(exc: StemSourceError) -> None:
    """Preserve the hub's own refusal for a spent (or unconfigured) month.

    A 429 from the hub is an answer, not a failure: it means this owner has
    used the month's CloudStems allowance. Without this branch the code fell
    through to ``STEM_HUB_INDEX_FAILED`` and the stems route answered 502,
    which a client reads as "the hub broke" and retries -- spending the same
    refusal again (Codex round 3, P2). 503 UNSET keeps its own code for the
    same reason: the operator can fix a missing allowance, a retry cannot.
    """
    if exc.code == STEM_MONTHLY_CAP_REACHED:
        raise HTTPException(
            status_code=429,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if exc.code == STEM_MONTHLY_CAP_UNSET:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


def _raise_source_error(exc: StemSourceError) -> None:
    _raise_monthly_cap_refusal(exc)
    if hub_transport_failure_kind(exc) == "unreachable":
        raise_sync_hub_unreachable(_hub_endpoint_from_message(exc.message), exc)
    raise HTTPException(
        status_code=502,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


def _run_hydration(
    stable_id: str,
    *,
    source: StemHydrationSource,
    index: stem_index.StemAssetIndex,
    data_dir: Path,
    stems_dir: Path,
) -> stem_hydration.HydrationOutcome:
    try:
        outcome = stem_hydration.hydrate_one(
            stable_id,
            data_dir=data_dir,
            source=source,
            index=index,
            stems_dir=stems_dir,
            skip_reserved=False,  # on-demand deck load NEVER skips reserved ids (D5)
            hand_off_to_deck=True,  # the deck polls for this bundle next
        )
    except StemSourceError as exc:
        _remember_hydrate_error(stable_id, exc.code, exc.message)
        _log_hydration_failure(stable_id, exc.code, exc.message)
        return stem_hydration.HydrationOutcome(
            stable_id=stable_id,
            status="error",
            reason=exc.message,
        )
    except Exception as exc:
        _remember_hydrate_error(stable_id, "STEM_BUNDLE_HYDRATION_FAILED", str(exc))
        _log_hydration_failure(stable_id, "STEM_BUNDLE_HYDRATION_FAILED", str(exc))
        raise
    if outcome.status == "error":
        # The outcome carries the source's own code when it had one; without
        # it a monthly-cap refusal was re-labelled STEM_BUNDLE_HYDRATION_FAILED
        # and answered 502 instead of 429/503 (Codex round 5).
        code = outcome.code or "STEM_BUNDLE_HYDRATION_FAILED"
        reason = outcome.reason or "hydration failed"
        _remember_hydrate_error(stable_id, code, reason)
        _log_hydration_failure(stable_id, code, reason)
    elif outcome.status == "hub_error":
        reason = outcome.reason or "hub transport failure"
        hub_exc = _stem_source_error_from_hub_reason(reason)
        _remember_hydrate_error(stable_id, hub_exc.code, hub_exc.message)
        _log_hydration_failure(stable_id, hub_exc.code, reason)
    else:
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
    return outcome


def _log_hydration_failure(stable_id: str, code: str, reason: str) -> None:
    """Report an indexed bundle that could not be fetched.

    The manifest route already answers this loud (502), but an HTTP error
    never becomes a Sentry event (``failed_request_status_codes=set()``), and
    the deck that asked may be mid-set, where the client report is held back.
    ERROR reaches the error sink and Sentry through the engine warning log;
    an unreachable hub and a monthly-cap refusal stay WARNING, because a
    laptop offline at a venue and a spent allowance are expected states, not
    defects.
    """
    exc = StemSourceError(code, reason)
    # A spent or unconfigured monthly allowance is an answer from the hub,
    # not a defect: WARNING, so it never reaches Sentry (Sol, P2).
    expected = code in (STEM_MONTHLY_CAP_REACHED, STEM_MONTHLY_CAP_UNSET)
    level = (
        logging.WARNING
        if expected or hub_transport_failure_kind(exc) == "unreachable"
        else logging.ERROR
    )
    log.log(
        level,
        "stem-hydration: indexed bundle could not be fetched [%s] stable_id=%s: %s",
        code,
        stable_id,
        reason,
    )


def _enqueue_hydration(stable_id: str, request: Request) -> Future | None:
    """Start background hydration if R2 has this bundle indexed.

    Returns the in-flight future, or ``None`` when there is nothing to
    hydrate -- either hydration is not configured on this machine, or the
    bundle is not in the (local-cache copy of the) R2 index. The index
    lookup here reads ONLY the local cache file (no network), so this is
    always cheap enough to call from the manifest route without blocking it.
    """
    deps = _hydration_deps(request)
    if deps is None:
        return None
    source, data_dir = deps
    cache_path = stem_index.local_index_cache_path(data_dir)
    if not cache_path.is_file():
        try:
            source.refresh_index(data_dir)
        except StemSourceError as exc:
            _raise_source_error(exc)
        except Exception as exc:
            _raise_index_refresh_failed(str(exc))
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(stable_id)
        if existing is not None and not existing.done():
            return existing
        # Corrupt cache must fail loud on the index-dependent miss path only.
        # A bundle that already loads locally never reaches this code.
        try:
            index = stem_index.load_cached_index(data_dir)
        except stem_index.StemIndexError as exc:
            _raise_stem_index_corrupt(exc, data_dir)
        if stable_id not in index:
            if not cache_path.is_file():
                refresh_err = source.refresh_error(data_dir)
                if refresh_err is not None:
                    _raise_index_refresh_failed(refresh_err)
            _raise_bundle_not_indexed(stable_id)
        stems_dir = _stems_dir(request)
        future = _HYDRATE_EXECUTOR.submit(
            _run_hydration,
            stable_id,
            source=source,
            index=index,
            data_dir=data_dir,
            stems_dir=stems_dir,
        )
        _INFLIGHT[stable_id] = future
        _INFLIGHT_FILE_TOTALS[stable_id] = len(index[stable_id])
        return future


#: How long an UNSET-allowance refusal is remembered: long enough to stop a hot
#: loop hammering the hub, short enough that fixing the env is felt.
CAP_UNSET_RECHECK_S: float = 900.0
#: How long a cap refusal whose text names no month is trusted before asking again.
CAP_UNNAMED_RECHECK_S: float = 900.0


#: The billed month the hub names in a cap refusal: ``StemCapExceeded`` words
#: it "CloudStems monthly cap reached for YYYY-MM", and every relay of that
#: refusal (transport error, source error, outcome reason) keeps that text.
_CAP_MONTH_RE = re.compile(r"monthly cap reached for (\d{4})-(\d{2})\b")


def _cap_refusal_expiry(code: str, now: datetime, message: str = "") -> float | None:
    """When a time-bound refusal stops being true, in epoch seconds.

    ``CLOUDSTEMS_MONTHLY_CAP_REACHED`` expires at 00:00 UTC on the first of the
    month after the one the HUB refused, which is when its allowance resets.
    The month comes from the hub's own refusal text, not from when this spoke
    received it, so a refusal sent at 23:59 on the 31st and read at 00:00 on
    the 1st is already expired rather than cached for a whole month (Sol review
    of 61dd5ee997, P1). A refusal that names no valid month is not guessed
    into a month: it expires after :data:`CAP_UNNAMED_RECHECK_S`, so a
    refusal from a hub whose text changed is asked again soon rather than
    cached past a reset it cannot see (Sol review of 27f9acf396, P1).
    ``..._UNSET`` expires after :data:`CAP_UNSET_RECHECK_S`, because an
    operator can fix the env at any time. Every other code is not time-bound
    and is recorded with no expiry.
    """
    if code == STEM_MONTHLY_CAP_REACHED:
        named = _CAP_MONTH_RE.search(message)
        if named is None or not 1 <= int(named.group(2)) <= 12:
            return now.timestamp() + CAP_UNNAMED_RECHECK_S
        year, month = int(named.group(1)), int(named.group(2))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return datetime(year, month, 1, tzinfo=UTC).timestamp()
    if code == STEM_MONTHLY_CAP_UNSET:
        return now.timestamp() + CAP_UNSET_RECHECK_S
    return None


def _remember_hydrate_error(stable_id: str, code: str, message: str) -> None:
    """Record a failed hydration, with an expiry for a time-bound refusal."""
    error = _HydrateError(
        code,
        message,
        expires_at=_cap_refusal_expiry(code, datetime.now(UTC), message),
    )
    with _INFLIGHT_LOCK:
        _LAST_HYDRATE_ERROR[stable_id] = error


def _recorded_hydration_error(stable_id: str) -> _HydrateError | None:
    """The recorded failure for ``stable_id``, forgetting an expired one.

    A refusal that has expired is dropped rather than returned, so the next
    request re-enqueues a fresh hydration and asks the hub again. That is what
    makes a month's reset (or an operator fixing the cap) visible without a
    restart or an explicit hydrate POST.
    """
    with _INFLIGHT_LOCK:
        error = _LAST_HYDRATE_ERROR.get(stable_id)
        if error is None:
            return None
        if error.expires_at is not None and error.expires_at <= time.time():
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
            return None
        return error


def _raise_hydration_failed(error: _HydrateError | str) -> None:
    """Fail LOUD, never silent-empty: the index said this bundle exists, and
    hydration could not produce it. A caller must never read this the same
    as "no bundle anywhere" (HTTP 200 unavailable / 404) -- see the storage
    view's fail-loud requirement (ADR-0024)."""
    if isinstance(error, str):
        detail = {"code": "STEM_BUNDLE_HYDRATION_FAILED", "message": error}
    else:
        detail = {"code": error.code, "message": error.message}
    raise HTTPException(status_code=502, detail=detail)


def _raise_bundle_not_indexed(stable_id: str) -> None:
    """Fail loud when hydration is armed but the index has no bundle entry."""
    raise HTTPException(
        status_code=502,
        detail={
            "code": STEM_BUNDLE_NOT_INDEXED,
            "message": f"stable_id {stable_id!r} is not in the published stem index",
        },
    )


def _raise_stem_index_corrupt(exc: stem_index.StemIndexError, data_dir: Path) -> None:
    """Fail loud when the local index cache exists but cannot be read.

    Only reached on the index-dependent miss path (no local bundle yet).
    A bundle that already loads locally never consults the index cache.
    """
    raise HTTPException(
        status_code=502,
        detail={
            "code": "STEM_INDEX_CORRUPT",
            "message": str(exc),
            "path": str(stem_index.local_index_cache_path(data_dir)),
        },
    ) from exc


def _raise_index_refresh_failed(reason: str) -> None:
    """Fail loud when a cold-cache single-flight refresh from R2 failed: the
    caller cannot tell 'nothing published yet' from 'refresh error' unless
    this is surfaced explicitly, never silently folded into the ordinary
    unavailable/404 empty state."""
    raise HTTPException(
        status_code=502,
        detail={"code": "STEM_INDEX_REFRESH_FAILED", "message": reason},
    )


def _hydration_progress(stable_id: str, stems_dir: Path) -> StemHydrationProgressOut:
    """Files and bytes the in-flight fetch has written so far.

    Read from the fetch's own temp directory (``<stable_id>.tmp-hydrate-*``),
    so it costs a directory listing and never touches the network or the
    fetch itself. A file still being written counts as done with the bytes it
    has, which is why this is progress and not a completion check.
    """
    files_done = 0
    bytes_done = 0
    prefix = f"{stable_id}{stem_hydration.IN_FLIGHT_MARKER}"
    if stems_dir.is_dir():
        for tmp_dir in stems_dir.iterdir():
            if not tmp_dir.name.startswith(prefix) or not tmp_dir.is_dir():
                continue
            dir_files, dir_bytes = _written_so_far(tmp_dir)
            files_done += dir_files
            bytes_done += dir_bytes
    with _INFLIGHT_LOCK:
        files_total = _INFLIGHT_FILE_TOTALS.get(stable_id, 0)
    return StemHydrationProgressOut(
        files_total=files_total, files_done=files_done, bytes_done=bytes_done
    )


def _written_so_far(tmp_dir: Path) -> tuple[int, int]:
    """Regular files and bytes in one fetch's temp directory.

    The fetch publishes (renames) or removes the directory when it finishes,
    which can land between the caller's ``is_dir()`` and this listing, or
    between the listing and a file's ``stat()``. Either way the directory's
    files are no longer in flight: they count as nothing here, never as an
    error, so a progress read racing a finished fetch cannot fail the poll.
    """
    files_done = 0
    bytes_done = 0
    # PermissionError: Windows refuses access to a directory mid-rename or
    # mid-delete instead of reporting it gone.
    try:
        children = list(tmp_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return 0, 0
    for child in children:
        try:
            child_stat = child.stat()
        except (FileNotFoundError, PermissionError):
            continue
        if stat.S_ISREG(child_stat.st_mode):
            files_done += 1
            bytes_done += child_stat.st_size
    return files_done, bytes_done


def _hydration_in_flight(stable_id: str) -> bool:
    with _INFLIGHT_LOCK:
        future = _INFLIGHT.get(stable_id)
        return future is not None and not future.done()


def _hydration_arming(state: Any) -> tuple[str | None, Any, bool]:
    """Read, never change, whether cloud stem hydration is armed on ``app.state``.

    Returns the recorded reason it is not armed (or None), its data directory,
    and whether a fetch could run now: no unarmed reason, a source, and a data
    directory.
    """
    unarmed_reason = getattr(state, "stem_hydration_unarmed_reason", None)
    data_dir = getattr(state, "stem_hydration_data_dir", None)
    has_source = getattr(state, "stem_hydration_source", None) is not None or (
        getattr(state, "stem_hydration_cfg", None) is not None
        and getattr(state, "stem_hydration_s3", None) is not None
    )
    return unarmed_reason, data_dir, unarmed_reason is None and has_source and data_dir is not None


def _bundle_landed(stable_id: str, stems_dir: Path, request: Request) -> bool:
    """Whether a valid bundle for ``stable_id`` is on this machine now."""
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except (StemBundleNotFoundError, StemArtifactError):
        return False
    return True


def _index_proves_absent(stable_id: str, data_dir: Path | str | None) -> bool:
    """Whether a cached cloud index exists, reads cleanly and lacks ``stable_id``.

    A missing cache proves nothing (it was never fetched), and an unreadable
    one is not evidence either, so both answer ``False``.
    """
    if data_dir is None:
        return False
    if not stem_index.local_index_cache_path(Path(data_dir)).is_file():
        return False
    try:
        return stable_id not in stem_index.load_cached_index(Path(data_dir))
    except stem_index.StemIndexError:
        return False


def _local_bundle_read(stable_id: str, stems_dir: Path, request: Request) -> str | None:
    """Read the bundle on this machine: ``"local"`` when it loads, the reason
    when it is invalid and no fetch is replacing it, else ``None``.

    An invalid bundle a fetch is replacing reads as that fetch, not an error.
    """
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except StemBundleNotFoundError:
        return None
    except StemArtifactError as exc:
        return None if _hydration_in_flight(stable_id) else str(exc)
    return "local"


def _stem_state(stable_id: str, request: Request) -> StemStateOut:
    """Name the track's stem state WITHOUT starting or re-arming anything.

    Deliberately reads ``app.state`` directly instead of calling
    :func:`_hydration_deps`, which raises and may re-arm hydration: a state
    read that changes the state it reports is not a read.
    """
    stems_dir = _stems_dir(request)
    deck_open = stem_hydration.OPEN_DECKS.is_open(stable_id)
    unarmed_reason, data_dir, armed = _hydration_arming(request.app.state)

    def _out(
        name: StemTrackState,
        message: str,
        *,
        progress: StemHydrationProgressOut | None = None,
        error_code: str | None = None,
    ) -> StemStateOut:
        return StemStateOut(
            stable_id=stable_id,
            state=name,
            message=message,
            hydration_armed=armed,
            progress=progress,
            error_code=error_code,
            deck_open=deck_open,
        )

    local = _local_bundle_read(stable_id, stems_dir, request)
    if local == "local":
        return _out("local", "stem bundle is on this machine")
    if local is not None:
        return _out("error", local, error_code="STEM_ARTIFACT_INVALID")

    if _hydration_in_flight(stable_id):
        return _out(
            "fetching",
            "fetching the stem bundle from the cloud",
            progress=_hydration_progress(stable_id, stems_dir),
        )
    recorded = _recorded_hydration_error(stable_id)
    if recorded is not None:
        return _out("error", recorded.message, error_code=recorded.code)
    if unarmed_reason is not None:
        if _index_proves_absent(stable_id, data_dir):
            return _out("none", "no stem bundle on this machine or in the cloud")
        return _out("error", unarmed_reason, error_code="STEM_HYDRATION_NOT_ARMED")
    if not armed:
        return _out(
            "none",
            "no stem bundle on this machine, and cloud stems are not configured here",
        )
    name, message, code = _armed_index_state(stable_id, Path(data_dir), stems_dir, request)
    return _out(name, message, error_code=code)


def _armed_index_state(
    stable_id: str, data_dir: Path, stems_dir: Path, request: Request
) -> tuple[StemTrackState, str, str | None]:
    """Name an armed engine's state for a track that is neither local nor
    being fetched, from the cached cloud index: cloud, local, none or error."""
    if not stem_index.local_index_cache_path(data_dir).is_file():
        # load_cached_index reads a missing cache as empty; that is "not
        # fetched yet", never proof the track has no cloud bundle.
        return "error", "the cloud stem index has not been fetched yet", "STEM_INDEX_NOT_FETCHED"
    try:
        index = stem_index.load_cached_index(data_dir)
    except stem_index.StemIndexError as exc:
        return "error", str(exc), "STEM_INDEX_CORRUPT"
    if stable_id not in index:
        return "none", "no stem bundle on this machine or in the cloud", None
    # A fetch that finished between the local read and the in-flight check has
    # already landed the bundle; read again before calling it cloud-only, so a
    # just-landed retry never reads as "cloud".
    if _bundle_landed(stable_id, stems_dir, request):
        return "local", "stem bundle is on this machine", None
    return "cloud", "stem bundle is in the cloud and is fetched on demand", None


def _manifest_out(bundle: StemBundle) -> StemManifestOut:
    """Expose only the stable client contract after internal provenance checks."""
    return StemManifestOut(
        stable_id=bundle.manifest.stable_id,
        # The separator that actually made these files. Reporting "demucs" for
        # a RoFormer bundle would put a wrong model name in front of the user.
        source="demucs" if bundle.layout == "demucs4" else "roformer",
        model=bundle.manifest.model.name,
        layout=bundle.layout,
        sample_rate_hz=bundle.alignment.sample_rate,
        frame_count=bundle.alignment.frame_count,
        channel_count=bundle.alignment.channels,
        # The bundle's OWN parts, not a fixed four: a 2-part RoFormer bundle
        # must advertise exactly what it has so the client can render the
        # controls it cannot drive as inert rather than silently dead.
        parts={
            part: StemPartOut(media_type=bundle.media_type) for part in bundle.parts
        },
    )


def _open_validated_stem(path: Path) -> BufferedReader:
    """Open one validated file without a pathname-reopen race.

    The descriptor never blocks a later swap from renaming the bundle or
    deleting its backup, on Windows too (:func:`open_read_shared`).
    """
    descriptor = open_read_shared(path)
    try:
        opened_stat = os.fstat(descriptor)
        current_stat = path.lstat()
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or not stat.S_ISREG(current_stat.st_mode)
            or not os.path.samestat(opened_stat, current_stat)
        ):
            raise StemArtifactError("stem file changed after artifact validation")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _stream_file(source: BufferedReader) -> Iterator[bytes]:
    # Sync route -> each chunk is a separate anyio threadpool round trip. 1 MiB
    # meant 30-40 GIL handoffs per stem file; under N-deck-parallel loads those
    # compete and serialize throughput well below disk speed. 8 MiB cuts the
    # round trips ~8x with no behavior change.
    with source:
        while chunk := source.read(8 * 1024 * 1024):
            yield chunk


def _stem_manifest_response(stable_id: str, request: Request) -> StemManifestOut | StemUnavailableOut:
    try:
        with _read_barrier(stable_id, request):
            bundle = load_stem_bundle(
                stable_id,
                stems_dir=_stems_dir(request),
                roots=_stem_roots(request),
            )
    except StemBundleNotFoundError as exc:
        recorded_error = _recorded_hydration_error(stable_id)
        if recorded_error is not None:
            _raise_hydrate_error(recorded_error)
        future = _enqueue_hydration(stable_id, request)
        if future is not None:
            return StemUnavailableOut(
                stable_id=stable_id,
                code="STEM_BUNDLE_HYDRATING",
                message="bundle not local yet; fetching from R2",
                hydrating=True,
                progress=_hydration_progress(stable_id, _stems_dir(request)),
            )
        return _unavailable_out(stable_id, exc)
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc
    return _manifest_out(bundle)


@router.get(
    "/{stable_id}/stems",
    response_model=StemManifestOut | StemUnavailableOut,
    responses=STEM_HYDRATION_ERROR_RESPONSES,
)
def get_stem_manifest(
    stable_id: str, request: Request
) -> StemManifestOut | StemUnavailableOut:
    """Return a stored v1 manifest after alignment, or HTTP 200 unavailable when none exists.

    Never blocks on R2: if the bundle is missing locally but the R2 index
    has it, this ENQUEUES a background hydration and returns immediately
    with ``hydrating=True`` (D2). A prior hydration attempt that already
    failed is reported LOUD as HTTP 502, never silently folded into the
    ordinary "no bundle" empty state.
    """
    withdraw = _hold_for_deck(stable_id, request)
    try:
        out = _stem_manifest_response(stable_id, request)
    except BaseException:
        withdraw()
        raise
    if isinstance(out, StemUnavailableOut):
        # No bundle was served, so this read holds none. A hydration it
        # enqueued hands its own served lease to the deck that asked once the
        # bundle lands (``hand_off_to_deck``); that one is the deck load's
        # (Codex on #6820).
        withdraw()
    return out


@router.get("/{stable_id}/stems/state", response_model=StemStateOut)
def get_stem_state(stable_id: str, request: Request) -> StemStateOut:
    """Name this track's stem state. Read-only: never starts a download.

    Agent-native parity for what the deck's stem row shows. Registered BEFORE
    the ``{part}`` route on purpose, which would otherwise read ``state`` as a
    stem part name.
    """
    return _stem_state(stable_id, request)


@router.post(
    "/{stable_id}/stems/hydrate",
    response_model=StemStateOut,
    responses=STEM_HYDRATION_ERROR_RESPONSES,
)
def hydrate_stem_bundle(stable_id: str, request: Request) -> StemStateOut:
    """Fetch this track's stem bundle from the cloud now, and retry a failure.

    The manifest route records a failed fetch and answers 502 from then on so
    a hot loop cannot hammer R2 with the same doomed request. This is the
    explicit way back: it drops that record and starts a fresh fetch through
    the same single-flight path, so the cache floor and the evictor apply
    exactly as they do for a deck load. A bundle already on disk is a no-op.
    Returns at once with the resulting state; poll ``.../stems/state``.
    """
    stems_dir = _stems_dir(request)
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except StemBundleNotFoundError:
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
        _enqueue_hydration(stable_id, request)
    except StemArtifactError as exc:
        # An invalid bundle on disk is repaired by fetching the cloud copy,
        # which replaces it; with no cloud copy there is nothing to retry.
        invalid = HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        )
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
        try:
            enqueued = _enqueue_hydration(stable_id, request)
        except HTTPException as not_indexed:
            detail = not_indexed.detail
            if not isinstance(detail, dict) or detail.get("code") != STEM_BUNDLE_NOT_INDEXED:
                raise
            raise invalid from exc
        if enqueued is None:
            raise invalid from exc
    return _stem_state(stable_id, request)


def _open_stem_part(stable_id: str, part: str, request: Request) -> tuple[StemBundle, BufferedReader]:
    """Resolve the bundle and open ``part`` inside one :func:`_read_barrier` hold.

    The opened descriptor keeps the file a swap later moves away, so the
    stream that follows needs no lock.
    """
    with _read_barrier(stable_id, request):
        bundle = _load_or_http_error(stable_id, request)
        if part not in bundle.parts:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "STEM_PART_NOT_FOUND",
                    "message": (
                        f"unknown stem part {part!r} for a {bundle.layout} bundle; "
                        f"it has {bundle.parts}"
                    ),
                },
            )
        try:
            return bundle, _open_validated_stem(bundle.files[part])
        except (OSError, StemArtifactError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
            ) from exc


def _http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail
    return detail.get("code") if isinstance(detail, dict) else None


def _stem_file_response(stable_id: str, part: str, request: Request) -> StreamingResponse:
    try:
        bundle, source = _open_stem_part(stable_id, part, request)
    except HTTPException as http_exc:
        # Only a missing bundle can be hydrated: a local bundle without the
        # asked part keeps its own 404 STEM_PART_NOT_FOUND (Codex on #6821).
        if _http_error_code(http_exc) != "STEM_BUNDLE_NOT_FOUND":
            raise
        recorded_error = _recorded_hydration_error(stable_id)
        if recorded_error is not None:
            _raise_hydrate_error(recorded_error)
        future = _enqueue_hydration(stable_id, request)
        if future is None:
            raise
        try:
            outcome = future.result(timeout=STEM_PART_HYDRATE_WAIT_S)
        except FutureTimeoutError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "STEM_BUNDLE_HYDRATING",
                    "message": "still fetching from R2; retry shortly",
                },
            ) from None
        except Exception as exc:  # noqa: BLE001 - any escaping exception from the hydration future must still answer the same 502 contract, never an unclassified 500
            recorded = _recorded_hydration_error(stable_id)
            if recorded is not None:
                _raise_hydrate_error(recorded)
            _raise_hydration_failed(str(exc))
        if outcome.status == "hub_error":
            hub_exc = _stem_source_error_from_hub_reason(
                outcome.reason or "hub transport failure"
            )
            _raise_hydrate_error(_HydrateError(hub_exc.code, hub_exc.message))
        if outcome.status == "error":
            recorded = _recorded_hydration_error(stable_id)
            if recorded is not None:
                _raise_hydrate_error(recorded)
            _raise_hydration_failed(outcome.reason or "hydration failed")
        bundle, source = _open_stem_part(stable_id, part, request)
    return StreamingResponse(
        _stream_file(source),
        media_type=bundle.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(os.fstat(source.fileno()).st_size),
        },
    )


@router.get(
    "/{stable_id}/stems/{part}",
    response_class=StreamingResponse,
    responses=STEM_PART_RESPONSES,
)
def get_stem_file(stable_id: str, part: str, request: Request) -> StreamingResponse:
    """Stream one validated stem part using the bundle's container media type.

    Unlike the manifest route, this one DOES wait (bounded) for an in-flight
    hydration: a part request means the caller already decided it wants
    these bytes. A prior failure is surfaced immediately as HTTP 502; a
    fresh attempt that does not finish within
    :data:`STEM_PART_HYDRATE_WAIT_S` answers HTTP 503 so the caller can
    retry rather than hang forever on this one request.
    """
    withdraw = _hold_for_deck(stable_id, request)
    try:
        return _stem_file_response(stable_id, part, request)
    except BaseException:
        withdraw()
        raise


class StemWaveformOut(BaseModel):
    """Mono peak envelope for one stem part (issue #1036)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_version: int = Field(default=1, alias="schema")
    stable_id: str
    part: str
    layout: str
    points: int
    envelope: list[float]


def _stem_waveform_cache_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    if configured is not None:
        return Path(configured) / "state" / "stem-waveform-cache"
    root = Path(os.environ.get("MDT_DATA_DIR", "data"))
    return root / "state" / "stem-waveform-cache"


# Resolves after the first that a waveform request makes when swaps keep
# landing under its decode; the last attempt's error is the answer.
_WAVEFORM_SWAP_RETRIES = 3


# What the read barrier saw: the bundle, the pin scratch dir (or None), the
# pinned links, the stat of every part the decode reads, and the envelope
# when nothing could be pinned and it was built under the barrier.
_ResolvedWaveform = tuple[Any, Path | None, dict[Path, Path], dict[Path, os.stat_result], dict[str, Any] | None]


def _retry_swaps(attempt: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run ``attempt`` again only when a swap landed under it, a bounded number of times.

    Only what a swap landing under the decode raises is retried: the
    builder's identity check, or a part path that vanished. A decode that
    fails or times out on its own fails once (Codex on #6821). The last
    attempt's error is the answer.
    """
    from apps.stems.stem_waveform import StemPartChangedError

    for _ in range(_WAVEFORM_SWAP_RETRIES):
        try:
            return attempt()
        except (StemPartChangedError, FileNotFoundError):
            continue
    return attempt()


def _resolve_waveform_inputs(
    stable_id: str, part: str, request: Request
) -> _ResolvedWaveform:
    """Resolve the bundle and pin the parts the decode reads, under :func:`_read_barrier`.

    Where the parts cannot be pinned the envelope is built here, still
    under the barrier: decoding the canonical paths after it, a swap and its
    rollback (which puts the same inodes back) could land between the bass
    and other decodes and leave every stat as it was, so a mixed envelope
    would pass the identity check and be cached (Sol on #6820). That holds
    writers off for the decode, on volumes that cannot hard-link only.
    """
    from apps.stems.stem_waveform import build_stem_waveform_payload, pin_waveform_inputs

    with _read_barrier(stable_id, request):
        bundle = load_stem_bundle(stable_id, stems_dir=_stems_dir(request), roots=_stem_roots(request))
        scratch, pinned, seen = pin_waveform_inputs(bundle, part)
        if scratch is None:
            payload = build_stem_waveform_payload(
                bundle, part, cache_root=_stem_waveform_cache_root(request), pinned={}, seen=seen
            )
            return bundle, None, {}, seen, payload
    return bundle, scratch, pinned, seen, None


def _build_resolved_waveform(
    resolved: _ResolvedWaveform,
    part: str,
    request: Request,
) -> dict[str, Any]:
    """Build the envelope from what :func:`_resolve_waveform_inputs` saw, then drop its pins.

    An unpinned resolve already built it under the barrier."""
    from apps.stems.stem_waveform import build_stem_waveform_payload, remove_waveform_pins

    bundle, scratch, pinned, seen, built = resolved
    if built is not None:
        return built
    try:
        return build_stem_waveform_payload(
            bundle, part, cache_root=_stem_waveform_cache_root(request), pinned=pinned, seen=seen
        )
    finally:
        if scratch is not None:
            remove_waveform_pins(scratch)  # raises StemPinCleanupError by name (Sol on #6820)


def _stem_waveform_payload(stable_id: str, part: str, request: Request) -> dict[str, Any]:
    """Resolve the bundle under :func:`_read_barrier`, then build the envelope outside it.

    A cold envelope decodes a whole part, too long to hold writers off, so
    the barrier covers only the resolve and hard-linking the parts the
    decode reads (``pin_waveform_inputs``). The decode then reads the
    version the barrier saw even if a swap lands under it, and an
    instrumental envelope never mixes two versions (Codex on #6820). Where
    the parts could not be linked, none is pinned, and the envelope is
    built under the barrier instead (Sol on #6820).
    """
    return _retry_swaps(
        lambda: _build_resolved_waveform(_resolve_waveform_inputs(stable_id, part, request), part, request)
    )


def _stem_waveform_response(stable_id: str, part: str, request: Request) -> StemWaveformOut:
    from apps.stems.stem_waveform import StemPinCleanupError

    try:
        payload = _stem_waveform_payload(stable_id, part, request)
    except StemPinCleanupError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "STEM_PIN_CLEANUP_FAILED", "message": str(exc)},
        ) from exc
    except StemBundleNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_BUNDLE_NOT_FOUND", "message": str(exc)},
        ) from exc
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_PART_NOT_FOUND", "message": str(exc)},
        ) from exc
    return StemWaveformOut(**payload)


@router.get(
    "/{stable_id}/stems/{part}/waveform",
    response_model=StemWaveformOut,
    responses=STEM_PART_RESPONSES,
)
def get_stem_waveform(stable_id: str, part: str, request: Request) -> StemWaveformOut:
    """Return a downsampled mono peak envelope for one validated stem part."""
    withdraw = _hold_for_deck(stable_id, request)
    try:
        return _stem_waveform_response(stable_id, part, request)
    except BaseException:
        withdraw()
        raise


@router.post("/{stable_id}/stems/deck-open")
def mark_stem_deck_open(stable_id: str, request: Request) -> dict[str, str]:
    """A deck has this bundle open. Protects it from eviction until closed.

    Agent-native parity for the deck-load lifecycle: there is no other
    request boundary that tells the eviction path "a deck is playing this
    right now, do not free its bundle regardless of recency" (D3). Refcounted
    (:class:`apps.cloud.stem_hydration.OpenDeckRegistry`), so two open
    references (e.g. two decks on the same track) both need closing.
    """
    _hold_for_deck(stable_id, request, deck_open=True)
    return {"status": "ok", "stable_id": stable_id, "open": "true"}


@router.post("/{stable_id}/stems/deck-close")
def mark_stem_deck_closed(stable_id: str) -> dict[str, str]:
    """Release one deck-open reference. See :func:`mark_stem_deck_open`."""
    stem_hydration.OPEN_DECKS.mark_closed(stable_id)
    return {"status": "ok", "stable_id": stable_id, "open": "false"}


__all__ = [
    "STEM_HYDRATION_ERROR_RESPONSES",
    "STEM_PART_HYDRATE_WAIT_S",
    "StemHydrationProgressOut",
    "StemManifestOut",
    "StemPartOut",
    "StemStateOut",
    "StemUnavailableOut",
    "StemWaveformOut",
    "router",
]
