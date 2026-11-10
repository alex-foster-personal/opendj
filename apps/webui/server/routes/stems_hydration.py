"""On-demand R2 stem hydration helpers for stems routes.

Split from ``stems.py`` for issue #9676.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, Request

from apps.cloud import stem_hydration, stem_index
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
from apps.webui.server.routes.sync_hub_route_errors import raise_sync_hub_unreachable

from .stems_bundle import _stems_dir

# Tests and alert sinks filter on the pre-split logger name, not __name__.
log = logging.getLogger("apps.webui.server.routes.stems")

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
    prefetch: bool = False,
) -> stem_hydration.HydrationOutcome:
    """Hydrate one bundle. A ``prefetch`` (STEM-11747) hands nothing to a deck,
    so the stem-set stays evictable until a deck loads it, and records no
    failure: the prefetcher logs once, and a failed prefetch must never turn
    the deck's own later load into a 502 from a cached error."""
    remember = _noop_failure if prefetch else _remember_hydrate_error
    report = _noop_failure if prefetch else _log_hydration_failure
    try:
        outcome = stem_hydration.hydrate_one(
            stable_id,
            data_dir=data_dir,
            source=source,
            index=index,
            stems_dir=stems_dir,
            skip_reserved=False,  # on-demand deck load NEVER skips reserved ids (D5)
            hand_off_to_deck=not prefetch,  # the deck polls for this bundle next
        )
    except StemSourceError as exc:
        remember(stable_id, exc.code, exc.message)
        report(stable_id, exc.code, exc.message)
        return stem_hydration.HydrationOutcome(
            stable_id=stable_id,
            status="error",
            reason=exc.message,
            code=exc.code,
        )
    except Exception as exc:
        remember(stable_id, "STEM_BUNDLE_HYDRATION_FAILED", str(exc))
        report(stable_id, "STEM_BUNDLE_HYDRATION_FAILED", str(exc))
        raise
    if outcome.status == "error":
        # The outcome carries the source's own code when it had one; without
        # it a monthly-cap refusal was re-labelled STEM_BUNDLE_HYDRATION_FAILED
        # and answered 502 instead of 429/503 (Codex round 5).
        code = outcome.code or "STEM_BUNDLE_HYDRATION_FAILED"
        reason = outcome.reason or "hydration failed"
        remember(stable_id, code, reason)
        report(stable_id, code, reason)
    elif outcome.status == "hub_error":
        reason = outcome.reason or "hub transport failure"
        hub_exc = _stem_source_error_from_hub_reason(reason)
        remember(stable_id, hub_exc.code, hub_exc.message)
        report(stable_id, hub_exc.code, reason)
    else:
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
    return outcome


def _noop_failure(stable_id: str, code: str, reason: str) -> None:
    del stable_id, code, reason


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


def _refresh_index_cache_or_fail(source, data_dir: Path) -> None:
    try:
        source.refresh_index(data_dir)
    except StemSourceError as exc:
        _raise_source_error(exc)
    except Exception as exc:  # noqa: BLE001 -- direct R2 refresh can raise bare transport errors
        _raise_index_refresh_failed(str(exc))


def _enqueue_hydration(stable_id: str, request: Request, *, prefetch: bool = False) -> Future | None:
    """Start background hydration if R2 has this bundle indexed.

    Returns the in-flight future, or ``None`` when there is nothing to
    hydrate -- either hydration is not configured on this machine, or the
    bundle is not in the (local-cache copy of the) R2 index. The index
    lookup here reads ONLY the local cache file (no network), so this is
    always cheap enough to call from the manifest route without blocking it.

    A ``prefetch`` shares ``_INFLIGHT`` on purpose: the state route reads
    ``fetching`` meanwhile, and a deck load that arrives mid-prefetch joins
    the same future. That deck is not marked served by the prefetch; its next
    manifest read finds the stem-set local and holds it.
    """
    deps = _hydration_deps(request)
    if deps is None:
        return None
    source, data_dir = deps
    cache_path = stem_index.local_index_cache_path(data_dir)
    if not cache_path.is_file():
        _refresh_index_cache_or_fail(source, data_dir)
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
            prefetch=prefetch,
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
    detail = (
        {"code": "STEM_BUNDLE_HYDRATION_FAILED", "message": error}
        if isinstance(error, str)
        else {"code": error.code, "message": error.message}
    )
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
