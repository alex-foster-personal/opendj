#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Mirror every on-disk track to the bifrost2 asset store, idempotently.

An independently reachable second copy protects materialised tracks from
local eviction or loss. This mirror is written to be re-run idempotently.

SHELL: the configured remote shell is MSYS2/MinGW64 bash, not Windows cmd.
Command construction and listing parsing live in ``scripts/b2shell.py``.
The resume oracle is a ``find -printf`` index rather than rsync --size-only.

REMOTE LAYOUT -- content-addressed, deliberately NOT a path mirror:

    D:/asset-store/music/<stable_id[:2]>/<stable_id><ext>

Mirroring arbitrary source paths onto Windows can encounter NTFS-invalid
characters, non-ASCII text and MAX_PATH limits. A 40-hex stable_id is the
remote name, while ``manifest.json`` carries the mapping back to the source.
The two-character shard keeps each directory listing small.

REQUIREMENTS (status: -> out-of-scope, ? todo, ✔︎ done, ✔︎ ✅ done+ran+works,
✔︎ ✅ 🎯 done+working+regression tests)

R1 ✔︎ ✅ Idempotent and resumable: a file already on bifrost2 at the same byte
        size is skipped, so a re-run after any interruption costs one listing.
    [if] the script is run twice with no local change, the second run uploads 0
         files and reports every track as skipped [then ⛔️]
    [if] a remote copy is truncated (size differs from local), the re-run
         re-uploads that one file rather than trusting its presence [then ⛔️]
    [if] the run is killed mid-transfer, the next run resumes from the listing
         and does not restart from the first track [then ⛔️]

R2 ✔︎ ✅ Never reads an iCloud-dataless file: st_blocks == 0 means the bytes are
        not local, and opening it would either block on a network fetch or
        upload a zero-length husk over a good backup.
    [if] a track is evicted (st_blocks == 0), it is skipped and named in the
         report, never opened and never scp'd [then ⛔️]
    [if] a track is re-evicted between the initial scan and its own upload, the
         immediately-preceding re-stat catches it and skips it [then ⛔️]
    [if] any evicted track were uploaded, its remote size would be 0 while the
         manifest claims st_size, and verification would flag it [then ⛔️]

R3 ✔︎ ✅ Verified by remote byte size, not by exit code. This repo has a
        documented incident where rsync exited 0 having copied nothing.
    [if] scp exits 0 but the remote file is absent from the post-run listing,
         the run reports a MISMATCH and exits non-zero [then ⛔️]
    [if] the remote size differs from the local st_size by even one byte, that
         track is listed as a mismatch [then ⛔️]
    [if] every uploaded track re-lists at its exact local size, the run reports
         OK and exits 0 [then ⛔️]

R4 ✔︎ ✅ Writes a manifest mapping stable_id -> remote path and original
        file_path, so a future restore can put every file back where
        ``tracks.file_path`` expects it.
    [if] the manifest is missing any successfully-mirrored track, the restore
         mapping is incomplete [then ⛔️]
    [if] a manifest entry lacks either local_path or remote_path, that entry
         cannot drive a restore [then ⛔️]
    [if] the manifest is not also pushed to the store, the remote is not
         self-describing and a Mac loss strands the backup [then ⛔️]

R5 ✔︎ ✅ --limit and --dry-run make the script safe to rehearse.
    [if] --dry-run transfers a single byte or mutates the remote [then ⛔️]
    [if] --limit N considers more than N tracks [then ⛔️]
    [if] --dry-run still prints the real skip/upload split from a real remote
         listing (not a guess) [then ⛔️]

R6 ✔︎ ✅ --rehydrate recovers evicted tracks and mirrors them in the SAME loop.
        Rehydrated files can be re-evicted before a later backup pass when
        local storage is constrained. Upload each batch within its
        materialisation window instead of rehydrating the entire set first.
    [if] a batch is downloaded but not uploaded before the next batch starts,
         macOS may re-evict it and the mirror silently misses it [then ⛔️]
    [if] a batch would take free space below --min-free-gb, it must be skipped
         rather than fill the disk [then ⛔️]
    [if] a track iCloud will not return is not named in the report, a real data
         loss is hidden as a success [then ⛔️]

Run as a MODULE, not a file path: it imports scripts.b2shell, and
``python scripts/backup_music_to_bifrost2.py`` puts scripts/ on sys.path rather
than the repo root. The -m form needs no sys.path hack.

