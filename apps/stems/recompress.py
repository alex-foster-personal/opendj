"""Re-compress existing lossless stem bundles of lossy sources to MP3 (STEM-58),
and shrink MP3 bundles made under the old per-part rule to STEM-8231's.

STEM-8231 (Mon 2 Nov / Tue 3 Nov 2026): an MP3 bundle whose parts are over the
whole-set rule (each part at max(floor(source_kbps / 4) on the ladder, its
floor: 96 kbps bass and drums, 64 vocals and other), measured as part bytes
over the stems' duration, is decoded and re-encoded to that rule through the
same encoder; a compliant one is skipped as SKIP_AT_POLICY. Everything below
(locking, snapshot, verify, backup, journaled swap) applies to both.

WHY. From Fri 2 Oct 2026 (STEM-51) until STEM-57, the installed app wrote every
stem of an MP3/M4A source as lossless FLAC, and the Modal/relay routes have
always written FLAC (farm schema v2). Measured on a real 60 s mix, four FLAC
parts total 18.7 MB against a 1.4-2.4 MB source: each part 2.5x to 4.2x the
source. This pass brings those bundles to the size policy a new separation
follows (apps/stems/stem_size_policy.py): a lossy source gets MP3 parts at or
under the source bitrate, through the same encoder and ladder
(apps.stems.stem_encode.encode_part).

WHAT IT TOUCHES. Bundle directories under this machine's stem roots
(``<data>/state/stems`` and the RoFormer root), and only those whose parts are
FLAC or WAV while their source is MP3/M4A. A lossless source keeps FLAC; a
bundle that is already MP3 is left alone. R2 copies and the hydration index are
NOT touched: re-publishing is a separate, deliberate step.

SAFETY.
  * Dry run by default: it prints what it would do and writes nothing.
  * ``--live`` writes the new bundle beside the old one, loads it with the real
    reader (apps.stems.artifacts.load_stem_bundle) and checks every part's
    decoded frame count, and only then swaps it in.
  * The old bundle is MOVED to a timestamped backup directory, never deleted.
    Deleting the backup, after a listen, is the operator's call. The move is a
    rename on the same volume, journaled under
    ``<data>/state/.stem-recompress-swap/`` until the new bundle is in place,
    so a pass killed between the two renames is repaired by the next
    ``--live`` run before it plans (the backup goes back).
  * It refuses while a stems job is queued or running in ``jobs.db``, and when
    ``jobs.db`` cannot be read (absence of evidence is not idle;
    (private path)). ``--no-jobs-db-ok`` accepts a data dir that
    has never had a jobs database. The same check runs again immediately
    before EACH bundle is swapped in: a job that appeared mid-pass stops that
    swap and the rest of the pass (exit 3), leaving those bundles as they
    were.
  * Every writer of a bundle directory holds its cross-process lock
    (apps/stems/bundle_lock.py): a stems job's publish, an R2 hydrate, and
    this pass, which holds it to snapshot a bundle and again for the job
    re-check and the swap, never for the encode. Under that lock the bundle's
    content fingerprint must still match the snapshot; a bundle another writer
    replaced mid-encode is left as that writer published it and counted as a
    failure, never clobbered or moved into the backup.
  * A lossless bundle whose manifest says FLAC was chosen on purpose (a FLAC
    tier's preset, or an explicit codec from a policy-aware writer) is left
    alone; see deliberate_lossless_reason.
  * Each bundle is staged in a work directory beside the stem root
    (``<root parent>/.stem-recompress-work/<root name>/``), never inside it, so
    a concurrent cache-budget scan never counts or evicts against it. A work
    directory that cannot be removed is an error naming the directory, never
    a silent leftover.
  * A part's source is the library's current file for that stable_id
    (state.db), else the manifest's recorded source path; with neither on disk
    the bundle is skipped and counted, because the bitrate ladder needs the
    source's real size.

Run (on the machine that holds the stems):

    uv run --extra analysis python -m apps.stems.recompress            # dry run
    uv run --extra analysis python -m apps.stems.recompress --live --limit 5
    uv run --extra analysis python -m apps.stems.recompress --live
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePath
from typing import Any

from apps.shared.hashing import sha256_file_hex
from apps.shared.shared_read_open import open_read_shared
from apps.shared.state import locations as state_locations
from apps.shared.state import machine_identity
from apps.stems import stem_size_policy as pol
from apps.stems.artifacts import (
    StemArtifactError,
    StemBundle,
    load_stem_bundle,
)
from apps.stems.bundle_lock import (
    SwapBackupStuckError,
    _rename_durably,
    beside_stem_root,
    bundle_lock,
    data_dir_of_bundle,
    discard_landed_swap_backup,
    readers_held_off,
    swap_backup_path,
    undo_failed_swap,
)
from apps.stems.bundle_lock import (
    fsync_dir as _fsync_dir,
)
from apps.stems.bundle_lock import (
    fsync_tree as _fsync_tree,
)
from apps.stems.stem_encode import encode_part

LOSSLESS_MEDIA_TYPES: frozenset[str] = frozenset({"audio/flac", "audio/wav"})
LIVE_JOB_STATUSES: tuple[str, ...] = ("queued", "running", "cancelling")
MP3_FRAME_SAMPLES = 1152
# LAME adds encoder delay plus padding to the last frame: never fewer frames
# than the source, at most two MP3 frames more (tests/scripts/
# test_stem_bundle_worker_codec.py derives the same bound).
MP3_MAX_EXTRA_FRAMES = 2 * MP3_FRAME_SAMPLES

ELIGIBLE = "eligible"
SKIP_ALREADY_LOSSY = "already lossy"
SKIP_AT_POLICY = "mp3 already at the whole-set rule"
MP3_MEDIA_TYPE = "audio/mpeg"
# A part counts as at policy when its measured bitrate is within this factor
# of its whole-set target: CBR container overhead (Info frame, LAME padding,
# ID3) is well under 2% on a track, so 5% never mistakes an old 320 kbps or
# V0 part for a compliant one, and never flags a fresh STEM-8231 part.
AT_POLICY_KBPS_SLACK = 1.05
SKIP_LOSSLESS_SOURCE = "lossless source keeps FLAC"
SKIP_NO_SOURCE = "no source file on disk"
SKIP_UNKNOWN_SOURCE = "source format has no policy"
SKIP_INVALID = "bundle does not load"
SKIP_DELIBERATE_LOSSLESS = "lossless by choice (manifest provenance)"
SKIP_SOURCE_DURATION = "source duration does not match the stems"

# The local worker's demucs drift guard (scripts/stem_bundle_worker.py): a
# source whose length differs from the stems' by more than this is another
# recording, not the one these stems were separated from.
DURATION_ABS_TOL_S = 0.5
DURATION_REL_TOL = 0.005


class StemsJobLiveError(RuntimeError):
    """A stems job is queued or running, or that cannot be ruled out."""


class WorkspaceCleanupError(RuntimeError):
    """A re-compress work directory could not be removed and is still on disk."""


class BundleChangedError(RuntimeError):
    """The bundle was rewritten by another writer while this pass encoded it."""


class WorkVolumeError(RuntimeError):
    """The work directory is not on the stem root's volume, so no rename can swap it in."""


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One bundle directory and what this pass would do with it."""

    root: Path
    stable_id: str
    verdict: str
    bundle_bytes: int
    detail: str = ""
    source_path: Path | None = None


@dataclasses.dataclass(frozen=True)
class Outcome:
    """One bundle after a live re-compress."""

    stable_id: str
    before_bytes: int
    after_bytes: int
    source_bytes: int
    mp3_settings: dict[str, str]
    violations: list[dict[str, Any]]
    backup_dir: Path


# ----- discovery -------------------------------------------------------------


def stem_roots_for(data_dir: Path) -> tuple[Path, ...]:
    """The directory-form stem stores of one data dir, in reader precedence."""
    state = data_dir / "state"
    return (state / "stems", state / "stems-roformer-spike")


def readonly_sqlite_uri(db_path: PurePath) -> str:
    """A read-only SQLite URI for an absolute path.

    Built with ``as_uri()``, not by pasting the path after ``file:``: a Windows
    path pasted in keeps its backslashes and drive colon, which SQLite rejects,
    and on any platform a ``?`` or ``#`` in a directory name would cut the path
    short. ``as_uri()`` gives ``file:///C:/...`` and percent-encodes both.
    """
    return f"{db_path.as_uri()}?mode=ro"


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _live_track_paths(connection: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """stable_id -> recorded ``tracks.file_path`` ("" when NULL) for each id with a live row."""
    found: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({placeholders})",
            chunk,
        )
        found.update({stable_id: file_path or "" for stable_id, file_path in rows})
    return found


def _library_paths(data_dir: Path, stable_ids: Sequence[str]) -> dict[str, str]:
    """stable_id -> the library's current file for this machine, read-only; {} if no db.

    Every track with a live library row is keyed, "" when it names no file.
    The path comes from the production resolver (``state_locations.bulk_local_audio_paths``), which
    prefers this machine's ``track_locations`` rows and applies path
    mappings, so a CloudSync machine whose ``tracks.file_path`` still names
    another machine's path finds its own copy (Codex on #6820). A row no
    location materializes for keeps its recorded ``tracks.file_path``, which
    :func:`_source_for` then reports missing.
    """
    state_db = data_dir / "state" / "state.db"
    if not state_db.is_file() or not stable_ids:
        return {}
    connection = sqlite3.connect(readonly_sqlite_uri(state_db.resolve()), uri=True)
    try:
        found = _live_track_paths(connection, list(stable_ids))
        # Read the id, never mint it: a dry run must not write the data dir,
        # and a read-only snapshot of one has no id file to create (Codex on
        # #6820). With no id no location row can be this machine's.
        machine_id = machine_identity.read_machine_id(state_db.resolve().parent.parent)
        if found and machine_id is not None and state_locations.locations_table_ready(connection):
            local = state_locations.bulk_local_audio_paths(connection, list(found), machine_id=machine_id)
            for stable_id, path in local.items():
                if path is not None:
                    found[stable_id] = str(path)
        # A live row keeps its key even with no usable path: its absence is
        # what tells _source_for to fall back to the manifest (Codex on #6820).
        return found
    finally:
        connection.close()


#: Why a stat of a recorded source path can say "not on this machine": it
#: is not there, a parent is a file, its name is too long for this
#: filesystem, or it lies under another machine's home this one may not
#: enter. Every other failure (I/O, a stale mount, a symlink loop) is this
#: machine's filesystem failing, and is raised by name (Sol on #6820).
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG, errno.EACCES, errno.EPERM})


class SourceUnreadableError(OSError):
    """A recorded source path could not be stat-ed for a reason other than its absence here."""


def _is_reachable_file(path: Path) -> bool:
    """Whether ``path`` is a regular file this machine can stat. A path it
    cannot reach (another machine's home it may not enter, a name too long
    for this filesystem) is not a source here, rather than an error that
    aborts the whole plan; any other stat failure raises
    :class:`SourceUnreadableError` naming the path."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return False
        raise SourceUnreadableError(exc.errno, f"cannot stat the source {path}: {exc.strerror}", str(path)) from exc


