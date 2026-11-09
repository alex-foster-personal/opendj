# /// script
# requires-python = ">=3.11"
# dependencies = ["pyrekordbox"]
# ///
"""Dump rekordbox PQTZ beat grids to the JSON the beat-sync sim reads.

    uv run tools/beat-sync-sim/dump_grids.py OUT.json [--anlz-root DIR] name=anlz-subdir [name=anlz-subdir ...]

Each name becomes a key holding `[{"n": beat-in-bar, "bpm": float, "t": seconds}, ...]`,
the shape of the frontend's `AnlzBeat`. `out` is the master in `evalpairs.mjs`; every
other key is an incoming. The default ANLZ root is rekordbox's own share folder under
the current user's home; the subdir is the per-track folder that holds `ANLZ0000.DAT`.

Times are the raw PQTZ values. The app's `/anlz` endpoint shifts an MP3's grid by its encoder
lead-in (`on_our_timeline`) and drops beats before zero; apply the same shift before comparing
against positions read from the app. The lock statistics are unaffected by a per-track offset.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pyrekordbox.anlz import AnlzFile

DEFAULT_ANLZ_ROOT = Path.home() / "Library" / "Pioneer" / "rekordbox" / "share" / "PIONEER" / "USBANLZ"


def _grid(anlz_dat: Path) -> list[dict[str, float | int]]:
    beats, bpms, times = AnlzFile.parse_file(str(anlz_dat)).get_tag("PQTZ").get()
    return [{"n": int(b), "bpm": float(p), "t": float(x)} for b, p, x in zip(beats, bpms, times, strict=True)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_json", type=Path)
    parser.add_argument("--anlz-root", type=Path, default=DEFAULT_ANLZ_ROOT)
    parser.add_argument("pairs", nargs="+", help="name=anlz-subdir")
    args = parser.parse_args()
    grids: dict[str, list[dict[str, float | int]]] = {}
    for pair in args.pairs:
        name, subdir = pair.split("=", 1)
        anlz_dat = args.anlz_root / subdir / "ANLZ0000.DAT"
        if not anlz_dat.is_file():
            raise SystemExit(f"no ANLZ file at {anlz_dat}")
        grids[name] = _grid(anlz_dat)
        print(f"{name}: {len(grids[name])} beats, first {grids[name][:2]}")
    args.out_json.write_text(json.dumps(grids), encoding="utf-8")
    print(f"wrote {args.out_json}")


if __name__ == "__main__":
    main()
