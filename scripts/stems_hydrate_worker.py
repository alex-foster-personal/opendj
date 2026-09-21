#!/usr/bin/env python3
"""Hydrate indexed stem bundles without ML compute (issue #2630).

Used by ``apps.stems.job.build_argv`` when every requested track is already
published in the stem bundle index. Emits the same stdout progress protocol
as ``scripts/stems_modal_worker.py`` so the engine can publish
``library.changed`` per track via ``stems_ready``.

Run directly:
  python scripts/stems_hydrate_worker.py --stable-id <id> [--data-dir DIR]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from apps.cloud.stem_source import StemSourceError
from apps.stems.hydrate_runner import HydrateRunnerError, hydrate_stable_ids


def _hydrate_ids(stable_ids: list[str], data_dir: Path) -> int:
    try:
        hydrate_stable_ids(stable_ids, data_dir, refresh_index=True)
    except StemSourceError as exc:
        raise SystemExit(f"error: {exc.code}: {exc.message}") from exc
    except HydrateRunnerError as exc:
        raise SystemExit(f"error: {exc}") from exc
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--stable-id", action="append", dest="stable_ids", default=[])
    args = parser.parse_args(argv)
    if not args.stable_ids:
        raise SystemExit("error: pass at least one --stable-id")
    data_dir = args.data_dir
    if data_dir is None:
        from apps.shared.platform_paths import DATA_DIR

        data_dir = DATA_DIR
    return _hydrate_ids(list(dict.fromkeys(args.stable_ids)), Path(data_dir))


if __name__ == "__main__":
    raise SystemExit(main())
