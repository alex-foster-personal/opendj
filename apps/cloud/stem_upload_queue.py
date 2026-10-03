"""The stem upload queue file (``state/stem-upload-queue.json``) and its writes.

Two enforcement passes can overlap (the timer and a post-hydrate pass), and
each computes the queue from the bundles IT scanned. Writing that snapshot
as-is would let an older pass erase a bundle a newer pass queued after the
older one's scan. So every save is one serialized transaction that merges
against the file as it stands on disk: an entry for a bundle this pass never
saw (and did not evict) is kept while that bundle's directory still exists.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Collection
from pathlib import Path

from apps.cloud.stem_cache_settings import write_json_atomically

UPLOAD_QUEUE_FILENAME: str = "stem-upload-queue.json"
UPLOAD_QUEUE_SCHEMA_VERSION: int = 1

_SAVE_LOCK = threading.Lock()


def upload_queue_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / UPLOAD_QUEUE_FILENAME


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
    seen: Collection[str],
) -> dict[str, dict[str, object]]:
    """Write this pass's verdicts, keeping on-disk entries for bundles the
    pass did not see (``seen`` is every id it scanned, evicted ones too)
    that are still on disk. Returns the queue as written."""
    def unseen_and_present(stable_id: str) -> bool:
        return stable_id not in seen and (Path(stems_dir) / stable_id).is_dir()

    with _SAVE_LOCK:
        merged = {
            **{k: v for k, v in load_upload_queue(data_dir).items() if unseen_and_present(k)},
            **queue,
        }
        merged_verified = {
            **{
                k: v
                for k, v in load_verified_fingerprints(data_dir).items()
                if unseen_and_present(k)
            },
            **verified,
        }
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