def _source_for(bundle: StemBundle, library_path: str | None) -> Path | None:
    """The library's current file when state.db has a row for the track, else
    the path the manifest recorded. A library row whose file is missing, or
    that names no file, gives None, not the manifest's older path: that path may be a different
    recording the track was relinked away from (Sol on #6820)."""
    if library_path is not None:
        # A live row with no path, or an unknown one, has no source here; the
        # manifest's path is never substituted for it (Codex on #6820).
        if not library_path or library_path == "unknown":
            return None
        path = Path(library_path)
        return path if _is_reachable_file(path) else None
    raw = bundle.manifest.source.path
    if raw and raw != "unknown":
        path = Path(raw)
        if _is_reachable_file(path):
            return path
    return None


def _source_duration_s(path: Path) -> float | None:
    """The source file's own duration from its headers (tinytag), or None
    when it cannot be measured."""
    from apps.shared import _tagreader

    try:
        duration = _tagreader.read(path, duration=True).duration
    except (ImportError, _tagreader.TagReadError):
        return None
    return float(duration) if duration and duration > 0 else None


def _duration_mismatch(source: Path, stems_s: float) -> str | None:
    """Why ``source`` is not the recording the stems came from, or None."""
    source_s = _source_duration_s(source)
    if source_s is None:
        return f"cannot measure {source.name}'s duration"
    tol = max(DURATION_ABS_TOL_S, DURATION_REL_TOL * stems_s)
    if abs(source_s - stems_s) > tol:
        return f"source {source_s:.3f}s vs stems {stems_s:.3f}s (tol {tol:.3f}s)"
    return None