Re-run:  uv run python -m scripts.backup_music_to_bifrost2 --rehydrate
Rehearse: uv run python -m scripts.backup_music_to_bifrost2 --dry-run --limit 20

-Claude
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

try:
    from scripts import b2shell
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.backup_music_to_bifrost2") from None
    raise
from scripts.b2shell import (
    find_command,
    listing_or_absent,
    mkdir_command,
    parse_names,
    parse_sizes,
)

#----- config -----------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
STATE_DB: Path = REPO_ROOT / "data" / "state" / "state.db"
MANIFEST_PATH: Path = REPO_ROOT / "data" / "backup" / "bifrost2-music-manifest.json"

SSH_HOST: str = "bifrost2"                    # ~/.ssh/config alias, as b2store.py
STORE_ROOT: str = "D:/asset-store"            # forward slashes: bifrost2 runs bash
MUSIC_SUBDIR: str = "music"
REMOTE_ROOT: str = f"{STORE_ROOT}/{MUSIC_SUBDIR}"

# One multiplexed SSH connection avoids a fresh handshake for every scp call.
CONTROL_PATH: str = "/tmp/.b2-music-backup-%r@%h:%p"
SSH_OPTS: list[str] = [
    "-o", "ConnectTimeout=20",
    "-o", "ServerAliveInterval=15",
    "-o", f"ControlPath={CONTROL_PATH}",
]
CONTROL_PERSIST: str = "600"

PROGRESS_EVERY: int = 25

# One `mkdir -p` per chunk of shards, to keep the remote command line short.
# 25 shard paths is roughly 700 bytes, well inside any argv limit.
MKDIR_CHUNK: int = 25

# --rehydrate tuning. The batch is small on purpose: every downloaded file is a
# hostage to the next eviction sweep, so the window between `brctl download`
# and its scp must stay short.
REHYDRATE_BATCH: int = 20
REHYDRATE_POLL_S: float = 3.0
REHYDRATE_TIMEOUT_S: float = 240.0
DEFAULT_MIN_FREE_GB: float = 10.0

# Concurrent scp streams. bifrost2 is over the WAN, so a single stream leaves the
# uplink idle between round trips; the stem farm uses the same trick.
DEFAULT_WORKERS: int = 6


#----- model ------------------------------------------------------------------

@dataclass(frozen=True)
class Track:
    """One library row that has a local file, with its remote destination."""

    stable_id: str
    local_path: Path
    size: int

    @property
    def remote_rel(self) -> str:
        """Store-relative destination: <shard>/<stable_id><ext>."""
        return f"{self.stable_id[:2]}/{self.stable_id}{self.local_path.suffix.lower()}"

    @property
    def remote_path(self) -> str:
        return f"{REMOTE_ROOT}/{self.remote_rel}"


@dataclass
class Report:
    """Everything the run needs to prove what it did."""

    uploaded: list[Track] = field(default_factory=list)
    skipped_present: list[Track] = field(default_factory=list)
    skipped_evicted: list[Track] = field(default_factory=list)
    rehydrated: list[Track] = field(default_factory=list)
    refused_by_icloud: list[Track] = field(default_factory=list)
    missing_on_disk: int = 0
    failed: list[tuple[Track, str]] = field(default_factory=list)
    mismatched: list[tuple[Track, int]] = field(default_factory=list)
    bytes_uploaded: int = 0
    stopped_on_disk_floor: bool = False


#----- remote -----------------------------------------------------------------

def _ssh(remote_cmd: str, timeout: int = 900) -> str:
    """Bind this run's host and multiplexed SSH options onto b2shell.ssh."""
    return b2shell.ssh(SSH_HOST, SSH_OPTS, remote_cmd, timeout=timeout)


def _open_control_master() -> None:
    """Open the shared SSH connection, proving the tailnet is up before any work."""
    b2shell.run([
        "ssh", "-o", "ControlMaster=auto", "-o", f"ControlPersist={CONTROL_PERSIST}",
        *SSH_OPTS, SSH_HOST, "echo ok",
    ], timeout=60)


def _close_control_master() -> None:
    subprocess.run(
        ["ssh", "-O", "exit", "-o", f"ControlPath={CONTROL_PATH}", SSH_HOST],
        capture_output=True, text=True, check=False,
    )


