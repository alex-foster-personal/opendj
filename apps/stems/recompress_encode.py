"""Encode side of the stem recompress pass: eligibility re-check, MP3 encode and manifest, staged re-encode (split from apps/stems/recompress.py)."""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from apps.shared.hashing import sha256_file_hex
from apps.stems import stem_size_policy as pol
from apps.stems.artifacts import (
    StemArtifactError,
    StemBundle,
    load_stem_bundle,
)
from apps.stems.recompress_classify import (
    ELIGIBLE,
    MP3_MAX_EXTRA_FRAMES,
    MP3_MEDIA_TYPE,
    BundleChangedError,
    Candidate,
    WorkspaceCleanupError,
    _dir_bytes,
    _raw_manifest,
    _source_duration_s,
    classify,
)
from apps.stems.recompress_snapshot import (
    _decoded_frames,
    _require_recorded_source,
)
from apps.stems.stem_encode import encode_part, require_mp3_encoder


def _require_still_eligible(snapshot_root: Path, stable_id: str, planned: Candidate, library_path: str | None) -> Path:
    """Classify the locked snapshot afresh; refuse if the plan no longer holds.

    ``plan()`` classified the bundle without the lock, possibly long before
    this pass reaches it. A bundle another writer replaced in between (a
    deliberate FLAC tier, an MP3 bundle) is judged here on what is actually
    on disk now, and against the library's CURRENT path for the track
    (``library_path``, re-read from state.db under the lock), not the one the
    plan saw: a track relinked since (an MP3 replaced by a lossless file) is
    refused, and so is any change of the resolved source. Returns the source
    the encoder uses: the one resolved now, which is the planned one.
    """
    now = classify(snapshot_root, stable_id, library_path)
    if now.verdict != ELIGIBLE:
        raise BundleChangedError(
            f"{stable_id}: replaced or relinked since the plan and no longer eligible "
            f"({now.verdict}{': ' + now.detail if now.detail else ''}); left as it is"
        )
    if now.source_path is None or now.source_path != planned.source_path:
        raise BundleChangedError(
            f"{stable_id}: the library source changed since the plan "
            f"({planned.source_path} -> {now.source_path}); left as it is"
        )
    return now.source_path


