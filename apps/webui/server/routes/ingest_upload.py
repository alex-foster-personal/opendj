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
    [if] more library tracks fall within the duration tolerance than are
    fingerprinted [then] the closest by duration are compared (ties broken
    by closest file size), and a track with no stored duration is measured
    from its file
    [if] a library track's file exists but cannot be read [then] it is
    logged and named in unchecked_library_tracks, never silently dropped
    [if] the destination filename already exists in the batch [then ⛔️] 409,
    existing staged file untouched
    [if] a later file in a multi-file request is refused (409 or 422)
    [then ⛔️] files staged or held earlier in that request are removed and
    files staged by earlier requests are left untouched (LIBM-12329)
  ✔︎ A multi-file request stages all of its files or none (issue #12295).
    [if] a later file collides with a staged name [then ⛔️] 409, no earlier file staged
    [if] a later file is empty or rejected [then ⛔️] 422, no earlier file staged
    [if] every file is valid and unique [then] all of them are staged
"""
from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from apps.shared import _tagreader
from apps.shared._tagreader import HAS_TAG_READER
from apps.shared.fingerprints import ChromaprintMissing, FingerprintFailed, compare, compute
from apps.shared.paths import AUDIO_EXTENSIONS
from apps.webui.server.routes import ingest as ingest_cfg

_TAG_READER_UNAVAILABLE_MESSAGE = (
    "ingest upload requires the 'tinytag' tag reader (a core dependency) for "
    "duration-based duplicate detection; reinstall the environment (uv sync)"
)

router = APIRouter(prefix="/ingest", tags=["ingest"])
MOUNT_UNDER_API_PREFIX = True
_log = logging.getLogger(__name__)

Verdict = Literal["new", "possible_duplicate", "skipped_duplicate"]


class UploadFileResult(BaseModel):
    filename: str
    staged_path: str | None
    skipped_duplicate: bool
    verdict: Verdict
    duplicate_of: dict | None        # {stable_id, title, artist, method, score}
    duration_s: float | None
    fingerprint_method: str          # "chromaprint" | "duration"
    # Library rows whose file exists but could not be read, so the duplicate
    # check could not compare against them.
    unchecked_library_tracks: list[str]


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


def _duration_s(path: Path) -> float:
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


def _stored_or_file_duration_ms(duration_ms: int | None, path: Path) -> int:
    """The row's stored duration, else the file's own duration.

    Raises TagReadError or OSError when the file has to be measured and cannot be read.
    """
    if duration_ms and duration_ms > 0:
        return duration_ms
    return int(_duration_s(path) * 1000)


def _duration_gap_ms(known_ms: int, target_ms: int) -> int:
    """Duration distance for ranking, with whole-second storage error forgiven.

    rekordbox stores whole seconds, rounded either way, so a stored value that
    is a whole second can sit up to 999 ms from the file's true duration. Such
    a row ranks as 0 ms away when it is within that error, so an exact copy
    stored a second off never ranks behind unrelated rows on the next second.
    """
    gap = abs(known_ms - target_ms)
    if known_ms % 1000 == 0:
        return max(0, gap - 999)
    return gap


def _dup_candidates(
    duration_s: float, size_bytes: int, limit: int | None = None
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """(stable_id, title, artist, file_path) within duration tolerance, closest first,
    and the stable_ids of rows whose file exists but could not be read.

    A row with no stored duration is measured from its file, so a library track
    whose duration was never recorded can still be found as the duplicate.
    Durations imported from rekordbox are whole seconds, so a whole-second row
    within a second of the upload counts as 0 ms away (_duration_gap_ms) and
    many rows tie on duration distance; ties go to the file closest in size to the upload (an
    exact copy is 0 bytes away), then stable_id for a stable order. With a
    limit, every row tied with the last one kept is kept too, so no tie-break
    by id can push the copy out of the fingerprinted set.

    A row whose file is gone (a broken link) has nothing to compare and is
    left out. A row whose file exists but cannot be read is logged and
    returned as unchecked, so the upload result says the check was partial.
    """
    target = int(duration_s * 1000)
    tolerance = ingest_cfg.DUP_DURATION_TOLERANCE_MS
    conn = ingest_cfg.open_ro()
    try:
        rows = conn.execute(
            "SELECT stable_id, title, artists_json, file_path, duration_ms FROM tracks "
            "WHERE file_path IS NOT NULL AND deleted_at IS NULL "
            "AND (duration_ms BETWEEN ? AND ? OR duration_ms IS NULL OR duration_ms <= 0)",
            (target - tolerance, target + tolerance),
        ).fetchall()
    finally:
        conn.close()
    ranked: list[tuple[int, int, str, tuple[str, str, str, str]]] = []
    unchecked: list[str] = []
    for sid, title, artist, file_path, duration_ms in rows:
        path = Path(file_path)
        if not path.is_file():
            continue
        try:
            known_ms = _stored_or_file_duration_ms(duration_ms, path)
            size = path.stat().st_size
        except (_tagreader.TagReadError, OSError) as exc:
            _log.warning("ingest upload: duplicate check could not read %s (%s): %s",
                         sid, file_path, exc)
            unchecked.append(sid)
            continue
        if abs(known_ms - target) > tolerance:
            continue
        ranked.append(
            (_duration_gap_ms(known_ms, target), abs(size - size_bytes), sid,
             (sid, title, artist, file_path))
        )
    ranked.sort(key=lambda r: r[:3])
    if limit is not None and len(ranked) > limit:
        cutoff = ranked[limit - 1][:2]
        ranked = ranked[:limit] + [r for r in ranked[limit:] if r[:2] == cutoff]
    return [row for *_, row in ranked], unchecked


def _best_duplicate(staged: Path, duration_s: float) -> tuple[dict | None, str, list[str]]:
    """Best duplicate candidate, the method actually used, and the unchecked rows."""
    candidates, unchecked = _dup_candidates(
        duration_s, staged.stat().st_size, limit=ingest_cfg.DUP_MAX_FP_CANDIDATES
    )
    if not candidates:
        return None, "duration", unchecked
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
            unchecked,
        )
    best: dict | None = None
    for sid, title, artist, fp_path in candidates:
        try:
            library_fp = compute(Path(fp_path))
        except (ChromaprintMissing, FingerprintFailed, OSError) as exc:
            # The upload was just fingerprinted, so a backend works: the fpcalc
            # path reports a file it cannot decode as ChromaprintMissing too.
            _log.warning("ingest upload: duplicate check could not fingerprint %s (%s): %s",
                         sid, fp_path, exc)
            unchecked.append(sid)
            continue
        score = compare(new_fp, library_fp)
        if best is None or score > best["score"]:
            best = {"stable_id": sid, "title": title, "artist": artist,
                    "method": "chromaprint", "score": round(score, 4)}
    return best, "chromaprint", unchecked


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


def stage_path_upload(
    dest_dir: Path,
    source: Path,
    rel_name: str,
    batch: str,
    force: bool,
) -> UploadFileResult:
    """Stage bytes already on disk through the same door as multipart upload."""
    with source.open("rb") as fh:
        up = UploadFile(filename=rel_name, file=fh)
        return _stage_one_upload(dest_dir, up, batch, force)


def _stage_one_upload(
    dest_dir: Path, up: UploadFile, batch: str, force: bool
) -> UploadFileResult:
    """Stage one uploaded file, or skip it as a duplicate. Raises on refusal."""
    (result,) = _stage_uploads(dest_dir, [up], batch, force)
    return result


# ----- all-or-nothing staging (issue #12295) -------------------------------
@dataclass
class _PreparedUpload:
    """One file of a request, fully checked and written to a request-private temp, not yet visible."""

    temp: Path
    final: Path
    hold: Path
    batch: str
    result: UploadFileResult


def _temp_path(final: Path) -> Path:
    """A request-private name beside ``final``; the ``.part`` ending keeps it out of every staged-file listing."""
    return final.parent / f".{final.name}.{uuid.uuid4().hex}.upload.part"


def _refuse_if_taken(final: Path, hold: Path, rel_name: str, batch: str) -> None:
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


def _make_dirs(path: Path, created: list[Path]) -> None:
    """mkdir -p ``path``, appending each directory this call created to ``created``."""
    missing = [p for p in (path, *path.parents) if not p.exists()]
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)


def _prepare_one_upload(
    dest_dir: Path, up: UploadFile, batch: str, force: bool, claimed: set[Path], created: list[Path]
) -> _PreparedUpload:
    """Check one file and write it to a private temp. Raises on refusal, leaving no temp behind."""
    name = up.filename or ""
    final = _safe_relative_path(dest_dir, name)
    # POSIX separators on every OS, like materialize: the client named the file
    # with /, and a Windows backslash here would not round-trip to it.
    rel_name = final.relative_to(dest_dir.resolve()).as_posix()
    hold = _hold_path(final)
    if final in claimed:
        raise HTTPException(409, f"{rel_name!r} is named twice in this upload")
    _refuse_if_taken(final, hold, rel_name, batch)
    if not HAS_TAG_READER:
        _raise_tag_reader_unavailable()
    claimed.add(final)
    _make_dirs(final.parent, created)
    temp = _temp_path(final)
    try:
        with temp.open("wb") as fh:
            shutil.copyfileobj(up.file, fh)
        _refuse_if_empty(temp, rel_name)
        result = _verdict_for(temp, final, rel_name, force)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return _PreparedUpload(temp=temp, final=final, hold=hold, batch=batch, result=result)


def _refuse_if_empty(temp: Path, rel_name: str) -> None:
    if temp.stat().st_size == 0:
        raise HTTPException(422, f"empty upload: {rel_name}")


def _verdict_for(temp: Path, final: Path, rel_name: str, force: bool) -> UploadFileResult:
    """The duplicate verdict for one written temp; ``staged_path`` is where it will land."""
    duration = _hold_duration(temp, rel_name)
    dup: dict | None = None
    method = "duration"
    unchecked: list[str] = []
    if duration is not None:
        dup, method, unchecked = _best_duplicate(temp, duration)
    is_confirmed_dup = (
        dup is not None
        and dup["method"] == "chromaprint"
        and dup["score"] >= ingest_cfg.DUP_FP_THRESHOLD
    )
    if is_confirmed_dup and not force:
        verdict: Verdict = "skipped_duplicate"
    elif dup is not None and not is_confirmed_dup and not force:
        verdict = "possible_duplicate"
    else:
        verdict = "new"
    return UploadFileResult(
        filename=rel_name, staged_path=str(final) if verdict == "new" else None,
        skipped_duplicate=verdict == "skipped_duplicate", verdict=verdict,
        duplicate_of=dup, duration_s=duration, fingerprint_method=method,
        unchecked_library_tracks=unchecked,
    )


def _publish(prepared: _PreparedUpload) -> Path | None:
    """Move one checked temp to where its verdict puts it; the path it now holds, or None when dropped."""
    _refuse_if_taken(prepared.final, prepared.hold, prepared.result.filename, prepared.batch)
    verdict = prepared.result.verdict
    landed: Path | None
    if verdict == "skipped_duplicate":
        prepared.temp.unlink()
        landed = None
    elif verdict == "possible_duplicate":
        landed = prepared.temp.rename(prepared.hold)
    elif verdict == "new":
        landed = prepared.temp.rename(prepared.final)
    else:
        raise AssertionError(f"unhandled verdict {verdict!r}")
    return landed


def _roll_back(prepared: list[_PreparedUpload], published: list[Path], created: list[Path]) -> None:
    """Remove every file and directory this request wrote, newest first."""
    for path in [*published, *(p.temp for p in prepared)]:
        path.unlink(missing_ok=True)
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError as exc:
            # Only a write by someone else lands in a directory this request made.
            _log.warning("ingest upload: rollback kept %s, it is not empty: %s", directory, exc)


def _stage_uploads(dest_dir: Path, uploads: list[UploadFile], batch: str, force: bool) -> list[UploadFileResult]:
    """Stage every file of one request or none of them.

    Each file is checked and written to a private temp first. Only when every
    file passed are the temps moved to their staged or held names; a refusal
    of any file, or a move that fails, removes everything this request wrote,
    so the batch directory is left exactly as it was before the request.
    """
    created: list[Path] = []
    claimed: set[Path] = set()
    prepared: list[_PreparedUpload] = []
    published: list[Path] = []
    try:
        _make_dirs(dest_dir, created)
        for up in uploads:
            # One at a time, not extend: the rollback needs every file prepared before a refusal.
            prepared.append(_prepare_one_upload(dest_dir, up, batch, force, claimed, created))  # noqa: PERF401
        for item in prepared:
            landed = _publish(item)
            if landed is not None:
                published.append(landed)
    except BaseException:
        _roll_back(prepared, published, created)
        raise
    return [item.result for item in prepared]


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
    results = _stage_uploads(dest_dir, files, batch, force)
    return UploadOut(batch=batch, dest_dir=str(dest_dir), results=results)


@router.post("/upload/decide", response_model=UploadFileResult)
def decide_upload(body: DecideIn) -> UploadFileResult:
    if not ingest_cfg.BATCH_RE.match(body.batch):
        raise HTTPException(422, f"invalid batch name {body.batch!r}")
    dest_dir = ingest_cfg.INGEST_INBOX / body.batch
    if not dest_dir.is_dir():
        raise HTTPException(404, f"batch {body.batch!r} not found")
    final = _safe_relative_path(dest_dir, body.filename)
    rel_name = final.relative_to(dest_dir.resolve()).as_posix()
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
            unchecked_library_tracks=[],
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
        unchecked_library_tracks=[],
    )
