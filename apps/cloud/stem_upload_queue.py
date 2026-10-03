"""The stem upload queue file (``state/stem-upload-queue.json``) and its writes.

Two enforcement passes can overlap (the timer and a post-hydrate pass), and
each computes the queue from the bundles IT scanned. Writing that snapshot
as-is would let an older pass erase a bundle a newer pass queued after the
older one's scan. So every save is one serialized transaction that merges
against the file as it stands on disk: an entry for a bundle this pass never
saw (and did not evict) is kept while that bundle's directory still exists,
and so is the on-disk verdict for a bundle this pass did see when the bundle
was re-rendered after this pass scanned it (its fingerprint moved), since a
newer pass may have judged the new bytes.

The passes can run in different processes (the engine timer and a
``python -m apps.stems.hydrate_runner`` job on the same data dir), so the
transaction holds an OS file lock beside the queue file as well as the
in-process lock: ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows, the
same primitive ``apps.webui.server.dedup_decisions.decision_file_lock`` uses.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import IO, TypeVar

from apps.cloud.stem_bundles import current_fingerprint
from apps.cloud.stem_cache_settings import write_json_atomically

UPLOAD_QUEUE_FILENAME: str = "stem-upload-queue.json"
UPLOAD_QUEUE_SCHEMA_VERSION: int = 1

_SAVE_LOCK = threading.Lock()
_V = TypeVar("_V")

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def upload_queue_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / UPLOAD_QUEUE_FILENAME


def upload_queue_lock_path(data_dir: Path) -> Path:
    path = upload_queue_path(data_dir)
    return path.with_name(f"{path.name}.lock")


def lock_region_until_acquired(
    handle: IO[bytes],
    locking: Callable[[int, int, int], None],
    nonblocking_mode: int,
    *,
    retry_s: float = 0.05,
) -> None:
    """Take byte 0 of ``handle`` on Windows, waiting as long as it takes.

    ``msvcrt.LK_LOCK`` gives up with OSError after about ten one-second
    retries, which a hydrate holding the lock across a whole bundle download
    outlasts; so this polls the non-blocking mode instead, without a limit,
    like ``flock(LOCK_EX)`` on POSIX.
    """
    while True:
        handle.seek(0)
        try:
            locking(handle.fileno(), nonblocking_mode, 1)
        except OSError:
            time.sleep(retry_s)
            continue
        return


@contextlib.contextmanager
def exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an OS-level exclusive lock on ``lock_path`` (blocking), across processes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            lock_region_until_acquired(handle, msvcrt.locking, msvcrt.LK_NBLCK)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def upload_queue_file_lock(data_dir: Path) -> Iterator[None]:
    """Hold the cross-process lock on ``data_dir``'s upload queue (blocking)."""
    with exclusive_file_lock(upload_queue_lock_path(data_dir)):
        yield


def _payload(data_dir: Path) -> dict[str, object]:
    path = upload_queue_path(data_dir)
    if not path.is_file():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")))


def load_upload_queue(data_dir: Path) -> dict[str, dict[str, object]]:
    """``{stable_id: {reason, bytes, queued_at}}`` for bundles awaiting upload."""
    return dict(_payload(data_dir).get("bundles", {}))  # type: ignore[call-overload]


def load_verified_fingerprints(data_dir: Path) -> dict[str, str]:
    """``{stable_id: fingerprint}`` of bundles last hashed equal to the index.

    Kept beside the queue (an additive key, absent in older files) so a
    bundle is hashed once per change, not once per pass."""
    return dict(_payload(data_dir).get("verified", {}))  # type: ignore[call-overload]


def save_upload_queue(
    data_dir: Path,
    queue: dict[str, dict[str, object]],
    verified: dict[str, str],
    *,
    stems_dir: Path,
    seen: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Write this pass's verdicts, merged against the file on disk. Returns
    the queue as written.

    ``seen`` maps every id the pass scanned (evicted ones too) to the
    fingerprint it scanned. The on-disk entry wins for an id the pass did not
    see whose directory is still there, and for a seen id whose bundle has
    been re-rendered since (a newer pass may have judged those bytes); this
    pass's verdict wins everywhere else, including for a bundle now gone."""
    root = Path(stems_dir)

    def disk_wins(stable_id: str) -> bool:
        if stable_id not in seen:
            return (root / stable_id).is_dir()
        current = current_fingerprint(root / stable_id)
        return current is not None and current != seen[stable_id]

    def merge(ours: Mapping[str, _V], on_disk: Mapping[str, _V]) -> dict[str, _V]:
        kept = {k for k in set(ours) | set(on_disk) if ours.get(k) != on_disk.get(k) and disk_wins(k)}
        merged = {k: v for k, v in ours.items() if k not in kept}
        merged.update({k: on_disk[k] for k in kept if k in on_disk})
        return merged

    with _SAVE_LOCK, upload_queue_file_lock(data_dir):
        merged = merge(queue, load_upload_queue(data_dir))
        merged_verified = merge(verified, load_verified_fingerprints(data_dir))
        path = upload_queue_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "schema_version": UPLOAD_QUEUE_SCHEMA_VERSION,
            "bundles": merged,
        }
        if merged_verified:
            payload["verified"] = merged_verified
        write_json_atomically(path, payload)
        return merged