def _source_signature(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _encode_part_to_mp3(
    bundle: StemBundle,
    stable_id: str,
    part: str,
    out: Path,
    rung1: str,
    source_bytes: int,
    source_kbps: float,
    frame_count: int,
) -> tuple[int, str]:
    """Decode one part (lossless, or an over-budget MP3), encode it to ``out``
    and check its frames.

    ``frame_count`` is the bundle's true length (``_true_frame_count``): an
    MP3 part decoding up to ``MP3_MAX_EXTRA_FRAMES`` longer (LAME's end
    padding) is trimmed to it, so the re-encode starts from exactly those
    frames. Returns the encoded size and the LAME settings that were used.
    """
    import soundfile as sf

    align = bundle.alignment
    label = f"{stable_id}/{part}"
    data, rate = sf.read(str(bundle.files[part]), dtype="float32", always_2d=True)  # type: ignore[index]
    extra = data.shape[0] - frame_count
    if bundle.media_type == MP3_MEDIA_TYPE and 0 < extra <= MP3_MAX_EXTRA_FRAMES:
        data = data[:frame_count]
    if (int(rate), data.shape[0], data.shape[1]) != (align.sample_rate, frame_count, align.channels):
        raise StemArtifactError(
            f"{label}: decoded {rate} Hz x {data.shape} does not match {frame_count} frames of the manifest's {align}"
        )
    encoded, rung2 = encode_part(
        data,
        align.sample_rate,
        codec="mp3",
        part=part,
        mp3_settings=rung1,
        source_bytes=source_bytes,
        source_kbps=source_kbps,
        out_path=out,
    )
    frames = _decoded_frames(out)
    if not frame_count <= frames <= frame_count + MP3_MAX_EXTRA_FRAMES:
        raise StemArtifactError(
            f"{label}: mp3 decodes to {frames} frames, expected {frame_count}..{frame_count + MP3_MAX_EXTRA_FRAMES}"
        )
    return len(encoded), rung2 or rung1


def _true_frame_count(bundle: StemBundle, stable_id: str) -> int:
    """The frames every part really carries.

    A lossless bundle's alignment is exact. An MP3 bundle's may not be: a
    schema v1 manifest records no ``audio`` block, so the reader derives the
    length from the first part's header, which overestimates by up to two
    MP3 frames (measured on silver, Tue 3 Nov 2026: three v1 bundles decode
    1152 to 2176 frames short of it, yet line up with the source at offset
    0). So an MP3 bundle's length is its shortest decoded part, and it must
    lie within ``MP3_MAX_EXTRA_FRAMES`` of the alignment.
    """
    align = bundle.alignment
    if bundle.media_type != MP3_MEDIA_TYPE:
        return align.frame_count
    decoded = {part: _decoded_frames(Path(bundle.files[part])) for part in bundle.parts}  # type: ignore[index]
    shortest = min(min(decoded.values()), align.frame_count)
    spread = {p: f - shortest for p, f in decoded.items()}
    if align.frame_count - shortest > MP3_MAX_EXTRA_FRAMES or max(spread.values()) > MP3_MAX_EXTRA_FRAMES:
        raise StemArtifactError(
            f"{stable_id}: mp3 parts decode to {decoded} frames against an alignment of "
            f"{align.frame_count}; more than {MP3_MAX_EXTRA_FRAMES} apart"
        )
    return shortest


def _mp3_manifest(
    bundle: StemBundle,
    stable_id: str,
    old_raw: dict[str, Any],
    source_path: Path,
    source_sha256: str,
    size_policy: dict[str, Any],
    frame_count: int,
) -> dict[str, Any]:
    """The schema v3 MP3 manifest that replaces ``bundle``'s.

    ``source`` names the file this pass encoded against and the hash it just
    took of it, not the old manifest's: after a relink, or under a legacy
    all-zero hash, the old block would name another file or no hash at all
    (Codex on #6820). Any other key the old block carried is kept.
    """
    align = bundle.alignment
    old_source = old_raw.get("source")
    source = dict(old_source) if isinstance(old_source, dict) else {}
    source.update(path=str(source_path), sha256=source_sha256)
    manifest: dict[str, Any] = {
        "schema_version": 3,
        "stable_id": stable_id,
        "layout": bundle.layout,
        "model": old_raw.get("model") or {"name": bundle.manifest.model.name},
        "source": source,
        "files": {part: f"{part}.mp3" for part in bundle.parts},
        "audio": {
            "sample_rate": align.sample_rate,
            "frame_count": frame_count,
            "channels": align.channels,
        },
        "codec": "mp3",
        "size_policy": size_policy,
        "recompressed_from": {
            "schema_version": old_raw.get("schema_version"),
            "media_type": bundle.media_type,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    if "preset" in old_raw:
        manifest["preset"] = old_raw["preset"]
    return manifest


def _write_mp3_bundle(
    bundle: StemBundle,
    stable_id: str,
    old_raw: dict[str, Any],
    source_path: Path,
    source_digest: str,
    source_bytes: int,
    duration_s: float,
    new_root: Path,
) -> tuple[dict[str, int], dict[str, str], list[dict[str, Any]]]:
    """Encode ``bundle`` to the policy MP3 at ``new_root/stable_id``, write its
    manifest and re-read it strictly. Returns (part_bytes, mp3_settings, violations)."""
    align = bundle.alignment
    source_kbps = pol.effective_bitrate_kbps(source_bytes, duration_s)
    rung1 = pol.mp3_lame_settings_for_source_kbps(source_kbps)

    new_dir = new_root / stable_id
    new_dir.mkdir()
    part_bytes: dict[str, int] = {}
    settings: dict[str, str] = {}
    frame_count = _true_frame_count(bundle, stable_id)
    for part in bundle.parts:
        part_bytes[part], settings[part] = _encode_part_to_mp3(
            bundle, stable_id, part, new_dir / f"{part}.mp3", rung1, source_bytes, source_kbps, frame_count
        )
    violations = [
        dataclasses.asdict(v)
        for v in pol.assert_lossy_stems_not_larger_than_source(
            source_bytes=source_bytes, part_sizes=part_bytes, codec="mp3"
        )
    ]
    manifest = _mp3_manifest(
        bundle,
        stable_id,
        old_raw,
        source_path,
        source_digest,
        {
            "source_bytes": source_bytes,
            "source_kbps": round(source_kbps, 2),
            "mp3_settings": settings,
            "part_bytes": part_bytes,
            "violations": violations,
        },
        frame_count,
    )
    # Every part's hash, as register-stems writes them: the lyrics stems
    # hydrate path fetches parts by these and refuses a manifest without
    # them (Codex on #6820).
    manifest["files_sha256"] = {part: sha256_file_hex(new_dir / rel) for part, rel in manifest["files"].items()}
    from apps.stems.recompress import write_manifest  # late: recompress re-exports this module

    write_manifest(new_dir, manifest)
    reread = load_stem_bundle(stable_id, roots=[new_root])
    if reread.media_type != "audio/mpeg" or reread.alignment != dataclasses.replace(align, frame_count=frame_count):
        raise StemArtifactError(f"{stable_id}: re-read bundle is {reread.media_type} {reread.alignment}")
    return part_bytes, settings, violations


def _remove_work(work: Path, stable_id: str, failure: BaseException | None) -> None:
    """Remove the work directory, or raise naming it and the failure that led here."""
    try:
        shutil.rmtree(work)
    except OSError as cleanup_error:
        during = f" (while handling: {failure})" if failure is not None else ""
        raise WorkspaceCleanupError(
            f"{stable_id}: could not remove the work directory {work}: {cleanup_error}{during}"
        ) from cleanup_error


@dataclasses.dataclass(frozen=True)
class StagedOutcome:
    """One privately staged bundle after :func:`reencode_staged_bundle`."""

    stable_id: str
    before_bytes: int
    after_bytes: int
    media_type_before: str
    mp3_settings: dict[str, str]
    violations: list[dict[str, Any]]


def reencode_staged_bundle(candidate: Candidate) -> StagedOutcome:
    """Re-encode an ELIGIBLE bundle in a PRIVATE staging root to the policy MP3, in place.

    For a copy no reader or scan sees (an R2 hydrate's verify container,
    #9665): no bundle lock, no snapshot, no backup, no journal. On any failure
    the work dir is removed and ``root/stable_id`` is left as it was.

    * [if] the candidate is not ELIGIBLE or has no source [then] ValueError, nothing written
    * [if] this install cannot write MP3 [then] Mp3EncoderUnavailableError before any decode
    * [if] the encode succeeds [then] ``root`` holds only ``stable_id``, now MP3
    """
    if candidate.verdict != ELIGIBLE or candidate.source_path is None:
        raise ValueError(f"{candidate.stable_id} is not eligible: {candidate.verdict}")
    require_mp3_encoder()
    root, stable_id, source_path = candidate.root, candidate.stable_id, candidate.source_path
    bundle_dir = root / stable_id
    bundle = load_stem_bundle(stable_id, roots=[root])
    old_raw = _raw_manifest(bundle_dir)
    work = root / ".reencode"
    work.mkdir()
    try:
        source_digest = _require_recorded_source(stable_id, old_raw, source_path)
        duration_s = _source_duration_s(source_path)
        if duration_s is None:
            raise BundleChangedError(f"{stable_id}: {source_path} reports no duration; left as it is")
        _parts, settings, violations = _write_mp3_bundle(
            bundle, stable_id, old_raw, source_path, source_digest, source_path.stat().st_size, duration_s, work
        )
        before = _dir_bytes(bundle_dir)
        old = root / ".reencode-old"
        os.rename(bundle_dir, old)
        os.rename(work / stable_id, bundle_dir)
        shutil.rmtree(old)
    finally:
        _remove_work(work, stable_id, sys.exc_info()[1])
    return StagedOutcome(stable_id, before, _dir_bytes(bundle_dir), bundle.media_type, settings, violations)
