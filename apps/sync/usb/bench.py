"""Throwaway benchmark for USB verify throughput.

Not part of the pytest suite. Invoke manually::

    python -m apps.sync.usb.bench --profile ... --drive-root /Volumes/GIG-A
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from apps.shared.hashing import HashCache
from apps.sync.usb import profile as profile_mod
from apps.sync.usb.state import load_canonical_tracks
from apps.sync.usb.verify import verify_drive


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--drive-root", required=True)
    args = p.parse_args()

    profile = profile_mod.load(args.profile)
    canonical = load_canonical_tracks(playlist_names=profile.playlists)

    cache = HashCache(Path("data/usb/hashes.sqlite"))
    t0 = time.monotonic()
    report = verify_drive(
        profile=profile,
        canonical=canonical,
        drive_root=Path(args.drive_root),
        hash_cache=cache,
    )
    elapsed = time.monotonic() - t0
    total_bytes = sum(
        (Path(args.drive_root) / f.actual_path).stat().st_size
        for f in report.files
        if f.actual_path and f.status.value in ("ok", "corrupted", "renamed")
    )
    mb = total_bytes / (1024 * 1024)
    print(f"hashed {mb:.1f} MB in {elapsed:.2f}s = {mb / elapsed:.1f} MB/s")
    print(f"drift = {report.drift_count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
