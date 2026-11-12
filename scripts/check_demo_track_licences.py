#!/usr/bin/env python3
"""Build check: bundled demo tracks must match the licence manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apps.onboarding import contract as demo_tracks


def _run_check(bundle_root: Path, manifest_path: Path) -> int:
    errors = demo_tracks.verify_demo_track_licences(
        bundle_root=bundle_root,
        manifest_path=manifest_path,
    )
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    media = demo_tracks.bundled_media_paths(bundle_root)
    print(
        f"demo-track licence manifest ok ({len(media)} bundled "
        f"track{'s' if len(media) != 1 else ''}, {len(demo_tracks.load_manifest(manifest_path).tracks)} entries)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle-root",
        type=Path,
        default=demo_tracks.BUNDLE_ROOT,
        help="Directory holding bundled demo media (default: apps/onboarding/demo-tracks)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=demo_tracks.MANIFEST_PATH,
        help="Licence manifest path (default: apps/onboarding/demo-tracks.manifest.json)",
    )
    args = parser.parse_args(argv)
    return _run_check(args.bundle_root, args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