def _remote_shard_dirs() -> set[str]:
    """Shard directory names that currently exist under the music root.

    An absent root reads as empty; every other non-zero exit propagates. The old
    blanket ``return set()`` hid the opposite: ``dir /b /ad`` exited 2 on every
    call and the emptiness read as "no shards yet".
    """
    listing = listing_or_absent(_ssh(
        find_command(REMOTE_ROOT, "-maxdepth 1 -mindepth 1 -type d -printf '%P\\n'"),
        timeout=120,
    ))
    return set() if listing is None else parse_names(listing)


def _ensure_remote_dirs(shards: set[str]) -> None:
    """Create the music root and every shard dir, then PROVE they exist.

    The result is re-listed rather than trusted: a zero exit proves the command
    ran, not that the directories are there, and a backup reporting success
    while copying nothing is the failure this script exists to rule out.
    """
    wanted = sorted(shards)
    # The root on its own: with nothing pending there are no shard chunks to
    # piggyback on, and the manifest scp still needs somewhere to land.
    _ssh(mkdir_command([REMOTE_ROOT]))
    for start in range(0, len(wanted), MKDIR_CHUNK):
        batch = wanted[start:start + MKDIR_CHUNK]
        _ssh(mkdir_command([f"{REMOTE_ROOT}/{shard}" for shard in batch]))

    missing = sorted(set(shards) - _remote_shard_dirs())
    if missing:
        raise RuntimeError(
            f"could not create {len(missing)} shard dirs on {SSH_HOST}: "
            f"{missing[:10]}"
        )


def _remote_index() -> dict[str, int]:
    """{store-relative path: bytes} for every file under the music tree.

    One round trip. Keyed by ``Track.remote_rel``, so a file in the wrong shard
    reads as missing, not as mirrored. An absent tree yields an empty index.
    """
    listing = listing_or_absent(_ssh(
        find_command(REMOTE_ROOT, "-type f -printf '%s %P\\n'"), timeout=300,
    ))
    return {} if listing is None else parse_sizes(listing, source=SSH_HOST)


def _scp(track: Track) -> None:
    b2shell.run(["scp", *SSH_OPTS, "-p", str(track.local_path), f"{SSH_HOST}:{track.remote_path}"])


#----- local ------------------------------------------------------------------

def _is_materialised(path: Path) -> tuple[bool, int]:
    """(bytes are local, size). st_blocks == 0 with a non-zero size is the
    signature of an iCloud dataless placeholder -- reading it would fetch or
    stall, so callers must not open it."""
    try:
        stat = path.stat()
    except OSError:
        return False, -1
    return stat.st_blocks > 0, stat.st_size


def _load_tracks(limit: int | None) -> tuple[list[Track], list[Track], int]:
    """(materialised, evicted, missing_count) from the state DB, read-only."""
    if not STATE_DB.exists():
        raise FileNotFoundError(f"state db not found: {STATE_DB}")
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "select stable_id, file_path from tracks where file_path is not null"
    ).fetchall()
    conn.close()

    live: list[Track] = []
    evicted: list[Track] = []
    missing = 0
    for stable_id, file_path in rows:
        path = Path(file_path)
        materialised, size = _is_materialised(path)
        if size < 0:
            missing += 1
        elif materialised:
            live.append(Track(stable_id, path, size))
        else:
            evicted.append(Track(stable_id, path, size))
        if limit is not None and len(live) + len(evicted) >= limit:
            break
    return live, evicted, missing


#----- run --------------------------------------------------------------------

def _upload_one(track: Track, report: Report, lock: threading.Lock) -> None:
    """scp one track, re-checking eviction immediately before reading it (R2)."""
    materialised, size = _is_materialised(track.local_path)
    if not materialised:
        with lock:
            report.skipped_evicted.append(track)
        return
    try:
        _scp(track)
        with lock:
            report.uploaded.append(track)
            report.bytes_uploaded += size
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        with lock:
            report.failed.append((track, str(exc).splitlines()[0]))