def classify(root: Path, stable_id: str, library_path: str | None) -> Candidate:
    """What this pass would do with ``root/stable_id``. Pure read."""
    bundle_dir = root / stable_id
    size = _dir_bytes(bundle_dir)
    try:
        bundle = load_stem_bundle(stable_id, roots=[root])
    except (StemArtifactError, FileNotFoundError) as exc:
        return Candidate(root, stable_id, SKIP_INVALID, size, str(exc)[:200])
    lossless = bundle.media_type in LOSSLESS_MEDIA_TYPES
    if not lossless and bundle.media_type != MP3_MEDIA_TYPE:
        return Candidate(root, stable_id, SKIP_ALREADY_LOSSY, size, bundle.media_type)
    source = _source_for(bundle, library_path)
    if source is None:
        detail = f"library path {library_path} is not a file" if library_path else ""
        return Candidate(root, stable_id, SKIP_NO_SOURCE, size, detail)
    try:
        pol.stem_output_codec_for_source_ext(source.suffix)
    except pol.UnknownSourceFormatError:
        return Candidate(root, stable_id, SKIP_UNKNOWN_SOURCE, size, source.suffix, source)
    # STEM-58's scope for LOSSLESS bundles, unchanged by STEM-8231: only those of
    # LOSSY sources are rewritten. An MP3 bundle of any known source is judged
    # against the STEM-8231 whole-set rule instead.
    if lossless and source.suffix.lower() not in pol.LOSSY_SOURCE_EXTS:
        return Candidate(root, stable_id, SKIP_LOSSLESS_SOURCE, size, source.suffix, source)
    try:
        raw = _raw_manifest(bundle_dir)
    except (OSError, ValueError, StemArtifactError) as exc:
        return Candidate(root, stable_id, SKIP_INVALID, size, str(exc)[:200])
    verdict, detail = _verdict_for_source(bundle, raw, source) if lossless else _verdict_for_mp3_bundle(bundle, source)
    return Candidate(root, stable_id, verdict, size, detail, source)


def mp3_part_targets_kbps(source: Path, parts: Sequence[str]) -> dict[str, int] | None:
    """Each part's STEM-8231 CBR for ``source`` (the whole-set rate lifted to the
    part's floor), or None when the source reports no duration."""
    duration_s = _source_duration_s(source)
    if duration_s is None:
        return None
    rung1 = pol.mp3_whole_set_cbr_kbps(pol.effective_bitrate_kbps(source.stat().st_size, duration_s))
    return {part: max(rung1, pol.mp3_part_floor_kbps(part)) for part in parts}


def _verdict_for_mp3_bundle(bundle: StemBundle, source: Path) -> tuple[str, str]:
    """(verdict, detail) for an MP3 bundle: eligible when any part's measured
    bitrate is over its STEM-8231 target (an old per-part-rule bundle: 320 CBR,
    V0, or a rung-2 CBR), else already at policy."""
    align = bundle.alignment
    stems_s = align.frame_count / align.sample_rate
    mismatch = _duration_mismatch(source, stems_s)
    if mismatch is not None:
        return SKIP_SOURCE_DURATION, mismatch
    targets = mp3_part_targets_kbps(source, bundle.parts)
    if targets is None:
        return SKIP_SOURCE_DURATION, f"{source.name} reports no duration"
    measured = {part: Path(bundle.files[part]).stat().st_size * 8 / stems_s / 1000 for part in bundle.parts}
    over = {p: round(k) for p, k in measured.items() if k > targets[p] * AT_POLICY_KBPS_SLACK}
    if not over:
        return SKIP_AT_POLICY, ", ".join(f"{p}={targets[p]}" for p in bundle.parts)
    return ELIGIBLE, "mp3 over the whole-set rule: " + ", ".join(f"{p} {k}>{targets[p]}" for p, k in over.items())


def _verdict_for_source(bundle: StemBundle, raw: dict[str, Any], source: Path) -> tuple[str, str]:
    """(verdict, detail) for a lossless bundle of a lossy source: lossless by
    choice, the wrong recording, or eligible."""
    deliberate = deliberate_lossless_reason(raw)
    if deliberate is not None:
        return SKIP_DELIBERATE_LOSSLESS, deliberate
    align = bundle.alignment
    mismatch = _duration_mismatch(source, align.frame_count / align.sample_rate)
    if mismatch is not None:
        return SKIP_SOURCE_DURATION, mismatch
    return ELIGIBLE, bundle.media_type


