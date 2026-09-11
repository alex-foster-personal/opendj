"""Build the LIBUX-06 library-wheel e2e fixture.

Writes a throwaway ``MDT_DATA_DIR`` with a real ``state/state.db`` (via
``apply_migrations``) and ``master.plain.db``, populated with the same 5-track
genre library ``tests.webui.library_wheel_fixtures.wheel_dbs`` uses. The root
Playwright deckload fixture has no rekordbox vendor mapping and no
``master.plain.db``, so ``GET /api/v1/library/wheel`` returns 503 there; this
builder exists so the dedicated suite can render the real library grouped by
genre.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.library_wheel_fixture \
        --data-dir /abs/path/to/.tmp/library-wheel-e2e-data

``--data-dir`` must be absolute. Idempotent: the directory is wiped and rebuilt
on every call.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tests.webui.library_wheel_fixtures import _make_master_db, _make_state_db


def build(data_dir: Path) -> None:
    """Wipe ``data_dir`` and write the 5-track genre-family library into it."""
    if data_dir.exists():
        shutil.rmtree(data_dir)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)

    tracks = [
        {"stable_id": "t-techno-1", "title": "Techno One", "artists": ["Artist A"]},
        {"stable_id": "t-techno-2", "title": "Techno Two", "artists": ["Artist B"]},
        {"stable_id": "t-house-1", "title": "House One", "artists": ["Artist C"]},
        {"stable_id": "t-unmatched-1", "title": "Spoken One", "artists": ["Artist D"]},
        {"stable_id": "t-no-rb-1", "title": "Local Only", "artists": ["Artist E"]},
    ]
    vendor_ids = {
        "t-techno-1": "v-1",
        "t-techno-2": "v-2",
        "t-house-1": "v-3",
        "t-unmatched-1": "v-4",
        # t-no-rb-1 deliberately has no rekordbox mapping.
    }
    playlists = [{"playlist_id": "pl-1", "name": "Warmup"}]
    memberships = [
        {"playlist_id": "pl-1", "stable_id": "t-techno-1"},
        {"playlist_id": "pl-1", "stable_id": "t-house-1"},
    ]
    _make_state_db(
        state_dir / "state.db",
        tracks=tracks,
        memberships=memberships,
        playlists=playlists,
        vendor_ids=vendor_ids,
    )
    _make_master_db(
        data_dir / "master.plain.db",
        content=[
            {"vendor_id": "v-1", "genre_id": "g-techno", "play_count": 40},
            {"vendor_id": "v-2", "genre_id": "g-techno", "play_count": 10},
            {"vendor_id": "v-3", "genre_id": "g-house", "play_count": 5},
            {"vendor_id": "v-4", "genre_id": "g-unmatched", "play_count": 1},
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="library_wheel_fixture",
        description="Build the library-wheel e2e genre-family fixture library.",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="absolute path of the throwaway fixture data dir",
    )
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}")
    build(data_dir)
    print(f"[OK] library-wheel fixture at {data_dir} with 5 tracks, 2 families")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