def _upload(pending: list[Track], report: Report, workers: int) -> None:
    """scp the pending tracks over a pool of concurrent connections.

    bifrost2 is reached over the WAN (Tailscale reports a direct path to a public
    IP, not the LAN), so a single scp stream leaves the uplink idle between round
    trips. The stem farm parallelises its pushes for the same reason.
    """
    started = time.time()
    lock = threading.Lock()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_upload_one, track, report, lock): track
                   for track in pending}
        for _ in as_completed(futures):
            done += 1
            if done % PROGRESS_EVERY == 0 or done == len(pending):
                elapsed = max(time.time() - started, 1e-6)
                rate = report.bytes_uploaded / elapsed / 1e6
                eta_min = ((len(pending) - done) / max(done / elapsed, 1e-9)) / 60
                print(
                    f"  [{done}/{len(pending)}] uploaded={len(report.uploaded)} "
                    f"failed={len(report.failed)} {report.bytes_uploaded / 1e9:.2f} GB "
                    f"@ {rate:.1f} MB/s eta {eta_min:.0f}m",
                    flush=True,
                )


def _free_gb() -> float:
    return shutil.disk_usage("/Users").free / 1e9


def _rehydrate_and_upload(evicted: list[Track], report: Report, min_free_gb: float,
                          workers: int) -> None:
    """Pull evicted tracks back from iCloud and scp each batch straight out.

    Deliberately batch-interleaved rather than two passes: this Mac re-evicts
    within minutes (see R6), so a downloaded file is only reliably readable for
    a short window. Smallest-first maximises files rescued per GB of headroom.
    """
    lock = threading.Lock()
    queue = sorted(evicted, key=lambda t: t.size)
    print(f"rehydrating {len(queue)} evicted tracks "
          f"({sum(t.size for t in queue) / 1e9:.2f} GB), free={_free_gb():.2f} GB, "
          f"floor={min_free_gb:.1f} GB")

    for start in range(0, len(queue), REHYDRATE_BATCH):
        batch = queue[start:start + REHYDRATE_BATCH]
        need_gb = sum(t.size for t in batch) / 1e9
        if _free_gb() - need_gb < min_free_gb:
            print(f"  [STOP] disk floor: free={_free_gb():.2f} GB, "
                  f"batch needs {need_gb:.2f} GB")
            report.stopped_on_disk_floor = True
            report.skipped_evicted.extend(queue[start:])
            return

        for track in batch:
            subprocess.run(["brctl", "download", str(track.local_path)],
                           capture_output=True, check=False)

        deadline = time.time() + REHYDRATE_TIMEOUT_S
        waiting = list(batch)
        while waiting and time.time() < deadline:
            time.sleep(REHYDRATE_POLL_S)
            waiting = [t for t in waiting if not _is_materialised(t.local_path)[0]]

        arrived, refused = [], []
        for track in batch:
            (arrived if _is_materialised(track.local_path)[0] else refused).append(track)
        report.refused_by_icloud.extend(refused)
        report.rehydrated.extend(arrived)
        # Upload immediately, in parallel: every arrived file is a hostage to the
        # next eviction sweep, so the window must stay short.
        if arrived:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(partial(_upload_one, report=report, lock=lock), arrived))

        print(f"  [{min(start + REHYDRATE_BATCH, len(queue))}/{len(queue)}] "
              f"rehydrated={len(report.rehydrated)} refused={len(report.refused_by_icloud)} "
              f"uploaded={len(report.uploaded)} free={_free_gb():.2f} GB", flush=True)


def _verify(report: Report, candidates: list[Track]) -> None:
    """Re-list the remote and compare byte sizes. Exit code is not evidence."""
    index = _remote_index()
    for track in candidates:
        remote_size = index.get(track.remote_rel)
        if remote_size is None:
            report.mismatched.append((track, -1))
        elif remote_size != track.size:
            report.mismatched.append((track, remote_size))