def _raw_manifest(bundle_dir: Path) -> dict[str, Any]:
    raw = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StemArtifactError(f"{bundle_dir}/manifest.json is not an object")
    return raw


def _lossless_tier_presets() -> frozenset[str]:
    from apps.stems.tiers import TIERS

    return frozenset(t.preset_tag for t in TIERS.values() if t.codec == "flac")


def deliberate_lossless_reason(raw: dict[str, Any]) -> str | None:
    """Why a lossless bundle of a lossy source was made lossless ON PURPOSE, or None.

    Decided from the manifest's own provenance, never from the source alone
    (Codex P2 on #6820): an MP3 source's FLAC bundle is a migration target
    only when no writer chose FLAC for it.

    Deliberate (left alone):
      * ``preset.tag`` names a tier whose codec is FLAC (tier L, the scored
        control arm), whatever the schema;
      * an explicit lossless ``codec`` from a policy-aware writer (the local
        worker since STEM-57, the farm and relay since STEM-75): under the
        source-format policy that writer would have picked MP3, so FLAC was
        asked for (``--stem-codec flac``).

    Legacy (eligible): no ``codec`` key at all (farm schema v2 before STEM-75,
    the local worker's WAV bundles before the codec policy), or the STEM-51
    fallback, whose ``size_policy`` records ``policy_codec: mp3`` with a
    ``codec_fallback``: the policy wanted MP3 and the install had no encoder.
    """
    preset = raw.get("preset")
    tag = preset.get("tag") if isinstance(preset, dict) else None
    if tag in _lossless_tier_presets():
        return f"preset {tag} is a lossless tier"
    codec = raw.get("codec")
    if codec is None:
        return None
    policy = raw.get("size_policy")
    if isinstance(policy, dict) and policy.get("policy_codec") == "mp3" and policy.get("codec_fallback"):
        return None
    return f"manifest codec {codec!r} was chosen by its writer"


def plan(data_dir: Path, *, only: Iterable[str] = (), roots: Sequence[Path] | None = None) -> list[Candidate]:
    """Every bundle directory under this data dir's stem roots, classified."""
    wanted = set(only)
    found: list[tuple[Path, str]] = []
    for root in roots if roots is not None else stem_roots_for(data_dir):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
                continue
            if wanted and child.name not in wanted:
                continue
            found.append((root, child.name))
    library = _library_paths(data_dir, sorted({sid for _root, sid in found}))
    return [classify(root, sid, library.get(sid)) for root, sid in found]


# ----- the live-job guard ----------------------------------------------------


