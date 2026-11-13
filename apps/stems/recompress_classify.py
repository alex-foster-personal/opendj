"""Plan side of the stem recompress pass: policy constants, candidates and classification (split from apps/stems/recompress.py)."""

from __future__ import annotations

import dataclasses
import errno
import json
import sqlite3
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePath
from typing import Any

from apps.shared.state import locations as state_locations
from apps.shared.state import machine_identity
from apps.stems import stem_size_policy as pol
from apps.stems.artifacts import (
    StemArtifactError,
    StemBundle,
    load_stem_bundle,
)

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
AT_POLICY_KBPS_SLACK = pol.AT_POLICY_KBPS_SLACK
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
    from apps.stems.artifacts import PRODUCTION_ROFORMER_STEMS_DIR

    state = data_dir / "state"
    return (state / "stems", state / PRODUCTION_ROFORMER_STEMS_DIR.name)


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


def library_paths(data_dir: Path, stable_ids: Sequence[str]) -> dict[str, str]:
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
    library = library_paths(data_dir, sorted({sid for _root, sid in found}))
    return [classify(root, sid, library.get(sid)) for root, sid in found]