def _write_manifest(mirrored: list[Track], report: Report) -> None:
    """stable_id -> remote path + original file_path, locally and on the store."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ssh_host": SSH_HOST,
        "remote_root": REMOTE_ROOT,
        "layout": "<remote_root>/<stable_id[:2]>/<stable_id><ext>",
        "restore": "copy remote_path back to local_path for each entry",
        "counts": {
            "mirrored": len(mirrored),
            "uploaded_this_run": len(report.uploaded),
            "skipped_evicted": len(report.skipped_evicted),
            "failed": len(report.failed),
            "mismatched": len(report.mismatched),
        },
        "tracks": {
            track.stable_id: {
                "local_path": str(track.local_path),
                "remote_path": track.remote_path,
                "size": track.size,
            }
            for track in sorted(mirrored, key=lambda t: t.stable_id)
        },
        "skipped_evicted": [str(t.local_path) for t in report.skipped_evicted],
    }
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    b2shell.run(["scp", *SSH_OPTS, str(MANIFEST_PATH), f"{SSH_HOST}:{REMOTE_ROOT}/manifest.json"])


def _summarise(report: Report, mirrored: list[Track]) -> int:
    print("\n----- summary -----------------------------------------------------")
    print(f"  mirrored on bifrost2 : {len(mirrored)}")
    print(f"  uploaded this run    : {len(report.uploaded)} "
          f"({report.bytes_uploaded / 1e9:.2f} GB)")
    print(f"  already present      : {len(report.skipped_present)}")
    print(f"  rehydrated + mirrored: {len(report.rehydrated)}")
    print(f"  refused by iCloud    : {len(report.refused_by_icloud)}")
    print(f"  skipped (evicted)    : {len(report.skipped_evicted)}")
    print(f"  missing on disk      : {report.missing_on_disk}")
    print(f"  failed transfers     : {len(report.failed)}")
    print(f"  size mismatches      : {len(report.mismatched)}")
    if report.stopped_on_disk_floor:
        print("  NOTE: stopped early on the free-disk floor; re-run after freeing space")
    for track in report.refused_by_icloud[:10]:
        print(f"    REFUSED  {track.local_path}")
    for track in report.skipped_evicted[:10]:
        print(f"    EVICTED {track.local_path}")
    for track, why in report.failed[:10]:
        print(f"    FAILED  {track.stable_id} {why}")
    for track, size in report.mismatched[:10]:
        print(f"    MISMATCH {track.stable_id} local={track.size} remote={size}")
    ok = not report.failed and not report.mismatched
    print(f"  RESULT: {'OK' if ok else 'INCOMPLETE'}")
    print("-------------------------------------------------------------------")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="consider at most N tracks (rehearsal)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list and diff against the real remote, transfer nothing")
    parser.add_argument("--rehydrate", action="store_true",
                        help="pull evicted tracks back from iCloud and mirror them "
                             "in the same batch (see R6)")
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB,
                        help=f"never take free disk below this (default {DEFAULT_MIN_FREE_GB})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"concurrent scp streams (default {DEFAULT_WORKERS})")
    args = parser.parse_args()

    live, evicted, missing = _load_tracks(args.limit)
    report = Report(missing_on_disk=missing)
    print(f"tracks: {len(live)} materialised, {len(evicted)} evicted, "
          f"{missing} not on disk")

    _open_control_master()
    try:
        index = _remote_index()
        print(f"remote holds {len(index)} files under {REMOTE_ROOT}")

        def already_mirrored(track: Track) -> bool:
            return index.get(track.remote_rel) == track.size

        pending = [t for t in live if not already_mirrored(t)]
        report.skipped_present = [t for t in live if already_mirrored(t)]
        # An evicted track already on bifrost2 needs no risky rehydration.
        evicted_present = [t for t in evicted if already_mirrored(t)]
        evicted_pending = [t for t in evicted if not already_mirrored(t)]
        report.skipped_present.extend(evicted_present)
        print(f"to upload: {len(pending)} "
              f"({sum(t.size for t in pending) / 1e9:.2f} GB); "
              f"already present: {len(report.skipped_present)}; "
              f"evicted still unmirrored: {len(evicted_pending)}")

        if args.dry_run:
            report.skipped_evicted = evicted_pending
            print("[dry-run] no transfer performed")
            return _summarise(report, report.skipped_present)

        shards = {t.stable_id[:2] for t in pending}
        if args.rehydrate:
            shards |= {t.stable_id[:2] for t in evicted_pending}
        # Unconditional: with nothing pending there is still a manifest to scp
        # into the root, and `mkdir -p` costs one round trip.
        _ensure_remote_dirs(shards)
        if pending:
            _upload(pending, report, args.workers)

        if args.rehydrate and evicted_pending:
            _rehydrate_and_upload(evicted_pending, report, args.min_free_gb,
                                  args.workers)
        else:
            report.skipped_evicted.extend(evicted_pending)

        mirrored = report.skipped_present + report.uploaded
        _verify(report, mirrored)
        _write_manifest([t for t in mirrored
                         if t not in {m for m, _ in report.mismatched}], report)
        return _summarise(report, mirrored)
    finally:
        _close_control_master()


if __name__ == "__main__":
    raise SystemExit(main())
