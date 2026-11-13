"""Can this hub presign R2 assets? Measured, never inferred (CLOUDSYNC-4686b).

A hub serves stem bundles and lyric transcripts by minting presigned R2 GETs
(``service_stem_assets``, ``service_lyrics_asr_assets``). The R2 variables
reach the serve process only when its unit is wrapped in ``doppler run``
(``hub_deploy.hub_serve_argv(serve_r2=True)``). A unit re-rendered without
that wrap still boots, still answers health 200 and still syncs rows, while
every presign answers 503. That happened on Thu 1 Oct 2026 and nothing
flagged it for hours, because every check looked for an error and none
asked for the capability.

So the capability is asserted positively. One measurement:

1. read the R2 variables from the process env (``ABSENT`` when missing);
2. mint ONE presigned GET for the stem bundle index, the same SigV4 path the
   presign endpoints use;
3. fetch that URL. Minting is local arithmetic that succeeds with any
   non-empty variables, so only a fetch the bucket accepted proves the URLs
   this hub hands out work (``SERVING``). A refusal is ``FAILED``; no answer
   at all is ``UNKNOWN``, never a verdict.

The fetched index is also the stem half of "are assets indexed": its bundle
count is written beside the hub DB so a LATER process that has lost R2 still
knows there are bundles it cannot serve. The lyric half is counted from the
synced ``lyric_verdict`` rows. Verdict: ``DEGRADED`` when the hub cannot
presign and assets are indexed, or when variables are present and refused.

The measurement runs at startup (logged) and on ``GET /api/v1/sync/status``.
The network half is cached per process for ``CFG.VERIFY_TTL_S``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from apps.cloud import asset_store, stem_index
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub.atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

R2State = Literal["SERVING", "ABSENT", "FAILED", "UNKNOWN"]
R2Verdict = Literal["OK", "DEGRADED", "UNKNOWN"]
StemCountSource = Literal["r2", "last_known", "unmeasured"]

R2_STATES: tuple[str, ...] = ("SERVING", "ABSENT", "FAILED", "UNKNOWN")
R2_VERDICTS: tuple[str, ...] = ("OK", "DEGRADED", "UNKNOWN")


class CFG:
    #: How long one process reuses the network half of a measurement.
    VERIFY_TTL_S: float = 300.0
    VERIFY_TIMEOUT_S: float = 5.0
    PROBE_URL_EXPIRY_S: int = 60
    LAST_KNOWN_FILENAME: str = "hub-r2-capability.json"
    #: R2's error code for "bucket and credentials fine, no such object".
    NO_SUCH_KEY: bytes = b"NoSuchKey"
    CACHE_ATTR: str = "r2_presign_probe_cache"
    CACHE_LOCK_ATTR: str = "r2_presign_probe_lock"


class R2CapabilityError(RuntimeError):
    """The hub's own capability record is unreadable. The message names the file."""


@dataclass(frozen=True)
class PresignProbe:
    """The network half: did one presigned GET of the stem index work?"""

    state: R2State
    reason: str
    indexed_stem_bundles: int | None = None


@dataclass(frozen=True)
class R2Capability:
    state: R2State
    reason: str
    verdict: R2Verdict
    verdict_reason: str
    #: None = never measured: no index was fetched, now or by an earlier process.
    indexed_stem_bundles: int | None
    stem_count_source: StemCountSource
    indexed_lyric_assets: int
    measured_at: str


# ----- the presign itself ----------------------------------------------------


