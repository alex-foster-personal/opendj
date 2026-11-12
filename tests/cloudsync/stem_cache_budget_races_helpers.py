"""Shared helpers for the test_stem_cache_budget_races test modules (split from test_stem_cache_budget_races.py)."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_hydration import OpenDeckRegistry
from apps.stems.bundle_lock import swap_backup_path
from tests.cloudsync.stem_cache_rig import (
    FLOOR_BYTES,
    disk_usage,
    enforce,
    make_bundle,
    wav_bytes,
)


def _seen(stems_dir: Path, *ids: str) -> dict[str, str]:
    """``seen`` as a pass that scanned these bundles just now passes it."""
    from apps.cloud.stem_bundles import current_fingerprint

    return {i: current_fingerprint(stems_dir / i) or "gone" for i in ids}


def _lose_the_claim_for(budget_mod, lost: str, monkeypatch) -> None:
    """Another pass wins the claim on ``lost``: it removes the bundle, and
    this pass's ``claim_and_remove`` reports False."""
    import shutil

    real = budget_mod.claim_and_remove

    def claim(path: Path) -> bool:
        if path.name == lost:
            shutil.rmtree(path)
            return False
        return real(path)

    monkeypatch.setattr(budget_mod, "claim_and_remove", claim)


def _swap_in_renamed_parts(bundle_dir: Path) -> None:
    """Replace ``bundle_dir`` with a copy whose ``bass.wav`` is named ``bass.mp3``, as a re-render would."""
    import shutil

    staged = bundle_dir.with_name(bundle_dir.name + ".staged")
    shutil.copytree(bundle_dir, staged)
    (staged / "bass.wav").rename(staged / "bass.mp3")
    shutil.rmtree(bundle_dir)
    staged.rename(bundle_dir)


def _swap_on_hash(monkeypatch, stable_id: str, call: int, act) -> None:
    """Run ``act`` on ``stable_id``'s bundle just before its ``call``-th hash (1-based)."""
    real = budget.unconfirmed_reason
    calls: list[str] = []

    def hash_after(bundle, idx):
        if bundle.stable_id == stable_id:
            calls.append(stable_id)
            if len(calls) == call:
                act(bundle.path)
        return real(bundle, idx)

    monkeypatch.setattr(budget, "unconfirmed_reason", hash_after)


def _hold_bundle_lock(data_dir: Path, stable_id: str):
    from apps.cloud.stem_upload_queue import exclusive_file_lock
    from apps.stems.bundle_lock import bundle_lock_path

    return exclusive_file_lock(bundle_lock_path(data_dir, stable_id))


def _hold_claim_lock(data_dir: Path, stable_id: str):
    from apps.cloud.stem_upload_queue import exclusive_file_lock

    return exclusive_file_lock(budget.eviction_claim_lock_path(data_dir, stable_id))


def _enforce_in_thread(stems_dir: Path, data_dir: Path, index: dict) -> tuple[threading.Thread, list]:
    reports: list = []

    def run() -> None:
        reports.append(enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1)))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, reports


def _swap_in_a_new_bundle(stems_dir: Path, stable_id: str) -> dict[str, bytes]:
    """What the STEM-58 swap does under the lock: old dir out, a new one renamed in."""
    import shutil

    staged = stems_dir.parent / "staged"
    make_bundle(staged, stable_id, atime=300.0)
    (staged / stable_id / "vocals.wav").write_bytes(wav_bytes(frames=16, seed=7))
    shutil.move(str(stems_dir / stable_id), str(stems_dir.parent / "backup"))
    (staged / stable_id).rename(stems_dir / stable_id)
    return {p.name: p.read_bytes() for p in (stems_dir / stable_id).iterdir()}


def _rewrite_same_size_same_mtime(bundle_dir: Path) -> dict[str, bytes]:
    """A same-codec republish on a coarse clock (1-2 s on HFS+ or FAT): new
    bytes, same size, and an mtime that did not tick, so name, size and mtime
    all match what the scan saw."""
    part = bundle_dir / "vocals.wav"
    before = part.stat()
    data = bytearray(part.read_bytes())
    data[-1] ^= 0xFF
    part.write_bytes(bytes(data))
    os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert part.stat().st_size == before.st_size and part.stat().st_mtime_ns == before.st_mtime_ns
    return {p.name: p.read_bytes() for p in bundle_dir.iterdir()}


def _enforce_live_in_thread(
    stems_dir: Path, data_dir: Path, index: dict, registry: OpenDeckRegistry
) -> tuple[threading.Thread, list]:
    reports: list = []

    def run() -> None:
        reports.append(
            enforce(
                stems_dir,
                data_dir,
                index=index,
                disk=disk_usage(free=FLOOR_BYTES - 1),
                protected=registry.open_ids(),
                live_protected=registry.open_ids,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    return thread, reports


def _wait_until(predicate, *, what: str, timeout_s: float = 30.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def _stale_swap_backup(stems_dir: Path, stable_id: str) -> Path:
    """What a writer killed after its new bundle landed and before it discarded
    the old one leaves: the live bundle at ``dest`` and an older render at the
    swap backup."""
    backup = swap_backup_path(stems_dir / stable_id)
    backup.mkdir(parents=True)
    (backup / "manifest.json").write_bytes(b"older render")
    return backup


def _big_bundle(stems_dir: Path, stable_id: str, *, mib: int) -> dict[str, str]:
    """A bundle whose ``other.wav`` is ``mib`` MiB; returns its R2 index entry.

    The hash reads parts in name order, so ``bass.wav`` (small, first) is
    hashed within microseconds and ``other.wav`` keeps the hash busy long
    after a writer that changes ``bass.wav`` has landed: the hash then
    matches R2 and only the re-check can see the change."""
    entry = make_bundle(stems_dir, stable_id, atime=100.0)
    vocals = stems_dir / stable_id / "other.wav"
    body = os.urandom(mib * 1024 * 1024)
    vocals.write_bytes(body)
    os.utime(vocals, (100.0, 100.0))
    entry["other.wav"] = hashlib.sha256(body).hexdigest()
    return entry


def _stage_a_lookalike(stems_dir: Path, stable_id: str) -> Path:
    """A new render with every name, size and mtime of the live one and one
    byte changed: what a same-codec republish on a coarse clock produces."""
    import shutil

    staged = stems_dir.parent / "staged" / stable_id
    shutil.copytree(stems_dir / stable_id, staged)
    for path in (stems_dir / stable_id).iterdir():
        os.utime(staged / path.name, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
    bass = staged / "bass.wav"
    before = bass.stat()
    with bass.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        last = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([last[0] ^ 0xFF]))
    os.utime(bass, ns=(before.st_atime_ns, before.st_mtime_ns))
    return staged