def assert_no_live_stems_job(data_dir: Path, *, missing_ok: bool) -> None:
    """Refuse while any stems job is queued or running (fail closed)."""
    jobs_db = data_dir / "state" / "jobs.db"
    if not jobs_db.is_file():
        if missing_ok:
            return
        raise StemsJobLiveError(
            f"{jobs_db} is missing, so a live stems job cannot be ruled out; "
            "pass --no-jobs-db-ok only for a data dir that has never run jobs"
        )
    try:
        connection = sqlite3.connect(readonly_sqlite_uri(jobs_db.resolve()), uri=True)
        try:
            placeholders = ",".join("?" for _ in LIVE_JOB_STATUSES)
            rows = connection.execute(
                f"SELECT id, kind, status FROM jobs WHERE kind LIKE 'stems.%' AND status IN ({placeholders})",
                LIVE_JOB_STATUSES,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StemsJobLiveError(f"cannot read {jobs_db}: {exc}") from exc
    if rows:
        listed = ", ".join(f"{kind} {status} ({job_id})" for job_id, kind, status in rows[:5])
        raise StemsJobLiveError(f"{len(rows)} stems job(s) live: {listed}")


# ----- one bundle --------------------------------------------------------------


def bundle_fingerprint(bundle_dir: Path) -> str:
    """SHA-256 over every file's relative path and bytes; ``"absent"`` if gone.

    Content, not mtimes: a coarse filesystem clock (1-2 s on HFS+ or FAT) can
    give a same-size rewrite the same mtime. Each part is read through
    :func:`open_read_shared`: the swap hashes outside the bundle lock, and
    on Windows a plain open would make a publisher's rename of this bundle
    fail with a sharing violation (Codex P1 on #6820). The seam refuses a
    symlinked part, whose ``OSError`` stops this bundle.
    """
    if not bundle_dir.is_dir():
        return "absent"
    digest = hashlib.sha256()
    for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(bundle_dir).as_posix().encode("utf-8") + b"\0")
        with os.fdopen(open_read_shared(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


#: What a manifest written before sources were hashed carries in ``source.sha256``.
_UNRECORDED_SHA256 = "0" * 64


def _recorded_source_sha256(raw: dict[str, Any]) -> str | None:
    """The source hash the separation recorded, or None when it recorded none."""
    source = raw.get("source")
    value = source.get("sha256") if isinstance(source, dict) else None
    if not isinstance(value, str) or value.lower() == _UNRECORDED_SHA256:
        return None
    return value.lower()


def _require_recorded_source(stable_id: str, raw: dict[str, Any], source_path: Path) -> str:
    """Hash the source and refuse it when the stems recorded another one.

    A file overwritten in place with another recording of about the same
    length passes the path and duration checks, so where the separation
    recorded ``source.sha256`` the bytes on disk must still be those (Codex
    on #6820). A manifest from before sources were hashed records none, and
    is judged by path and duration alone. Returns the hash, which the swap
    checks again so a source rewritten during the encode is caught too.
    """
    digest = sha256_file_hex(source_path)
    recorded = _recorded_source_sha256(raw)
    if recorded is not None and recorded != digest:
        raise BundleChangedError(
            f"{stable_id}: {source_path.name} is not the source these stems were separated from "
            f"(sha256 {digest[:12]} vs recorded {recorded[:12]}); left as it is"
        )
    return digest


def _decoded_frames(path: Path) -> int:
    """Frames the file really decodes to. Not ``sf.info().frames``: a low-rate
    CBR part (STEM-8231's 32 to 80 kbps) carries no Info header, so libsndfile
    estimates its length from the byte count (measured +3001 frames on 4 s
    at 48 kbps, while the decode is within LAME's two-frame pad)."""
    import soundfile as sf

    with sf.SoundFile(str(path)) as audio:
        return sum(len(block) for block in audio.blocks(blocksize=1 << 16, dtype="float32"))


def _marks(path: Path | None) -> tuple[tuple[object, ...], ...] | None:
    """``path`` and every file under it as (relative name, device, inode, ctime, size, mtime); None when gone.

    Taken under the bundle lock before the content hashes and compared under
    it again before the swap: every bundle writer swaps a whole directory in
    (new inodes) and a write in place moves the change time on POSIX.
    """
    if path is None:
        return ()
    try:
        info = path.stat()
        marks: list[tuple[object, ...]] = [("", info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size, info.st_mtime_ns)]
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                child_info = child.stat()
                marks.append(
                    (
                        child.relative_to(path).as_posix(),
                        child_info.st_dev,
                        child_info.st_ino,
                        child_info.st_ctime_ns,
                        child_info.st_size,
                        child_info.st_mtime_ns,
                    )
                )
    except (FileNotFoundError, NotADirectoryError):
        return None
    return tuple(marks)


def _swap_in(
    new_dir: Path,
    bundle_dir: Path,
    backup_dir: Path,
    read_fingerprint: str,
    before_swap: Callable[[], None] | None,
    source_unchanged: Callable[[], None],
    *,
    source_bytes_unchanged: Callable[[], None] | None = None,
    source_path: Path | None = None,
) -> int:
    """Move ``bundle_dir`` to ``backup_dir`` and ``new_dir`` into its place.

    The content hashes (the bundle's :func:`bundle_fingerprint`, then
    ``source_bytes_unchanged``, the source file's SHA-256) read every byte of
    a long mix and its stems, so they run outside the striped bundle lock,
    which a hydrate or publish of any id on that stripe would otherwise wait
    out (Codex on #6821). Under the lock first, the bundle's and the
    source's :func:`_marks` are recorded; then the hashes; then, under the
    lock again: ``before_swap`` (the live-job re-check), ``source_unchanged``
    (the library path and source signature), the marks compared, and the two
    renames, so no other writer of this id can publish between the checks
    and the move. Returns the old bundle's bytes.
    """
    with bundle_lock(bundle_dir):
        marks = (_marks(bundle_dir), _marks(source_path))
    try:
        unlocked_fingerprint = bundle_fingerprint(bundle_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:  # a writer moved it mid-hash
        raise BundleChangedError(
            f"{bundle_dir.name}: replaced while this pass hashed it; left as the other writer published it"
        ) from exc
    if unlocked_fingerprint != read_fingerprint:
        raise BundleChangedError(
            f"{bundle_dir.name}: rewritten by another writer while this pass encoded it; "
            "left as that writer published it"
        )
    # The source last, right before the locked re-check: on Windows its
    # change time is the creation time, so a same-size rewrite that puts
    # its mtime back is visible only to this hash, and a rewrite during the
    # long bundle hash must not land after it (Codex P1 on #6820).
    if source_bytes_unchanged is not None:
        source_bytes_unchanged()
    with bundle_lock(bundle_dir):
        if before_swap is not None:
            before_swap()
        source_unchanged()
        if marks[0] is None or (_marks(bundle_dir), _marks(source_path)) != marks:
            raise BundleChangedError(
                f"{bundle_dir.name}: replaced or rewritten while this pass verified it; "
                "left as the other writer published it"
            )
        # A generic swap backup a publisher killed after landing this bundle
        # left behind is an older render. Were this pass killed between its
        # renames, the read barrier would restore that backup before this
        # pass's own journal and serve it, stranding the newer bundle in
        # ``backup_dir``; so it goes before anything moves (Codex on #6820).
        if not discard_landed_swap_backup(bundle_dir):
            raise SwapBackupStuckError(swap_backup_path(bundle_dir))
        before = _dir_bytes(bundle_dir)
        _mkdir_durably(backup_dir.parent)
        if backup_dir.exists():
            raise FileExistsError(f"backup already holds {backup_dir}")
        # The new bundle's bytes reach the disk before anything moves: renames
        # flush only directory entries, so without this a power loss after the
        # journal is gone could leave the live bundle's parts empty and no
        # record of the intact backup (Codex on #6820).
        _fsync_tree(new_dir)
        journal = _write_swap_journal(bundle_dir, backup_dir)
        # Plain renames, never a copy-then-delete: each step is atomic, so a
        # process killed between them leaves one whole bundle in exactly one
        # of the two places the journal names. A --backup-dir on another
        # volume fails here (EXDEV) before anything has moved. Each rename is
        # flushed into both directories before the next step, and the journal
        # goes only once the bundle's final place is on disk, so after a power
        # loss the journal never outlives the moves it describes (Codex P1 on
        # #6821).
        # The canonical path is empty between the two renames, so readers are
        # held off for the window (Codex on #6821).
        # A flush can raise after its rename landed, so a failure anywhere in
        # the two moves is undone from what is on disk, and the journal goes
        # only once the old bundle is back (Sol on #6820).
        with readers_held_off(bundle_dir):
            try:
                _rename_durably(bundle_dir, backup_dir)
                _rename_durably(new_dir, bundle_dir)
            except BaseException:
                undo_failed_swap(bundle_dir, backup_dir, new_dir)
                _remove_journal(journal)
                raise
        # The swap has landed: readers already get the new bundle, so a
        # journal entry that will not go is not this bundle's failure. With
        # the bundle in place the entry reads as landed to every later reader
        # (recover_orphaned_swaps, clear_landed_recompress_journal), so it is
        # left for the next pass and named here (Codex on #6820).
        try:
            _remove_journal(journal)
        except OSError as exc:
            print(
                f"{bundle_dir.name}: swapped in, but its journal entry {journal} was not cleared ({exc}); "
                "the next --live pass clears it",
                file=sys.stderr,
            )
    return before


#: Where a live swap records the bundle it is replacing and the backup it moves
#: the old one to, ``<data>/state/.stem-recompress-swap/<stem root>/<stable_id>.json``,
#: so a pass killed between its two renames can be repaired by the next one.
SWAP_JOURNAL_DIR_NAME = ".stem-recompress-swap"

#: Where a pass stages one bundle's snapshot and new parts:
#: ``<stem root's parent>/.stem-recompress-work/<stem root name>/``. Beside the
#: stem root, never inside it, so a cache-budget scan or upload sweep of the
#: root never counts or queues the workspace as a bundle (Codex on PR #6821),
#: and on the same volume, so the swap stays a rename.
WORK_DIR_NAME = ".stem-recompress-work"


def work_parent(root: Path) -> Path:
    """The directory a pass makes its per-bundle work directories in, for ``root``.

    Beside the RESOLVED root, so a stem root that is a symlink to another disk
    gets its workspace on that disk and the swap stays a same-volume rename
    (Codex on #6820 and #6821).
    """
    return beside_stem_root(root) / WORK_DIR_NAME / root.name


#: Where a ``--live`` pass with no ``--backup-dir`` keeps the old bundles.
BACKUP_DIR_NAME = "stems-recompress-backup"


def default_backup_root(root: Path, stamp: str) -> Path:
    """The default backup root for bundles of ``root``: beside the RESOLVED root,
    on its volume, so moving the old bundle aside is a rename there too.

    Namespaced by the resolved root's name: two data dirs whose stem roots
    link to sibling directories on one volume share the directory beside
    them, and passes started in the same second would otherwise share one
    backup path per stable id (Codex on #6820)."""
    real = Path(os.path.realpath(root))
    return real.parent / BACKUP_DIR_NAME / real.name / stamp


def swap_journal_path(bundle_dir: Path) -> Path:
    """The journal entry for one bundle's swap, keyed on the bundle alone."""
    return (
        data_dir_of_bundle(bundle_dir)
        / "state"
        / SWAP_JOURNAL_DIR_NAME
        / bundle_dir.parent.name
        / f"{bundle_dir.name}.json"
    )


def _write_swap_journal(bundle_dir: Path, backup_dir: Path) -> Path:
    journal = swap_journal_path(bundle_dir)
    _mkdir_durably(journal.parent)
    tmp = journal.with_suffix(".json.tmp")
    payload = json.dumps({"bundle": str(bundle_dir.resolve()), "backup": str(backup_dir.resolve())}) + "\n"
    # Durable before the caller's first rename: after a power loss mid-swap
    # the journal is the only record that the live bundle sits in the backup
    # (Codex on #6820). The file is flushed to disk, then the directory entry
    # the replace wrote.
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(journal)
    _fsync_dir(journal.parent)
    return journal


def _remove_journal(journal: Path) -> None:
    """Remove a swap journal entry and flush its directory."""
    journal.unlink()
    _fsync_dir(journal.parent)


def _mkdir_durably(directory: Path) -> None:
    """``mkdir -p`` whose new entries reach the disk: each directory it
    creates is flushed into its parent, so the first swap under a stem root
    cannot lose the journal's own directories to a power loss (Codex on
    #6820). Directories that already exist are left as they are."""
    missing: list[Path] = []
    probe = directory
    while not probe.is_dir():
        missing.append(probe)
        probe = probe.parent
    for created in reversed(missing):
        created.mkdir(exist_ok=True)
        _fsync_dir(created.parent)


def orphaned_swaps(data_dir: Path) -> list[Path]:
    """Journal entries a pass killed mid-swap left behind (read-only)."""
    journal_root = data_dir / "state" / SWAP_JOURNAL_DIR_NAME
    if not journal_root.is_dir():
        return []
    return sorted(journal_root.glob("*/*.json"))


def recover_orphaned_swaps(data_dir: Path) -> list[str]:
    """Repair every swap a killed pass left half done, before anything is planned.

    Under the bundle lock, for each journal entry: no bundle in place but the
    backup whole means the pass died between its two renames, so the backup
    is the only good copy and goes back (Codex on #6820); a bundle in place
    means the swap landed, or never started, and the backup (if any) is the
    operator's kept copy, so only the entry goes. Neither on disk is reported
    and the entry is kept, never guessed at. Returns one line per entry.
    """
    stem_roots = {root.name: root for root in stem_roots_for(data_dir)}
    lines: list[str] = []
    for journal in orphaned_swaps(data_dir):
        root = stem_roots.get(journal.parent.name)
        if root is None:
            raise BundleChangedError(f"{journal}: names no stem root of {data_dir}; left for the operator")
        bundle_dir = root / journal.stem
        # Read the entry under the lock (Codex on #6820): a pass still mid-swap
        # holds it, and by the time it lets go it has removed its own entry.
        # The read barrier too, in the order a live swap takes them, so a deck
        # read restoring the same entry (restore_orphaned_recompress_swap)
        # waits for this one rather than racing it (Codex on #6820).
        with bundle_lock(bundle_dir), readers_held_off(bundle_dir):
            if not journal.exists():
                lines.append(f"{bundle_dir.name}: the pass that wrote this entry finished it")
                continue
            recorded = json.loads(journal.read_text(encoding="utf-8"))
            if Path(recorded["bundle"]) != bundle_dir.resolve():
                raise BundleChangedError(
                    f"{journal}: records {recorded['bundle']}, not {bundle_dir}; left for the operator"
                )
            backup_dir = Path(recorded["backup"])
            if bundle_dir.exists():
                _remove_journal(journal)
                lines.append(f"{bundle_dir.name}: swap had landed; nothing to restore")
            elif backup_dir.is_dir():
                _rename_durably(backup_dir, bundle_dir)
                _remove_journal(journal)
                lines.append(f"{bundle_dir.name}: restored the old bundle from {backup_dir}")
            else:
                raise BundleChangedError(
                    f"{bundle_dir.name}: neither the bundle nor its backup {backup_dir} is on disk; "
                    f"journal {journal} left for the operator"
                )
    return lines


def restore_orphaned_recompress_swap(bundle_dir: Path, data_dir: Path) -> bool:
    """Put back the bundle a recompression killed between its two renames left in its backup.

    Called by a deck read while it holds the bundle lock and then
    :func:`readers_held_off`, in a swap's own order. A live pass holds both
    across its renames and removes its
    journal entry inside it when it rolls back, so under it a journal entry
    with no bundle in place can only be a dead pass's, and its backup is the
    only good copy. Without this the track read as missing until the next
    ``recompress --live`` ran :func:`recover_orphaned_swaps` (Codex on #6820).
    A bundle in place is left alone: that swap landed, or never started, and
    the next pass clears the entry. An entry naming another bundle, or a
    backup that is not on disk, is left for the operator. Returns whether a
    bundle was put back.
    """
    bundle_dir = Path(bundle_dir)
    # The bundle being there is the common case: one stat, nothing read.
    if bundle_dir.exists():
        return False
    try:
        if data_dir_of_bundle(bundle_dir).resolve() != Path(data_dir).resolve():
            return False
    except ValueError:
        return False
    journal = swap_journal_path(bundle_dir)
    if not journal.is_file():
        return False
    recorded = json.loads(journal.read_text(encoding="utf-8"))
    backup_dir = Path(recorded["backup"])
    if Path(recorded["bundle"]) != bundle_dir.resolve() or not backup_dir.is_dir():
        return False
    _rename_durably(backup_dir, bundle_dir)
    _remove_journal(journal)
    return True


def clear_landed_recompress_journal(bundle_dir: Path) -> None:
    """Remove the journal entry of a recompression that landed but died before clearing it.

    The caller holds ``bundle_dir``'s bundle lock and is about to remove the
    bundle (cache eviction). A pass killed after its second rename and
    before :func:`_remove_journal` leaves the new bundle, the old backup and
    the entry; once eviction removes the bundle, a deck read would take the
    entry for an interrupted swap and put the old bundle back
    (:func:`restore_orphaned_recompress_swap`), undoing the eviction and the
    recompression (Codex on #6820). With the bundle still in place the entry
    proves only that the swap landed, so it goes, as
    :func:`recover_orphaned_swaps` would remove it; the backup is the
    operator's kept copy and stays.
    """
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.is_dir():
        return
    try:
        journal = swap_journal_path(bundle_dir)
    except ValueError:
        return
    if journal.is_file():
        _remove_journal(journal)


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


def _pin_generation(bundle_dir: Path, snapshot: Path) -> bool:
    """Hard-link every file of ``bundle_dir`` into ``snapshot``; False if the volume refuses.

    Held under the bundle lock, and cheap there: one link per file, no stem
    bytes read. Every writer of a bundle stages a new directory and renames
    it into place (:func:`apps.stems.bundle_lock.replace_bundle_dir`), and
    none rewrites a part in place, so the linked inodes keep this generation's
    bytes after another writer replaces or evicts the bundle. A volume
    without hard links (FAT, exFAT) leaves nothing behind and returns False.
    """
    snapshot.mkdir(parents=True)

    def refuse(exc: OSError) -> None:
        raise exc

    try:
        # A walk error (the bundle gone) raises, as a copy would have.
        for current, dirs, files in os.walk(bundle_dir, onerror=refuse):
            rel = Path(current).relative_to(bundle_dir)
            for name in dirs:
                (snapshot / rel / name).mkdir()
            for name in files:
                os.link(Path(current) / name, snapshot / rel / name)
    except (FileNotFoundError, NotADirectoryError):
        raise
    except OSError:
        shutil.rmtree(snapshot)
        return False
    return True


def _copy_unlocked(bundle_dir: Path, snapshot: Path) -> None:
    """Copy ``bundle_dir`` into ``snapshot`` without the bundle lock.

    For a volume that refuses hard links. A writer that replaces the bundle
    mid-copy leaves a copy mixing two generations, whose fingerprint matches
    neither, so the swap's re-hash of the live bundle refuses it
    (:func:`_swap_in`); one that removes it mid-copy stops this bundle here.
    """
    try:
        shutil.copytree(bundle_dir, snapshot)
    except (FileNotFoundError, NotADirectoryError, shutil.Error) as exc:
        raise BundleChangedError(
            f"{bundle_dir.name}: changed or unreadable while this pass copied it ({exc}); left as it is"
        ) from exc


def recompress_bundle(
    candidate: Candidate,
    backup_root: Path,
    *,
    before_swap: Callable[[], None] | None = None,
) -> Outcome:
    """Re-encode one eligible bundle to MP3, verify it, then swap it in.

    The old directory moves to ``backup_root/<root name>/<stable_id>``. On any
    failure before the swap nothing in the stem root has changed.

    ``before_swap`` runs after the new bundle is verified and immediately
    before the old one is moved; if it raises, nothing is swapped. The CLI
    passes the live-stems-job check here, so a job that appeared while this
    bundle was encoding stops its swap.
    """
    if candidate.verdict != ELIGIBLE or candidate.source_path is None:
        raise ValueError(f"{candidate.stable_id} is not eligible: {candidate.verdict}")
    root, stable_id = candidate.root, candidate.stable_id
    bundle_dir = root / stable_id
    work_root = work_parent(root)
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".recompress-{stable_id}-", dir=str(work_root)))
    try:
        if work.stat().st_dev != root.stat().st_dev:
            raise WorkVolumeError(
                f"{stable_id}: work directory {work} is not on the stem root's volume ({root}); "
                "the swap is a rename, so it needs the same volume"
            )
        # Snapshot the bundle into the work dir and fingerprint the snapshot,
        # so the encode below (which runs without the lock) reads bytes no
        # other writer can rewrite mid-read, and the swap can tell whether
        # the live bundle changed since. Under the lock the generation is
        # only PINNED, by hard links: a copy of every stem byte held there
        # would make every hydrate or publish of an id on that stripe wait it
        # out (Codex on #6821).
        snapshot_root = work / "snapshot"
        snapshot = snapshot_root / stable_id
        with bundle_lock(bundle_dir):
            pinned = _pin_generation(bundle_dir, snapshot)
            library_path = _library_paths(data_dir_of_bundle(bundle_dir), [stable_id]).get(stable_id)
        if not pinned:
            _copy_unlocked(bundle_dir, snapshot)
        # The plan is stale by now: another writer may have replaced the bundle,
        # or the library relinked the track, since (Codex on #6820). Decide
        # again from the locked copy and the library's current path.
        source_path = _require_still_eligible(snapshot_root, stable_id, candidate, library_path)
        read_fingerprint = bundle_fingerprint(snapshot)
        bundle = load_stem_bundle(stable_id, roots=[snapshot_root])
        old_raw = _raw_manifest(snapshot)
        align = bundle.alignment
        source_digest = _require_recorded_source(stable_id, old_raw, source_path)
        source_signature = _source_signature(source_path)
        source_bytes = source_signature[0]
        # The source's own duration, not the stems': classify() refused one
        # that disagrees beyond the drift tolerance, and this is the length
        # the source's bytes actually cover (Sol on #6820).
        duration_s = _source_duration_s(source_path)
        if duration_s is None:
            raise BundleChangedError(f"{stable_id}: {source_path} no longer reports a duration; left as it is")
        source_kbps = pol.effective_bitrate_kbps(source_bytes, duration_s)
        rung1 = pol.mp3_lame_settings_for_source_kbps(source_kbps)

        new_dir = work / stable_id
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
        (new_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        reread = load_stem_bundle(stable_id, roots=[work])
        if reread.media_type != "audio/mpeg" or reread.alignment != dataclasses.replace(align, frame_count=frame_count):
            raise StemArtifactError(f"{stable_id}: re-read bundle is {reread.media_type} {reread.alignment}")

        def source_unchanged() -> None:
            # The encode ran without the lock, so state.db may have relinked
            # the track, or the source file been rewritten, since the snapshot
            # (Codex on #6821). Re-read both under the swap's lock: the MP3
            # sizes were chosen against the source as it was. The content
            # hash catches a rewrite the size and mtime miss (Codex on #6820).
            library_now = _library_paths(data_dir_of_bundle(bundle_dir), [stable_id]).get(stable_id)
            _require_still_eligible(snapshot_root, stable_id, candidate, library_now)
            if not source_path.is_file() or _source_signature(source_path) != source_signature:
                raise BundleChangedError(
                    f"{stable_id}: the source {source_path} changed while this pass encoded it; left as it is"
                )

        def source_bytes_unchanged() -> None:
            # Outside the lock: a whole mix is read, and the swap's re-check
            # of the source's marks under the lock catches a write after it.
            if sha256_file_hex(source_path) != source_digest:
                raise BundleChangedError(
                    f"{stable_id}: the source {source_path} changed while this pass encoded it; left as it is"
                )

        backup_dir = backup_root / root.name / stable_id
        before = _swap_in(
            new_dir,
            bundle_dir,
            backup_dir,
            read_fingerprint,
            before_swap,
            source_unchanged,
            source_bytes_unchanged=source_bytes_unchanged,
            source_path=source_path,
        )
        return Outcome(
            stable_id=stable_id,
            before_bytes=before,
            after_bytes=_dir_bytes(bundle_dir),
            source_bytes=source_bytes,
            mp3_settings=settings,
            violations=violations,
            backup_dir=backup_dir,
        )
    finally:
        _remove_work(work, stable_id, sys.exc_info()[1])


def _remove_work(work: Path, stable_id: str, failure: BaseException | None) -> None:
    """Remove the work directory, or raise naming it and the failure that led here."""
    try:
        shutil.rmtree(work)
    except OSError as cleanup_error:
        during = f" (while handling: {failure})" if failure is not None else ""
        raise WorkspaceCleanupError(
            f"{stable_id}: could not remove the work directory {work}: {cleanup_error}{during}"
        ) from cleanup_error


# ----- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the pass from the command line (:mod:`apps.stems.recompress_cli`)."""
    from apps.stems.recompress_cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