def presign_and_fetch_index(
    env: Mapping[str, str] | None = None, *, timeout_s: float = CFG.VERIFY_TIMEOUT_S
) -> PresignProbe:
    """Mint one presigned GET for the stem index and fetch it.

    No reason string ever carries the URL: it holds a live signature.
    """
    try:
        cfg = CloudConfig.from_env(None if env is None else dict(env))
        asset_store.require_credentials(cfg)
    except MissingEnvError as exc:
        return PresignProbe("ABSENT", f"hub process has no R2 credentials: {exc}")
    try:
        url = asset_store.presign_object_key(cfg, stem_index.INDEX_OBJECT_KEY, CFG.PROBE_URL_EXPIRY_S)
    except asset_store.AssetStoreError as exc:
        return PresignProbe("FAILED", f"presign refused: {exc}")
    if "X-Amz-Signature=" not in url:
        return PresignProbe("FAILED", "presign returned a URL with no signature")
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and CFG.NO_SUCH_KEY in exc.read():
            return PresignProbe("SERVING", "presigned GET accepted; no stem index is published yet", 0)
        return PresignProbe("FAILED", f"R2 refused the presigned stem index GET with HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        detail = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return PresignProbe("UNKNOWN", f"R2 gave no answer to the presigned GET: {detail}")
    try:
        index = stem_index.decode_index(body)
    except stem_index.StemIndexError as exc:
        return PresignProbe("FAILED", f"presigned GET returned an unusable stem index: {exc}")
    return PresignProbe("SERVING", f"presigned GET fetched the stem index ({len(index)} bundles)", len(index))


def cached_presign_probe(app_state: Any) -> PresignProbe:
    """The process's presign probe, re-measured once ``VERIFY_TTL_S`` old."""
    lock = getattr(app_state, CFG.CACHE_LOCK_ATTR, None)
    if lock is None:
        lock = threading.Lock()
        setattr(app_state, CFG.CACHE_LOCK_ATTR, lock)
    with lock:
        cached: tuple[float, PresignProbe] | None = getattr(app_state, CFG.CACHE_ATTR, None)
        if cached is not None and time.monotonic() - cached[0] < CFG.VERIFY_TTL_S:
            return cached[1]
        probe = presign_and_fetch_index()
        setattr(app_state, CFG.CACHE_ATTR, (time.monotonic(), probe))
        return probe


# ----- what is indexed -------------------------------------------------------


def last_known_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / CFG.LAST_KNOWN_FILENAME


def read_last_known_stem_bundles(data_dir: Path) -> int | None:
    """Bundle count from the last index any process here fetched; None if never."""
    path = last_known_path(data_dir)
    if not path.is_file():
        return None
    try:
        count = json.loads(path.read_text(encoding="utf-8"))["indexed_stem_bundles"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise R2CapabilityError(f"{path} is unreadable: {exc!r}") from exc
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise R2CapabilityError(f"{path} holds indexed_stem_bundles={count!r}, not a count")
    return count


def count_indexed_lyric_assets(conn: sqlite3.Connection) -> int:
    """Live synced lyric rows that point at a word-timing artifact in R2."""
    row = conn.execute(
        "SELECT COUNT(*) FROM lyric_verdict WHERE words_content_hash IS NOT NULL AND deleted_at IS NULL"
    ).fetchone()
    return int(row[0])


# ----- verdict ---------------------------------------------------------------


def _assets_phrase(stems: int | None, lyrics: int) -> str:
    stem_part = "an unmeasured number of stem bundles" if stems is None else f"{stems} stem bundle(s)"
    return f"{stem_part} and {lyrics} lyric asset(s) indexed"


def judge(probe: PresignProbe, stems: int | None, lyrics: int) -> tuple[R2Verdict, str]:
    if probe.state == "SERVING":
        return "OK", f"R2 presign serving: {probe.reason}"
    if probe.state == "UNKNOWN":
        return "UNKNOWN", f"R2 presign capability could not be measured: {probe.reason}"
    if probe.state == "FAILED":
        return "DEGRADED", f"hub cannot presign R2 assets: {probe.reason}"
    if probe.state == "ABSENT":
        if (stems or 0) + lyrics > 0:
            return (
                "DEGRADED",
                f"hub cannot presign R2 assets with {_assets_phrase(stems, lyrics)}: "
                f"{probe.reason} Re-run `cloudsync_hub.sh start` with the serve options "
                "((private path), Upgrade order).",
            )
        return "OK", f"R2 presign off and no assets indexed ({_assets_phrase(stems, lyrics)})"
    raise R2CapabilityError(f"unhandled R2 state {probe.state!r}")


def measure(conn: sqlite3.Connection, data_dir: Path, probe: PresignProbe) -> R2Capability:
    measured_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    source: StemCountSource
    if probe.state == "SERVING":
        stems, source = probe.indexed_stem_bundles, "r2"
        write_json_atomic(
            last_known_path(data_dir),
            {"indexed_stem_bundles": stems, "measured_at": measured_at},
        )
    else:
        stems = read_last_known_stem_bundles(data_dir)
        source = "unmeasured" if stems is None else "last_known"
    lyrics = count_indexed_lyric_assets(conn)
    verdict, verdict_reason = judge(probe, stems, lyrics)
    return R2Capability(
        state=probe.state,
        reason=probe.reason,
        verdict=verdict,
        verdict_reason=verdict_reason,
        indexed_stem_bundles=stems,
        stem_count_source=source,
        indexed_lyric_assets=lyrics,
        measured_at=measured_at,
    )


def capability_for_hub(app_state: Any, conn: sqlite3.Connection, data_dir: Path) -> R2Capability | None:
    """This process's capability, or None when it is not a hub."""
    if not machine_identity.is_hub_from_env():
        return None
    return measure(conn, data_dir, cached_presign_probe(app_state))


def verdict_from_status_body(body: Mapping[str, object]) -> tuple[R2Verdict, str]:
    """Read the verdict a hub reported. Anything unreadable is UNKNOWN, never OK."""
    r2 = body.get("r2")
    if not isinstance(r2, dict):
        return "UNKNOWN", "status response carries no r2 capability (not a hub, or an older build)"
    verdict, reason = r2.get("verdict"), r2.get("verdict_reason")
    if verdict == "OK":
        return "OK", str(reason)
    if verdict == "DEGRADED":
        return "DEGRADED", str(reason)
    return "UNKNOWN", f"r2 verdict {verdict!r}: {reason}"


# ----- startup ---------------------------------------------------------------


def _app_data_dir(app_state: Any) -> Path:
    configured = getattr(app_state, "sync_hub_data_dir", None)
    if configured is not None:
        return Path(str(configured))
    return Path(str(app_state.state_db_path)).resolve().parent.parent


def log_startup_capability(app_state: Any) -> R2Capability | None:
    """Measure once at boot so a hub that cannot presign says so in its own log.

    An unreadable ``MDT_IS_HUB`` is logged and left to the request paths,
    which already answer it with their own declared errors: this hook reports
    on a hub, it does not decide whether an engine may boot.
    """
    try:
        if not machine_identity.is_hub_from_env():
            return None
    except machine_identity.MachineIdentityError as exc:
        logger.error("hub R2 capability [UNKNOWN]: cannot tell whether this is a hub: %s", exc)
        return None
    db_path = Path(str(app_state.state_db_path))
    if not db_path.is_file():
        # open_rw would CREATE it. A hub with no DB yet has nothing indexed to
        # report on, and a boot hook must not mint a database as a side effect.
        logger.warning("hub R2 capability [UNKNOWN]: %s does not exist; not measured", db_path)
        return None
    conn = state_db.open_rw(db_path)
    try:
        capability = capability_for_hub(app_state, conn, _app_data_dir(app_state))
    finally:
        conn.close()
    if capability is None:
        return None
    level = {"OK": logging.INFO, "UNKNOWN": logging.WARNING, "DEGRADED": logging.ERROR}[capability.verdict]
    logger.log(
        level,
        "hub R2 capability [%s] state=%s: %s",
        capability.verdict,
        capability.state,
        capability.verdict_reason,
    )
    return capability


__all__ = [
    "CFG",
    "R2_STATES",
    "R2_VERDICTS",
    "PresignProbe",
    "R2Capability",
    "R2CapabilityError",
    "R2State",
    "R2Verdict",
    "cached_presign_probe",
    "capability_for_hub",
    "count_indexed_lyric_assets",
    "judge",
    "last_known_path",
    "log_startup_capability",
    "measure",
    "presign_and_fetch_index",
    "read_last_known_stem_bundles",
    "verdict_from_status_body",
]
