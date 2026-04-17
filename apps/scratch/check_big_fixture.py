"""Sanity-check the LaCie-hosted rb-usb-export-big fixture.

Runs the production reader against the 1586-track fixture and prints a
small summary so we can eyeball-verify that:

* the resolver found the fixture,
* ``read_usb_export`` can parse it end-to-end,
* the track / playlist counts are in the right ballpark (>=1500).

Usage
-----
.. code-block:: bash

    .venv/bin/python -m apps.scratch.check_big_fixture

Exits 0 on success, 1 if any invariant fails.
"""
from __future__ import annotations

import json
import sys

from apps.sync.usb.pioneer import read_usb_export
from apps.sync.usb.pioneer.reader import validate_invariants
from tests.fixtures._resolver import (
    FixtureNotAvailable,
    fixture_path,
)


MIN_TRACKS = 1500  # real export has 1586


def main() -> int:
    try:
        big = fixture_path("rb-usb-export-big")
    except (FileNotFoundError, FixtureNotAvailable) as exc:
        print(f"[!] Fixture not available: {exc}", file=sys.stderr)
        return 1

    pioneer = big / "PIONEER"
    if not pioneer.is_dir():
        print(f"[!] PIONEER/ missing under {big}", file=sys.stderr)
        return 1

    print(f"[i] Resolved: {big}")
    print(f"[i] Reading  : {pioneer}")
    data = read_usb_export(pioneer)
    meta = data["metadata"]

    # Invariants (same ones the small-fixture test enforces).
    errors = validate_invariants(data)

    summary = {
        "total_tracks": meta["total_tracks"],
        "total_playlists": meta["total_playlists"],
        "total_playlist_entries": meta["total_playlist_entries"],
        "total_artists": meta["total_artists"],
        "total_albums": meta["total_albums"],
        "has_pdb": meta["has_pdb"],
        "has_extended_pdb": meta["has_extended_pdb"],
        "has_onelibrary": meta["has_onelibrary"],
        "anlz_total_dirs": meta["anlz_total_dirs"],
        "anlz_tag_coverage": meta["anlz_tag_coverage"],
    }
    print("[i] Summary:")
    print(json.dumps(summary, indent=2, default=str))

    ok = True
    if meta["total_tracks"] < MIN_TRACKS:
        print(
            f"[!] total_tracks={meta['total_tracks']} < {MIN_TRACKS}",
            file=sys.stderr,
        )
        ok = False
    if not meta["has_pdb"]:
        print("[!] export.pdb missing", file=sys.stderr)
        ok = False
    if errors:
        print(f"[!] {len(errors)} invariant failures:", file=sys.stderr)
        for err in errors[:10]:
            print(f"    {err}", file=sys.stderr)
        ok = False

    if ok:
        print("[✓] Big fixture parses cleanly and passes all invariants.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
