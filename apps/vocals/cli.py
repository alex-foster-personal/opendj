"""``python -m apps.vocals`` - vocal-detection demucs gap-fill trickle CLI.

Fills the PVDI gap (61% of the library lacks rekordbox 7 vocal analysis,
SPIKE-B1) by trickling tracks through the standalone demucs worker
(``scripts/vocal_region_worker.py``, PEP 723, run via ``uv run`` so
torch/demucs never enter the repo venv) and writing
``data/state/vocal-cache/{stable_id}.json`` per the apps.vocals.cache
contract. The webui /anlz endpoint consults that cache when PVDI is
absent (status "demucs").

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 scan: coverage report pvdi / cached-demucs / missing-file /
    missing-analysis / todo,
    with todo ETA at the SPIKE-B2 1.36x-realtime CPU rate; --playlist
    filter; --json machine-readable (agent-native parity).
    [if] a track has PVDI in its .2EX [then] it counts as pvdi
    [if] an audio track has no local ANLZ .DAT [then ⛔️] it enters the
      trickle queue (Demucs output would be unservable through /anlz)
    [if] counts do not sum to total [then ⛔️]
    [if] --playlist names an unknown playlist [then ⛔️] explicit error
  ✔︎ ✅ 🎯 trickle: process todo queue N tracks (--limit, default 5), ordered
    by (playlist with most on-disk members) desc then length asc; requires
    explicit --dry-run or --live (no silent default);
    idempotent via cache presence; missing files skipped EXPLICITLY with
    a log line; per-track wall time + running ETA on stdout.
    [if] neither --dry-run nor --live [then ⛔️] argparse rejects
    [if] run twice --live [then] second run recomputes nothing
    [if] a queued file vanished before its turn [then] "[SKIP]" line, no crash
    [if] --dry-run and --live both passed [then ⛔️] argparse rejects them
    [if] --limit is negative [then ⛔️] argparse rejects it before queue slicing
    [if] audio mtime changes during a worker run [then ⛔️] no cache write
  ✔︎ ✅ 🎯 one: analyse a single --stable-id immediately (debug path), --force
    recomputes over a valid cache entry.
    [if] cache valid and no --force [then] no worker run, prints cached
    [if] no local ANLZ .DAT exists [then ⛔️] reject before cache or worker
  ✔︎ ✅ from-stems: CPU-only backfill of vocal-cache from existing
    ``data/state/stems/<id>/`` bundles (no demucs). Requires explicit
    --dry-run or --live (no silent default). Skips valid cache unless --force.
    [if] neither --dry-run nor --live [then ⛔️] argparse rejects
    [if] stems present and cache missing [then] --live writes regions
    [if] cache valid and no --force [then] skip
  ✔︎ ✅ --data-dir overrides the repo-default data/ root everywhere
    (worktrees pass the primary checkout's data dir explicitly).
  ✔︎ ✅ Windows portability: FolderPath/.2EX resolution goes through the
    shared apps.shared.platform_paths.resolve_asset_path resolver (never
    a local /PIONEER/-only shim), so an unmapped foreign-absolute path
    (e.g. a Mac path read on Windows) is an explicit missing state, never
    a fabricated Path; the worker subprocess honours MDT_VOCAL_WORKER_PYTHON
    (bench-env interpreter override) and MDT_VOCAL_WORKER_DEVICE.
    [if] a FolderPath is foreign-absolute with no path-map entry [then]
    audio_on_disk stays False (classification unchanged, falls to missing)
    [if] MDT_VOCAL_WORKER_PYTHON is set [then] run_worker invokes it
    directly, never ``uv run``
  ✔︎ ✅ Worker preflight (INSTALL-27): run_worker refuses BEFORE launching
    the worker when the launch could only fail, so the operator reads a
    recovery step instead of "vocal_region_worker failed (exit N)".
    [if] MDT_VOCAL_WORKER_SCRIPT names a missing or unreadable file [then ⛔️]
    reinstall-from-a-complete-dmg guidance, no worker spawned
    [if] input is not .wav and no ffmpeg is reachable (MDT_FFMPEG or PATH,
    the worker's own lookup) [then ⛔️] a decoder message naming the file
    and the remedies, no worker spawned
  → per-night budget (--max-minutes) - PARITY-TODO follow-up, not here.

Exact command lines:
  python -m apps.vocals scan --data-dir /Users/user/code/music-dj-tools/data
  python -m apps.vocals trickle --limit 1 --live \\
      --data-dir /Users/user/code/music-dj-tools/data
  python -m apps.vocals from-stems --dry-run
  python -m apps.vocals from-stems --live

-Claude
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.paths import DATA_DIR
from apps.shared.platform_paths import (
    MappedPath,
    PathMap,
    load_path_map,
    resolve_asset_path,
)
from apps.shared.process_groups import WorkerCleanupError as _WorkerCleanupError
from apps.shared.process_groups import (
    group_has_live_member,
    signal_group,
    wait_group_gone,
)
from apps.shared.state import locations as state_locations
from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems

# ----- CFG ---------------------------------------------------------------------
DEMUCS_REALTIME_FACTOR: float = (
    1.36  # SPIKE-B2 measured CPU rate (honest band 0.8-1.4x)
)
DEFAULT_TRICKLE_LIMIT: int = 5
WORKER_TIMEOUT_S: float = 30 * 60
LOCK_LEASE_S: float = 30
LOCK_HEARTBEAT_S: float = 5
WORKER_TERMINATE_GRACE_S: float = 2
_WINDOWS: bool = os.name == "nt"
WORKER_SCRIPT: Path = (
    Path(__file__).resolve().parents[2] / "scripts" / "vocal_region_worker.py"
)
PACKAGED_WORKER_SCRIPT_ENV: str = "MDT_VOCAL_WORKER_SCRIPT"
PACKAGED_WORKER_PYTHON_ENV: str = "MDT_VOCAL_WORKER_PYTHON"
# Same variable scripts/vocal_region_worker.py::_read_via_ffmpeg reads.
FFMPEG_OVERRIDE_ENV: str = "MDT_FFMPEG"
WORKER_UNAVAILABLE_MESSAGE: str = (
    "Vocal separation is unavailable because the bundled audio runtime "
    "could not be started. Reinstall Open DJ from a complete dmg and "
    "try again."
)
_SQL_CHUNK: int = 500  # keep IN (...) under SQLite's var cap

CATEGORY_PVDI = "pvdi"
CATEGORY_CACHED = "cached_demucs"
CATEGORY_MISSING = "missing_file"
CATEGORY_MISSING_ANALYSIS = "missing_analysis"
CATEGORY_TODO = "todo"


# Raised when the worker tree may still exist, so its claim must remain held.
# Shared with the engine reaper rather than redeclared: both terminate a
# worker group, both must be catchable by the same `except`, and a second
# same-named class would silently escape the other's handler.
WorkerCleanupError = _WorkerCleanupError


@dataclass(frozen=True)
class Ctx:
    """Resolved data locations for one invocation (--data-dir aware)."""

    data_dir: Path

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state" / "state.db"

    @property
    def master_db(self) -> Path:
        return self.data_dir / "master.plain.db"

    @property
    def cache_dir(self) -> Path:
        return vcache.cache_dir(self.data_dir)


@dataclass
class VocalTrack:
    """One rekordbox-mapped track with everything scan/trickle needs."""

    stable_id: str
    vendor_id: str
    title: str
    length_s: int
    folder_path: str | None
    analysis_data_path: str | None
    audio_path: Path | None
    audio_on_disk: bool
    category: str = ""


@dataclass(frozen=True)
class TrackClaim:
    """One immutable lock owner with a renewable, process-independent lease."""

    path: Path
    owner_token: str
    lease_s: float
    stop_heartbeat: threading.Event
    heartbeat_thread: threading.Thread
    heartbeat_errors: list[BaseException]


# ----- db plumbing ---------------------------------------------------------------


def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"{label} missing on disk: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _resolve(path: str, *, path_map: PathMap) -> Path | None:
    """Resolve a state.db/rekordbox path via the shared platform resolver.

    ``None`` is the load-bearing "unmapped" state (a foreign-absolute path,
    e.g. a Mac FolderPath read on Windows with no path-map entry, or a
    streaming URI) -- never a fabricated ``Path`` that happens not to
    exist. Windows portability plan section 3.1."""
    mapped: MappedPath = resolve_asset_path(path, path_map=path_map)
    return mapped.resolved


def _resolve_share_path(path: str) -> Path:
    """Resolve a local path, rejecting unsafe or unmapped source paths."""
    mapped = resolve_asset_path(path)
    if mapped.resolved is None:
        raise ValueError(mapped.reason)
    return mapped.resolved


def _chunks(seq: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ----- track loading -------------------------------------------------------------


def load_tracks(ctx: Ctx, playlist: str | None) -> list[VocalTrack]:
    """All rekordbox-mapped tracks (optionally one playlist's members)."""
    path_map = load_path_map(ctx.data_dir)
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        if playlist is not None:
            pl_rows = state.execute(
                "SELECT playlist_id, name FROM playlists "
                "WHERE name = ? AND deleted_at IS NULL",
                (playlist,),
            ).fetchall()
            if not pl_rows:
                names = [
                    r[0]
                    for r in state.execute(
                        "SELECT DISTINCT name FROM playlists "
                        "WHERE deleted_at IS NULL ORDER BY name"
                    ).fetchall()
                ]
                from apps.vocals.errors import UnknownPlaylistError

                raise UnknownPlaylistError(name=playlist, known=names)
            pl_ids = [r[0] for r in pl_rows]
            member_ids: set[str] = set()
            for chunk in _chunks(pl_ids, _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                member_ids.update(
                    r[0]
                    for r in state.execute(
                        "SELECT DISTINCT stable_id FROM playlist_memberships "
                        f"WHERE playlist_id IN ({marks}) AND deleted_at IS NULL",
                        chunk,
                    ).fetchall()
                )
        # ADR 08 point 5 / round 2 finding 4b: a tombstoned track must not
        # enter the vocals trickle queue.
        mappings = state.execute(
            "SELECT t.stable_id, v.vendor_id, COALESCE(t.title, '') "
            "FROM tracks t "
            "JOIN track_vendor_ids v "
            "  ON v.stable_id = t.stable_id AND v.vendor = 'rekordbox' "
            "WHERE t.deleted_at IS NULL"
        ).fetchall()
    finally:
        state.close()

    if playlist is not None:
        mappings = [m for m in mappings if m[0] in member_ids]

    by_vendor: dict[str, tuple[str, str]] = {
        str(vid): (sid, title) for sid, vid, title in mappings
    }
    master = _open_ro(ctx.master_db, "MASTER_DB")
    rows: list[tuple[str, str, int, str | None, str | None]] = []
    try:
        for chunk in _chunks(list(by_vendor), _SQL_CHUNK):
            marks = ",".join("?" * len(chunk))
            rows.extend(
                master.execute(
                    "SELECT ID, Title, Length, FolderPath, AnalysisDataPath "
                    "FROM djmdContent "
                    f"WHERE ID IN ({marks}) AND +rb_local_deleted = 0",
                    chunk,
                ).fetchall()
            )
    finally:
        master.close()

    tracks: list[VocalTrack] = []
    for vendor_id, title, length_s, folder_path, adp in rows:
        stable_id, state_title = by_vendor[str(vendor_id)]
        audio: Path | None = None
        on_disk = False
        if folder_path:
            audio = _resolve(str(folder_path), path_map=path_map)
            on_disk = audio is not None and audio.is_file()
        tracks.append(
            VocalTrack(
                stable_id=stable_id,
                vendor_id=str(vendor_id),
                title=str(title or state_title),
                length_s=int(length_s) if length_s is not None else 0,
                folder_path=str(folder_path) if folder_path else None,
                analysis_data_path=str(adp) if adp else None,
                audio_path=audio,
                audio_on_disk=on_disk,
            )
        )
    tracks.sort(key=lambda t: t.stable_id)
    return tracks


def load_state_tracks(ctx: Ctx) -> list[VocalTrack]:
    """Rekordbox-mapped tracks with their local audio, read from state.db only.

    from-stems needs only an id and an audio file, so it must not depend on
    the decrypted rekordbox copy (``master.plain.db``): the packaged app has
    no such file, and requiring it failed every library refresh. Same track
    set as :func:`load_tracks` (live, rekordbox-mapped), with audio resolved
    the way the library listing resolves it.
    """
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        rows = state.execute(
            "SELECT t.stable_id, COALESCE(t.title, '') "
            "FROM tracks t "
            "JOIN track_vendor_ids v "
            "  ON v.stable_id = t.stable_id AND v.vendor = 'rekordbox' "
            "WHERE t.deleted_at IS NULL"
        ).fetchall()
        ids = [str(sid) for sid, _title in rows]
        audio = state_locations.bulk_local_audio_paths(state, ids)
    finally:
        state.close()
    tracks = [
        VocalTrack(
            stable_id=str(sid),
            vendor_id="",
            title=str(title),
            length_s=0,
            folder_path=None,
            analysis_data_path=None,
            audio_path=audio.get(str(sid)),
            audio_on_disk=audio.get(str(sid)) is not None,
        )
        for sid, title in rows
    ]
    tracks.sort(key=lambda t: t.stable_id)
    return tracks


# ----- classification --------------------------------------------------------------


def pvdi_present(path_2ex: Path) -> bool:
    """Cheap PVDI probe: seek-walk the PMAI section headers only (a scan
    over ~10k .2EX files must not read ~20 GB of full payloads)."""
    with path_2ex.open("rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"PMAI":
            raise ValueError(f"not an ANLZ container: {path_2ex}")
        head_len, file_len = struct.unpack(">II", head[4:12])
        off = head_len
        while off + 12 <= file_len:
            fh.seek(off)
            section = fh.read(12)
            if len(section) < 12:
                break
            if section[:4] == b"PVDI":
                return True
            total_len = struct.unpack(">I", section[8:12])[0]
            if total_len <= 0:
                raise ValueError(f"corrupt section length in {path_2ex}")
            off += total_len
    return False


def _anlz_data_file(track: VocalTrack, path_map: PathMap) -> Path | None:
    """Return the local ANLZ .DAT required before ``/anlz`` can serve a track."""
    if track.analysis_data_path is None:
        return None
    resolved = _resolve(track.analysis_data_path, path_map=path_map)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


def classify(ctx: Ctx, tracks: list[VocalTrack]) -> None:
    """Assign each track exactly one category (pvdi wins over everything:
    an analyzed track is covered even if its audio has since moved)."""
    path_map = load_path_map(ctx.data_dir)
    for tr in tracks:
        anlz_data = _anlz_data_file(tr, path_map)
        if anlz_data is None:
            if tr.audio_on_disk:
                tr.category = CATEGORY_MISSING_ANALYSIS
            else:
                tr.category = CATEGORY_MISSING
            continue

        twoex = anlz_data.with_suffix(".2EX")
        if twoex.is_file() and pvdi_present(twoex):
            tr.category = CATEGORY_PVDI
            continue
        entry = vcache.load_valid_entry(
            vcache.cache_path(ctx.data_dir, tr.stable_id), tr.audio_path
        )
        if entry is not None:
            tr.category = CATEGORY_CACHED
            continue
        if not tr.audio_on_disk:
            tr.category = CATEGORY_MISSING
            continue
        tr.category = CATEGORY_TODO


# ----- trickle queue ordering -------------------------------------------------------


def best_playlist_rank(
    ctx: Ctx, tracks: list[VocalTrack]
) -> dict[str, tuple[int, str]]:
    """stable_id -> (largest on-disk member count over its playlists, that
    playlist's name). Tracks in no playlist rank (0, '')."""
    on_disk_ids = {t.stable_id for t in tracks if t.audio_on_disk}
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        rows = state.execute(
            "SELECT m.playlist_id, p.name, m.stable_id "
            "FROM playlist_memberships m "
            "JOIN playlists p ON p.playlist_id = m.playlist_id "
            "WHERE m.deleted_at IS NULL AND p.deleted_at IS NULL"
        ).fetchall()
    finally:
        state.close()

    members: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for pl_id, name, sid in rows:
        members.setdefault(pl_id, []).append(sid)
        names[pl_id] = name
    counts = {
        pl_id: sum(1 for sid in sids if sid in on_disk_ids)
        for pl_id, sids in members.items()
    }

    rank: dict[str, tuple[int, str]] = {}
    for pl_id, sids in members.items():
        for sid in sids:
            if counts[pl_id] > rank.get(sid, (0, ""))[0]:
                rank[sid] = (counts[pl_id], names[pl_id])
    return rank


def order_todo(
    todo: list[VocalTrack], rank: dict[str, tuple[int, str]]
) -> list[VocalTrack]:
    """Priority: playlists with most on-disk members first, then shortest
    first (fast wins early), then stable_id for determinism."""
    return sorted(
        todo,
        key=lambda t: (-rank.get(t.stable_id, (0, ""))[0], t.length_s, t.stable_id),
    )


# ----- worker invocation --------------------------------------------------------------


def _worker_command(audio_path: Path) -> list[str]:
    device = os.environ.get("MDT_VOCAL_WORKER_DEVICE", "auto")
    worker_script = Path(os.environ.get(PACKAGED_WORKER_SCRIPT_ENV, WORKER_SCRIPT))
    override_python = os.environ.get(PACKAGED_WORKER_PYTHON_ENV)
    if override_python:
        return [
            override_python,
            str(worker_script),
            "--device",
            device,
            str(audio_path),
        ]
    return [
        "uv",
        "run",
        "--no-sync",
        "--script",
        str(worker_script),
        "--device",
        device,
        str(audio_path),
    ]


def _terminate_worker_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate and reap the complete worker process group before returning."""
    if _WINDOWS:
        try:
            taskkill = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise WorkerCleanupError(
                f"taskkill unavailable; vocal worker tree {proc.pid} is unverified"
            ) from exc
        if taskkill.returncode != 0:
            raise WorkerCleanupError(
                f"taskkill failed for vocal worker tree {proc.pid} "
                f"(exit {taskkill.returncode}): {taskkill.stderr.strip()}"
            )
        try:
            proc.wait(timeout=WORKER_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired as exc:
            raise WorkerCleanupError(
                f"taskkill did not reap vocal worker tree {proc.pid}: "
                f"{taskkill.stderr.strip()}"
            ) from exc
        return

    # Every rung below asks "is anything still RUNNING in the group", never
    # "does the pgid still exist". They are different questions on macOS: an
    # all-zombie group (the worker died, nobody has waited on it yet) keeps its
    # pgid and answers killpg with EPERM. Reading that as "alive" made this
    # function burn the full WORKER_TERMINATE_GRACE_S against a corpse and then
    # raise, falsely reporting a worker that survived SIGKILL and stranding its
    # claim for manual recovery. See apps/shared/process_groups.
    process_group_id = proc.pid
    try:
        if signal_group(process_group_id, signal.SIGTERM) is not None:
            proc.wait()
            return
        try:
            proc.wait(timeout=WORKER_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        if group_has_live_member(process_group_id):
            signal_group(process_group_id, signal.SIGKILL)
        proc.wait()
        if not wait_group_gone(process_group_id, WORKER_TERMINATE_GRACE_S):
            raise WorkerCleanupError(
                f"vocal worker process group {process_group_id} survived SIGKILL"
            )
    except WorkerCleanupError:
        raise
    except BaseException as exc:
        raise WorkerCleanupError(
            f"vocal worker process group {process_group_id} cleanup is unverified"
        ) from exc


class WorkerUnavailableError(RuntimeError):
    """The worker cannot run this input here; raised before any launch."""


def _ffmpeg_reachable(environ: Mapping[str, str]) -> bool:
    """Mirror the worker's decoder lookup: MDT_FFMPEG's dir first, then PATH."""
    search = environ.get("PATH", os.defpath)
    override = environ.get(FFMPEG_OVERRIDE_ENV)
    if override:
        search = os.pathsep.join([str(Path(override).parent), search])
    return shutil.which("ffmpeg", path=search) is not None


def preflight_worker(audio_path: Path, environ: Mapping[str, str]) -> None:
    """Refuse a worker launch that can only fail, with a recovery message.

    Two launches are knowable failures before the worker imports torch:
    a packaged worker script that is not there (an incomplete install),
    and a non-WAV input with no ffmpeg to decode it (the worker decodes
    only ``.wav`` itself, via soundfile). ``environ`` is the environment
    the worker would inherit.
    """
    packaged_script = environ.get(PACKAGED_WORKER_SCRIPT_ENV)
    if packaged_script:
        script = Path(packaged_script)
        if not (script.is_file() and os.access(script, os.R_OK)):
            raise WorkerUnavailableError(WORKER_UNAVAILABLE_MESSAGE)
    if audio_path.suffix.lower() != ".wav" and not _ffmpeg_reachable(environ):
        raise WorkerUnavailableError(
            f"Vocal separation cannot read {audio_path.name}: only WAV files "
            "can be decoded without ffmpeg, and ffmpeg was not found. "
            f"Install ffmpeg, set {FFMPEG_OVERRIDE_ENV} to its path, or "
            "convert the track to WAV, then try again."
        )


def run_worker(audio_path: Path, timeout_s: float = WORKER_TIMEOUT_S) -> dict[str, Any]:
    """Run the demucs worker with a deadline and parse its stdout JSON."""
    if timeout_s <= 0:
        raise ValueError(f"worker timeout must be > 0 seconds, got {timeout_s}")
    preflight_worker(audio_path, os.environ)
    process_kwargs: dict[str, Any] = {}
    if _WINDOWS:
        process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            _worker_command(audio_path),
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            **process_kwargs,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise WorkerUnavailableError(WORKER_UNAVAILABLE_MESSAGE) from exc
    try:
        stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _terminate_worker_tree(proc)
        proc.communicate()
        raise RuntimeError(
            f"vocal_region_worker timed out after {timeout_s}s for {audio_path}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"vocal_region_worker failed (exit {proc.returncode}) for {audio_path}"
        )
    return json.loads(stdout)


@contextmanager
def _claim_record_guard(lock: Path) -> Iterator[None]:
    """Serialize short claim-record transitions across processes."""
    mutex_path = lock.with_name(f"{lock.name}.mutex")
    mutex_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(mutex_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _read_claim_record(lock: Path) -> dict[str, Any] | None:
    if not lock.exists():
        return None
    try:
        record = json.loads(lock.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"invalid vocals claim record: {lock}") from exc
    if not isinstance(record, dict):
        raise RuntimeError(f"invalid vocals claim record: {lock}")
    if (
        not isinstance(record.get("owner_token"), str)
        or not record["owner_token"]
        or not isinstance(record.get("pid"), int)
        or isinstance(record["pid"], bool)
        or not isinstance(record.get("heartbeat_at"), (int, float))
        or isinstance(record["heartbeat_at"], bool)
        or not math.isfinite(record["heartbeat_at"])
        or not isinstance(record.get("lease_s"), (int, float))
        or isinstance(record["lease_s"], bool)
        or not math.isfinite(record["lease_s"])
        or record["lease_s"] <= 0
        or not isinstance(record.get("released"), bool)
        or not isinstance(record.get("manual_recovery_required", False), bool)
    ):
        raise RuntimeError(f"invalid vocals claim record: {lock}")
    return record


def _write_claim_record(lock: Path, record: dict[str, Any]) -> None:
    descriptor, tmp_name = tempfile.mkstemp(
        dir=str(lock.parent),
        prefix=f".{lock.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(record, file_handle, sort_keys=True)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_name, lock)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _claim_is_active(record: dict[str, Any], now: float) -> bool:
    return not record["released"] and (
        record.get("manual_recovery_required", False)
        or now - record["heartbeat_at"] <= record["lease_s"]
    )


def _heartbeat_claim(
    lock: Path,
    owner_token: str,
    lease_s: float,
    stop: threading.Event,
    errors: list[BaseException],
) -> None:
    interval_s = min(LOCK_HEARTBEAT_S, lease_s / 3)
    while not stop.wait(interval_s):
        try:
            with _claim_record_guard(lock):
                record = _read_claim_record(lock)
                if record is None or record["owner_token"] != owner_token:
                    return
                record["heartbeat_at"] = time.time()
                _write_claim_record(lock, record)
        except BaseException as exc:
            errors.append(exc)
            return


def _claim_track(
    cache_file: Path,
    lease_s: float = LOCK_LEASE_S,
) -> TrackClaim | None:
    """Claim one uncached track without waiting behind another CLI process.

    The persisted lease duration and heartbeat are independent of any
    contender's worker timeout. Every transition is serialized, and the
    immutable owner token prevents an expired owner from releasing its
    successor's lease.
    """
    if lease_s <= 0:
        raise ValueError(f"claim lease must be > 0 seconds, got {lease_s}")
    lock = cache_file.with_suffix(".json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    owner_token = uuid.uuid4().hex
    with _claim_record_guard(lock):
        record = _read_claim_record(lock)
        if record is not None and _claim_is_active(record, now):
            return None
        _write_claim_record(
            lock,
            {
                "owner_token": owner_token,
                "pid": os.getpid(),
                "heartbeat_at": now,
                "lease_s": lease_s,
                "released": False,
                "manual_recovery_required": False,
            },
        )
    stop = threading.Event()
    heartbeat_errors: list[BaseException] = []
    heartbeat_thread = threading.Thread(
        target=_heartbeat_claim,
        args=(lock, owner_token, lease_s, stop, heartbeat_errors),
        name=f"vocals-claim-{owner_token[:8]}",
        daemon=True,
    )
    heartbeat_thread.start()
    return TrackClaim(
        path=lock,
        owner_token=owner_token,
        lease_s=lease_s,
        stop_heartbeat=stop,
        heartbeat_thread=heartbeat_thread,
        heartbeat_errors=heartbeat_errors,
    )


@contextmanager
def _owned_track_claim(claim: TrackClaim) -> Iterator[None]:
    if claim.heartbeat_errors:
        raise RuntimeError("vocals claim heartbeat failed") from claim.heartbeat_errors[
            0
        ]
    with _claim_record_guard(claim.path):
        record = _read_claim_record(claim.path)
        if (
            record is None
            or record["owner_token"] != claim.owner_token
            or not _claim_is_active(record, time.time())
        ):
            raise RuntimeError("vocals track claim expired or changed owner")
        yield


def _assert_track_claim(claim: TrackClaim) -> None:
    with _owned_track_claim(claim):
        return


def _release_track_claim(claim: TrackClaim) -> None:
    claim.stop_heartbeat.set()
    claim.heartbeat_thread.join(timeout=WORKER_TERMINATE_GRACE_S)
    if claim.heartbeat_thread.is_alive():
        raise RuntimeError("vocals claim heartbeat thread did not stop")
    with _claim_record_guard(claim.path):
        record = _read_claim_record(claim.path)
        if record is None or record["owner_token"] != claim.owner_token:
            return
        if record.get("manual_recovery_required", False):
            raise RuntimeError(
                f"vocals claim requires manual recovery and cannot be released: "
                f"{claim.path}"
            )
        record["released"] = True
        record["heartbeat_at"] = time.time()
        _write_claim_record(claim.path, record)


def _retain_track_claim_for_manual_recovery(
    claim: TrackClaim,
    cleanup_error: WorkerCleanupError,
) -> None:
    """Make an unverified-cleanup claim non-expiring, then stop its heartbeat."""
    with _claim_record_guard(claim.path):
        record = _read_claim_record(claim.path)
        if record is None or record["owner_token"] != claim.owner_token:
            raise RuntimeError(
                "cannot retain unverified worker claim because ownership changed"
            ) from cleanup_error
        record["manual_recovery_required"] = True
        record["cleanup_error"] = str(cleanup_error)
        record["heartbeat_at"] = time.time()
        _write_claim_record(claim.path, record)
    print(
        f"[ERROR] worker cleanup unverified; claim retained at {claim.path}; "
        "verify the worker tree before manual recovery",
        file=sys.stderr,
    )
    claim.stop_heartbeat.set()
    claim.heartbeat_thread.join(timeout=WORKER_TERMINATE_GRACE_S)
    if claim.heartbeat_thread.is_alive():
        raise RuntimeError(
            "vocals claim heartbeat thread did not stop for manual recovery"
        ) from cleanup_error


@contextmanager
def _managed_track_claim(claim: TrackClaim) -> Iterator[None]:
    release_claim = True
    try:
        yield
    except WorkerCleanupError as exc:
        release_claim = False
        _retain_track_claim_for_manual_recovery(claim, exc)
        raise
    finally:
        if release_claim:
            _release_track_claim(claim)


# ----- output helpers ------------------------------------------------------------------


def _fmt_dur(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.0f}s"


def _counts(tracks: list[VocalTrack]) -> dict[str, int]:
    counts = {
        CATEGORY_PVDI: 0,
        CATEGORY_CACHED: 0,
        CATEGORY_MISSING: 0,
        CATEGORY_MISSING_ANALYSIS: 0,
        CATEGORY_TODO: 0,
    }
    for tr in tracks:
        counts[tr.category] += 1
    return counts


# ----- subcommands ----------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    tracks = load_tracks(ctx, args.playlist)
    classify(ctx, tracks)
    counts = _counts(tracks)
    if sum(counts.values()) != len(tracks):
        raise AssertionError("category counts do not sum to total - bug")
    todo_audio_s = sum(t.length_s for t in tracks if t.category == CATEGORY_TODO)
    eta_s = todo_audio_s * DEMUCS_REALTIME_FACTOR

    if args.json:
        print(
            json.dumps(
                {
                    "data_dir": str(ctx.data_dir),
                    "playlist": args.playlist,
                    "total": len(tracks),
                    **counts,
                    "todo_audio_s": todo_audio_s,
                    "todo_eta_s": round(eta_s, 1),
                    "realtime_factor": DEMUCS_REALTIME_FACTOR,
                },
                indent=1,
            )
        )
        return 0

    scope = f" playlist={args.playlist!r}" if args.playlist else ""
    print(f"vocal coverage scan{scope} (data-dir {ctx.data_dir})")
    print(f"  pvdi (rekordbox):  {counts[CATEGORY_PVDI]:>6}")
    print(f"  cached-demucs:     {counts[CATEGORY_CACHED]:>6}")
    print(f"  missing-file:      {counts[CATEGORY_MISSING]:>6}")
    print(f"  missing-analysis:  {counts[CATEGORY_MISSING_ANALYSIS]:>6}")
    print(
        f"  todo:              {counts[CATEGORY_TODO]:>6}"
        f"  ({_fmt_dur(todo_audio_s)} audio, "
        f"ETA {_fmt_dur(eta_s)} at {DEMUCS_REALTIME_FACTOR}x realtime)"
    )
    print(f"  total:             {len(tracks):>6}")
    return 0


def _process_one(
    ctx: Ctx,
    tr: VocalTrack,
    prefix: str,
    timeout_s: float = WORKER_TIMEOUT_S,
    claim: TrackClaim | None = None,
) -> tuple[float, dict[str, Any]]:
    """Run the worker for one track and write its cache entry.

    The recorded ``audio_mtime`` is captured BEFORE the (multi-minute)
    worker run: if the file is replaced mid-analysis the regions belong
    to the old content, so recording the pre-run mtime guarantees the
    entry self-invalidates against the new file instead of poisoning
    the cache. A detected change also fails loudly right here.
    """
    assert tr.audio_path is not None
    pre_mtime = tr.audio_path.stat().st_mtime
    pre_signature = vcache.audio_signature(tr.audio_path)
    t0 = time.perf_counter()
    result = run_worker(tr.audio_path, timeout_s)
    post_signature = vcache.audio_signature(tr.audio_path)
    if post_signature != pre_signature:
        raise RuntimeError(
            f"audio file changed during analysis: {tr.audio_path} "
            f"(source identity changed); regions were computed "
            f"from the old content, refusing to cache them"
        )
    if claim is None:
        entry = vcache.write_entry(
            vcache.cache_path(ctx.data_dir, tr.stable_id),
            result,
            tr.audio_path,
            audio_mtime=pre_mtime,
            source_signature=pre_signature,
        )
    else:
        with _owned_track_claim(claim):
            entry = vcache.write_entry(
                vcache.cache_path(ctx.data_dir, tr.stable_id),
                result,
                tr.audio_path,
                audio_mtime=pre_mtime,
                source_signature=pre_signature,
            )
    wall_s = time.perf_counter() - t0
    rate = wall_s / tr.length_s if tr.length_s else float("nan")
    print(
        f"{prefix} done in {wall_s:.1f}s ({rate:.2f}x realtime) "
        f"regions={len(entry['regions'])} cov={entry['coverage_pct']}% "
        f"-> {vcache.cache_path(ctx.data_dir, tr.stable_id)}"
    )
    return wall_s, entry


def cmd_trickle(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    live = bool(args.live)
    tracks = load_tracks(ctx, args.playlist)
    classify(ctx, tracks)

    # Explicit, never silent: every unprocessable track is logged (stderr -
    # stdout carries the plan / progress lines).
    n_missing = 0
    for tr in tracks:
        if tr.category == CATEGORY_MISSING:
            n_missing += 1
            print(
                f"[SKIP missing-file] {tr.stable_id} {tr.title!r}: "
                f"{tr.folder_path or '(no FolderPath)'}",
                file=sys.stderr,
            )
    if n_missing:
        print(f"[skipped {n_missing} missing-file tracks; see stderr]")

    n_missing_analysis = 0
    for tr in tracks:
        if tr.category == CATEGORY_MISSING_ANALYSIS:
            n_missing_analysis += 1
            print(
                f"[SKIP missing-analysis] {tr.stable_id} {tr.title!r}: "
                f"{tr.analysis_data_path or '(no AnalysisDataPath)'}",
                file=sys.stderr,
            )
    if n_missing_analysis:
        print(
            f"[skipped {n_missing_analysis} missing-analysis tracks; see stderr]"
        )

    rank = best_playlist_rank(ctx, tracks)
    todo = order_todo([t for t in tracks if t.category == CATEGORY_TODO], rank)
    batch = todo[: args.limit]
    batch_audio_s = sum(t.length_s for t in batch)
    est_s = batch_audio_s * DEMUCS_REALTIME_FACTOR

    mode = "live" if live else "dry-run"
    print(
        f"[{mode}] {len(batch)} of {len(todo)} todo tracks selected "
        f"(--limit {args.limit}); est {_fmt_dur(est_s)} "
        f"at {DEMUCS_REALTIME_FACTOR}x realtime"
    )
    for i, tr in enumerate(batch, 1):
        count, name = rank.get(tr.stable_id, (0, ""))
        pl = f"playlist={name!r}({count} on disk)" if count else "no playlist"
        print(
            f"  {i}. {tr.stable_id} {tr.title!r} {tr.length_s}s {pl} "
            f"est {_fmt_dur(tr.length_s * DEMUCS_REALTIME_FACTOR)}"
        )
    if not live:
        print("[dry-run] no work performed; pass --live to execute")
        return 0

    done_audio_s = 0.0
    done_wall_s = 0.0
    completed = 0
    for i, tr in enumerate(batch, 1):
        remaining_audio_s = sum(t.length_s for t in batch[i:])
        if tr.audio_path is None or not tr.audio_path.is_file():
            print(f"[SKIP vanished] {tr.stable_id} {tr.title!r}: {tr.audio_path}")
            continue
        if (
            vcache.load_valid_entry(
                vcache.cache_path(ctx.data_dir, tr.stable_id), tr.audio_path
            )
            is not None
        ):
            print(f"[SKIP cached] {tr.stable_id} {tr.title!r}: valid cache entry")
            continue
        cache_file = vcache.cache_path(ctx.data_dir, tr.stable_id)
        claim = _claim_track(cache_file)
        if claim is None:
            print(
                f"[SKIP in-progress] {tr.stable_id} {tr.title!r}: another CLI owns it"
            )
            continue
        with _managed_track_claim(claim):
            if vcache.load_valid_entry(cache_file, tr.audio_path) is not None:
                print(f"[SKIP cached] {tr.stable_id} {tr.title!r}: valid cache entry")
                continue
            print(f"[{i}/{len(batch)}] {tr.stable_id} {tr.title!r} ({tr.length_s}s)")
            wall_s, _entry = _process_one(
                ctx,
                tr,
                f"[{i}/{len(batch)}]",
                args.worker_timeout_s,
                claim,
            )
        completed += 1
        done_audio_s += tr.length_s
        done_wall_s += wall_s
        live_rate = done_wall_s / done_audio_s if done_audio_s else 0.0
        print(
            f"        running: {completed} done, {len(batch) - i} left, "
            f"est remaining {_fmt_dur(remaining_audio_s * live_rate)} "
            f"(measured {live_rate:.2f}x realtime)"
        )
    print(
        f"[live] finished: {completed} analysed, "
        f"{len(batch) - completed} skipped, wall {_fmt_dur(done_wall_s)}"
    )
    return 0


def cmd_one(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    tracks = [t for t in load_tracks(ctx, None) if t.stable_id == args.stable_id]
    if not tracks:
        raise SystemExit(
            f"error: stable_id {args.stable_id!r} has no rekordbox mapping "
            f"in {ctx.state_db}"
        )
    tr = tracks[0]
    if tr.audio_path is None or not tr.audio_path.is_file():
        raise SystemExit(
            f"error: audio file missing for {tr.stable_id}: "
            f"{tr.folder_path or '(no FolderPath)'}"
        )
    if _anlz_data_file(tr, load_path_map(ctx.data_dir)) is None:
        raise SystemExit(
            f"error: local ANLZ .DAT missing for {tr.stable_id}: "
            f"{tr.analysis_data_path or '(no AnalysisDataPath)'}; "
            "generated vocals would be unservable through /anlz"
        )
    cache_file = vcache.cache_path(ctx.data_dir, tr.stable_id)
    existing = vcache.load_valid_entry(cache_file, tr.audio_path)
    if existing is not None and not args.force:
        print(
            f"[cached] {tr.stable_id} {tr.title!r}: valid entry at "
            f"{cache_file} ({len(existing['regions'])} regions, "
            f"cov={existing['coverage_pct']}%); use --force to recompute"
        )
        return 0
    claim = _claim_track(cache_file)
    if claim is None:
        raise SystemExit(
            f"error: {tr.stable_id} is already being analysed by another vocals CLI process"
        )
    with _managed_track_claim(claim):
        if (
            not args.force
            and vcache.load_valid_entry(cache_file, tr.audio_path) is not None
        ):
            print(f"[cached] {tr.stable_id} {tr.title!r}: valid entry at {cache_file}")
            return 0
        print(
            f"[one] {tr.stable_id} {tr.title!r} ({tr.length_s}s) "
            f"est {_fmt_dur(tr.length_s * DEMUCS_REALTIME_FACTOR)}"
        )
        _process_one(ctx, tr, "[one]", args.worker_timeout_s, claim)
    return 0


def cmd_from_stems(args: argparse.Namespace) -> int:
    """CPU backfill: stem bundles -> vocal-cache (no demucs)."""
    from apps.stems.artifacts import (
        StemArtifactError,
        StemBundleNotFoundError,
        load_stem_bundle,
    )

    ctx = Ctx(data_dir=args.data_dir)
    root = vfrom_stems.stems_dir(ctx.data_dir)
    live = bool(args.live)
    force = bool(args.force)

    if args.stable_id:
        try:
            load_stem_bundle(args.stable_id, stems_dir=root)
        except StemBundleNotFoundError:
            raise SystemExit(
                f"error: no stem bundle for {args.stable_id!r} under {root}"
            )
        except StemArtifactError as exc:
            raise SystemExit(
                f"error: stem bundle invalid for {args.stable_id!r}: {exc}"
            )
        ids = [args.stable_id]
    else:
        ids = vfrom_stems.list_bundle_ids(root)

    by_id = {t.stable_id: t for t in load_state_tracks(ctx)}
    planned: list[VocalTrack] = []
    for sid in ids:
        tr = by_id.get(sid)
        if tr is None:
            print(
                f"[SKIP unmapped] {sid}: no rekordbox mapping in {ctx.state_db}",
                file=sys.stderr,
            )
            continue
        if tr.audio_path is None or not tr.audio_path.is_file():
            print(
                f"[SKIP missing-file] {sid} {tr.title!r}: no local audio file",
                file=sys.stderr,
            )
            continue
        cache_file = vcache.cache_path(ctx.data_dir, sid)
        existing = vcache.load_valid_entry(cache_file, tr.audio_path)
        if existing is not None and not force:
            print(
                f"[SKIP cached] {sid} {tr.title!r}: "
                f"{len(existing['regions'])} regions "
                f"cov={existing['coverage_pct']}%"
            )
            continue
        planned.append(tr)

    if args.limit is not None:
        planned = planned[: max(0, int(args.limit))]

    mode = "live" if live else "dry-run"
    print(
        f"[{mode}] from-stems: {len(planned)} track(s) "
        f"(bundles under {root}; force={force})"
    )
    for i, tr in enumerate(planned, 1):
        print(f"  {i}. {tr.stable_id} {tr.title!r}")
    if not live:
        print("[dry-run] no writes; pass --live to publish vocal-cache entries")
        return 0

    written = 0
    for i, tr in enumerate(planned, 1):
        assert tr.audio_path is not None
        t0 = time.perf_counter()
        try:
            entry = vfrom_stems.write_from_bundle(
                ctx.data_dir,
                tr.stable_id,
                tr.audio_path,
                stems_root=root,
            )
        except Exception as exc:
            print(
                f"[FAIL] {tr.stable_id} {tr.title!r}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        wall_s = time.perf_counter() - t0
        written += 1
        print(
            f"[{i}/{len(planned)}] {tr.stable_id} {tr.title!r} "
            f"regions={len(entry['regions'])} cov={entry['coverage_pct']}% "
            f"in {wall_s:.2f}s -> {vcache.cache_path(ctx.data_dir, tr.stable_id)}"
        )
    print(f"[live] finished: {written} written, {len(planned) - written} failed")
    return 0 if written == len(planned) else 1


# ----- parser ----------------------------------------------------------------------------


def _nonnegative_int(raw_value: str) -> int:
    """Parse a nonnegative CLI integer without Python slice semantics."""
    value = int(raw_value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.vocals",
        description="demucs vocal-region gap-fill: scan coverage, trickle "
        "the todo queue, analyse one track, or backfill from stems.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help=f"data root holding state/state.db, master.plain.db and "
        f"state/vocal-cache (default: {DATA_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan", parents=[common], help="report vocal coverage + trickle ETA"
    )
    scan.add_argument("--playlist", default=None, help="limit to one playlist name")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.set_defaults(func=cmd_scan)

    trickle = sub.add_parser(
        "trickle",
        parents=[common],
        help="process the todo queue (requires --dry-run or --live)",
    )
    trickle.add_argument(
        "--limit",
        type=_nonnegative_int,
        default=DEFAULT_TRICKLE_LIMIT,
        help=f"max tracks this run (default {DEFAULT_TRICKLE_LIMIT})",
    )
    trickle.add_argument(
        "--playlist", default=None, help="limit the queue to one playlist name"
    )
    trickle.add_argument(
        "--worker-timeout-s",
        type=float,
        default=WORKER_TIMEOUT_S,
        help=f"per-track worker deadline in seconds (default {WORKER_TIMEOUT_S:g})",
    )
    mode = trickle.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="plan only, no worker runs"
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="actually run the demucs worker + write cache",
    )
    trickle.set_defaults(func=cmd_trickle)

    one = sub.add_parser(
        "one", parents=[common], help="analyse a single track (debug path)"
    )
    one.add_argument("--stable-id", required=True)
    one.add_argument(
        "--force",
        action="store_true",
        help="recompute even when a valid cache entry exists",
    )
    one.add_argument(
        "--worker-timeout-s",
        type=float,
        default=WORKER_TIMEOUT_S,
        help=f"worker deadline in seconds (default {WORKER_TIMEOUT_S:g})",
    )
    one.set_defaults(func=cmd_one)

    from_stems = sub.add_parser(
        "from-stems",
        parents=[common],
        help="CPU backfill vocal-cache from existing stem bundles (no demucs)",
    )
    from_stems.add_argument(
        "--stable-id",
        default=None,
        help="only this track (default: every loadable stem bundle)",
    )
    from_stems.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max tracks to write this run (default: no cap)",
    )
    from_stems.add_argument(
        "--force",
        action="store_true",
        help="rewrite even when a valid vocal-cache entry exists",
    )
    fs_mode = from_stems.add_mutually_exclusive_group(required=True)
    fs_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="plan only, no cache writes",
    )
    fs_mode.add_argument(
        "--live",
        action="store_true",
        help="derive regions from stems and write vocal-cache",
    )
    from_stems.set_defaults(func=cmd_from_stems)
    return parser


def main(argv: list[str] | None = None) -> int:
    from apps.vocals.errors import UnknownPlaylistError

    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except UnknownPlaylistError as exc:
        raise SystemExit(
            2,
            f"error: unknown playlist {exc.name!r}. "
            f"Known: {', '.join(exc.known) or '(none)'}",
        )


if __name__ == "__main__":
    sys.exit(main())
