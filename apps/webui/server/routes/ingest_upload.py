"""Ingest upload endpoint: stage real bytes + duplicate check at the door.

Split out of :mod:`apps.webui.server.routes.ingest` (which keeps config,
coverage and the refresh job): this module owns POST /ingest/upload and the
fingerprint duplicate policy. Shared CFG (INGEST_INBOX, DUP_* thresholds,
BATCH_RE, open_ro) stays in ``ingest`` and is read via module attributes so
a test pointing ``ingest.INGEST_INBOX`` at a tmp dir steers uploads too.

Requirements (mini-PRD):
  ✔︎ ✅ POST upload: stage real bytes + duration & fingerprint dup check.
    [if] the file is not audio or the batch name is invalid [then ⛔️] 422
    [if] the upload is empty [then ⛔️] 422, temp file removed
    [if] the tinytag tag reader is not importable [then ⛔️] 503
    TAG_READER_UNAVAILABLE before any bytes are staged
    [if] fingerprint >= threshold match exists and force is not set
    [then] file skipped with duplicate_of reported
    [if] the destination filename already exists in the batch [then ⛔️] 409,
    existing staged file untouched
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from apps.shared import _tagreader
from apps.shared._tagreader import HAS_TAG_READER
from apps.shared.fingerprints import ChromaprintMissing, compare, compute
from apps.shared.paths import AUDIO_EXTENSIONS
from apps.webui.server.routes import ingest as ingest_cfg

_TAG_READER_UNAVAILABLE_MESSAGE = (
    "ingest upload requires the 'tinytag' tag reader (a core dependency) for "
    "duration-based duplicate detection; reinstall the environment (uv sync)"
)

router = APIRouter(prefix="/ingest", tags=["ingest"])

Verdict = Literal["new", "possible_duplicate", "skipped_duplicate"]


class UploadFileResult(BaseModel):
    filename: str
    staged_path: str | None
    skipped_duplicate: bool
    verdict: Verdict
    duplicate_of: dict | None        # {stable_id, title, artist, method, score}
    duration_s: float | None
    fingerprint_method: str          # "chromaprint" | "duration"


class UploadOut(BaseModel):
    batch: str
    dest_dir: str
    results: list[UploadFileResult]


class DecideIn(BaseModel):
    batch: str
    filename: str
    action: Literal["accept", "reject"]


def _raise_tag_reader_unavailable() -> NoReturn:
    raise HTTPException(
        status_code=503,
        detail={
            "code": "TAG_READER_UNAVAILABLE",
            "message": _TAG_READER_UNAVAILABLE_MESSAGE,
        },
    )


def _duration_s(path: Path) -> float | None:
    _tagreader.require()
    # tinytag has no raw ADTS .aac reader (it reports 0.03 s for a 7.3 s
    # stream), and without a true duration the duplicate check is skipped and
    # an exact duplicate stages as new. Walk ADTS frames first; it returns
    # None at once for any other format.
    duration = _tagreader.adts_duration(path)
    if duration is None:
        if _tagreader.starts_with_adts(path):
            # A damaged ADTS stream is refused, never staged as new on a
            # tinytag misread; see _stage_one_upload.
            raise _tagreader.TagReadError("damaged ADTS stream: the frame walk failed")
        # A file tinytag cannot parse is damaged, not durationless: the
        # TagReadError reaches _hold_duration, which refuses it with a 422.
        duration = _tagreader.read(path).duration
    if not duration:
        # Parsed, but no audio frames (an ID3-only mp3, an empty container):
        # damaged like a parse failure, as audio_playable._probe_tags rules.
        raise _tagreader.TagReadError("no audio frames: missing or zero duration")
    return float(duration)


def _dup_candidates(duration_s: float) -> list[tuple[str, str, str, str]]:
    """(stable_id, title, artist, file_path) within duration tolerance."""
    lo = int(duration_s * 1000) - ingest_cfg.DUP_DURATION_TOLERANCE_MS
    hi = int(duration_s * 1000) + ingest_cfg.DUP_DURATION_TOLERANCE_MS
    conn = ingest_cfg.open_ro()
    try:
        return conn.execute(
            "SELECT stable_id, title, artists_json, file_path FROM tracks "
            "WHERE duration_ms BETWEEN ? AND ? AND file_path IS NOT NULL "
            "AND deleted_at IS NULL",
            (lo, hi),
        ).fetchall()
    finally:
        conn.close()


def _best_duplicate(staged: Path, duration_s: float) -> tuple[dict | None, str]:
    """Best duplicate candidate and the method actually used."""
    candidates = [
        c for c in _dup_candidates(duration_s) if Path(c[3]).exists()
    ][: ingest_cfg.DUP_MAX_FP_CANDIDATES]
    if not candidates:
        return None, "duration"
    try:
        new_fp = compute(staged)
    except ChromaprintMissing:
        # No fingerprint available: report the closest duration match but say
        # so explicitly - the client decides, nothing silently passes as dup.
        sid, title, artist, _ = candidates[0]
        return (
            {"stable_id": sid, "title": title, "artist": artist,
             "method": "duration", "score": None},
            "duration",
        )
    best: dict | None = None
    for sid, title, artist, fp_path in candidates:
        score = compare(new_fp, compute(Path(fp_path)))
        if best is None or score > best["score"]:
            best = {"stable_id": sid, "title": title, "artist": artist,
                    "method": "chromaprint", "score": round(score, 4)}
    return best, "chromaprint"


def _safe_relative_path(dest_dir: Path, name: str) -> Path:
    """Resolve a client-supplied relative path under dest_dir."""
    rel = Path(name)
    if not name or rel.is_absolute() or ".." in rel.parts or name.startswith("/"):
        raise HTTPException(422, f"invalid relative path: {name!r}")
    if rel.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(422, f"not an audio file: {name!r}")
    final = (dest_dir / rel).resolve()
    dest_resolved = dest_dir.resolve()
    try:
        final.relative_to(dest_resolved)
    except ValueError:
        raise HTTPException(422, f"path escapes dest dir: {name!r}")
    return final


def _hold_path(final: Path) -> Path:
    return final.parent / (final.name + ".part")


def _hold_duration(hold: Path, rel_name: str) -> float | None:
    """Duration of a held upload; drop the hold and raise when it cannot be staged."""
    try:
        return _duration_s(hold)
    except ImportError:
        if hold.exists():
            hold.unlink()
        _raise_tag_reader_unavailable()
    except _tagreader.TagReadError as exc:
        hold.unlink(missing_ok=True)
        raise HTTPException(422, f"damaged audio: {rel_name}: {exc}") from exc


def _stage_one_upload(
    dest_dir: Path, up: UploadFile, batch: str, force: bool
) -> UploadFileResult:
    """Stage one uploaded file, or skip it as a duplicate. Raises on refusal."""
    name = up.filename or ""
    final = _safe_relative_path(dest_dir, name)
    rel_name = str(final.relative_to(dest_dir.resolve()))
    hold = _hold_path(final)
    if final.exists():
        raise HTTPException(
            409,
            f"{rel_name!r} is already staged in batch {batch!r} ({final}); "
            "remove it or pick a new batch name",
        )
    if hold.exists():
        raise HTTPException(
            409,
            f"{rel_name!r} is awaiting a duplicate decision in batch {batch!r}",
        )
    if not HAS_TAG_READER:
        _raise_tag_reader_unavailable()
    final.parent.mkdir(parents=True, exist_ok=True)
    with hold.open("wb") as fh:
        shutil.copyfileobj(up.file, fh)
    if hold.stat().st_size == 0:
        hold.unlink()
        raise HTTPException(422, f"empty upload: {rel_name}")

    duration = _hold_duration(hold, rel_name)
    dup, method = (None, "duration")
    if duration is not None:
        dup, method = _best_duplicate(hold, duration)
    is_confirmed_dup = (
        dup is not None
        and dup["method"] == "chromaprint"
        and dup["score"] >= ingest_cfg.DUP_FP_THRESHOLD
    )
    if is_confirmed_dup and not force:
        hold.unlink()
        return UploadFileResult(
            filename=rel_name, staged_path=None, skipped_duplicate=True,
            verdict="skipped_duplicate",
            duplicate_of=dup, duration_s=duration, fingerprint_method=method,
        )
    is_possible_dup = dup is not None and not is_confirmed_dup
    if is_possible_dup and not force:
        return UploadFileResult(
            filename=rel_name, staged_path=None, skipped_duplicate=False,
            verdict="possible_duplicate",
            duplicate_of=dup, duration_s=duration, fingerprint_method=method,
        )
    hold.rename(final)
    return UploadFileResult(
        filename=rel_name, staged_path=str(final), skipped_duplicate=False,
        verdict="new",
        duplicate_of=dup, duration_s=duration, fingerprint_method=method,
    )


@router.post(
    "/upload",
    response_model=UploadOut,
    responses={
        503: {
            "description": (
                "The tinytag tag reader is not importable."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "code": "TAG_READER_UNAVAILABLE",
                            "message": _TAG_READER_UNAVAILABLE_MESSAGE,
                        }
                    }
                }
            },
        },
    },
)
async def upload(
    files: Annotated[list[UploadFile], File()],
    batch: Annotated[str, Form()],
    force: Annotated[bool, Form()] = False,
) -> UploadOut:
    if not ingest_cfg.BATCH_RE.match(batch):
        raise HTTPException(
            422, f"invalid batch name {batch!r} (need {ingest_cfg.BATCH_RE.pattern})"
        )
    dest_dir = ingest_cfg.INGEST_INBOX / batch
    dest_dir.mkdir(parents=True, exist_ok=True)
    results = [_stage_one_upload(dest_dir, up, batch, force) for up in files]
    return UploadOut(batch=batch, dest_dir=str(dest_dir), results=results)


@router.post("/upload/decide", response_model=UploadFileResult)
def decide_upload(body: DecideIn) -> UploadFileResult:
    if not ingest_cfg.BATCH_RE.match(body.batch):
        raise HTTPException(422, f"invalid batch name {body.batch!r}")
    dest_dir = ingest_cfg.INGEST_INBOX / body.batch
    if not dest_dir.is_dir():
        raise HTTPException(404, f"batch {body.batch!r} not found")
    final = _safe_relative_path(dest_dir, body.filename)
    rel_name = str(final.relative_to(dest_dir.resolve()))
    hold = _hold_path(final)
    if body.action == "accept":
        if not hold.exists():
            if final.exists():
                raise HTTPException(409, f"{rel_name!r} already staged")
            raise HTTPException(404, f"no hold file for {rel_name!r}")
        hold.rename(final)
        return UploadFileResult(
            filename=rel_name, staged_path=str(final), skipped_duplicate=False,
            verdict="new", duplicate_of=None, duration_s=None,
            fingerprint_method="duration",
        )
    if not hold.exists():
        if final.exists():
            raise HTTPException(409, f"{rel_name!r} already staged")
        raise HTTPException(404, f"no hold file for {rel_name!r}")
    hold.unlink()
    return UploadFileResult(
        filename=rel_name, staged_path=None, skipped_duplicate=True,
        verdict="skipped_duplicate", duplicate_of=None, duration_s=None,
        fingerprint_method="duration",
    )
