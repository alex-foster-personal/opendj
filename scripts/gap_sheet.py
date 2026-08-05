"""Push the missing-by-playcount gap list to a Google Sheet for choice input.

Mini-PRD
--------
R1 ✔︎ Read data/reconcile/missing-by-playcount.csv (produced by
     missing_by_playcount.py) and enrich each row with purchase deep-links,
     reusing SOURCE_TEMPLATES from apps.spotify.acquisition so the sheet and
     the in-repo acquisition queue can never drift apart.
     [if acquisition.SOURCE_TEMPLATES gains a store then the sheet gains a
      column without editing this file ⛔️]
R2 ✔︎ Prepend a 'buy' choice column the maintainer fills in, and freeze the header row.
     [if the sheet opens then row 1 is frozen and 'buy' is the first column ⛔️]
R3 ✔︎ Create the sheet via gws (already authed) and print its URL. Fail fast if
     gws is missing or the CSV has not been generated yet.
     [if the CSV is absent then exit nonzero naming the script to run ⛔️]

Usage::

    uv run --no-sync python scripts/gap_sheet.py [--limit N] [--min-plays N]
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.spotify.acquisition import SOURCE_TEMPLATES  # noqa: E402

# ----- config -------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
IN_CSV: Path = REPO_ROOT / "data" / "reconcile" / "missing-by-playcount.csv"
SHEET_TITLE: str = "DJ library gaps -- most-played tracks missing files"

BASE_COLUMNS: tuple[str, ...] = ("buy", "plays", "rating", "title", "artist", "status")


# ----- helpers ------------------------------------------------------------


def _require_gws() -> str:
    exe = shutil.which("gws")
    if not exe:
        sys.exit("[ERROR] gws not on PATH -- cannot create the sheet")
    return exe


def _load_rows(csv_path: Path, min_plays: int, limit: int) -> list[dict[str, str]]:
    if not csv_path.exists():
        sys.exit(
            f"[ERROR] {csv_path} missing -- run scripts/missing_by_playcount.py first"
        )
    with csv_path.open() as fh:
        rows = [r for r in csv.DictReader(fh) if int(r["plays"]) >= min_plays]
    return rows[:limit]


def _store_links(artist: str, title: str) -> list[str]:
    q = quote_plus(f"{artist} {title}".strip())
    return [template.format(q=q) for _name, _key, template in SOURCE_TEMPLATES]


def _build_grid(rows: list[dict[str, str]]) -> list[list[str]]:
    store_names = [name for name, _key, _t in SOURCE_TEMPLATES]
    grid: list[list[str]] = [list(BASE_COLUMNS) + store_names]
    for r in rows:
        links = _store_links(r["artist"], r["title"])
        cells = [
            "",  # buy -- the maintainer fills this in
            r["plays"],
            r["rating"],
            r["title"],
            r["artist"],
            r["status"],
        ]
        # HYPERLINK keeps the store name readable instead of a raw URL wall.
        cells += [
            f'=HYPERLINK("{url}","{name}")'
            for url, name in zip(links, store_names, strict=True)
        ]
        grid.append(cells)
    return grid


def _create_sheet(exe: str, grid: list[list[str]]) -> str:
    body = {
        "properties": {"title": SHEET_TITLE},
        "sheets": [
            {
                "properties": {
                    "title": "gaps",
                    "gridProperties": {"frozenRowCount": 1},
                },
                "data": [
                    {
                        "startRow": 0,
                        "startColumn": 0,
                        "rowData": [
                            {
                                "values": [
                                    {
                                        "userEnteredValue": (
                                            {"formulaValue": c}
                                            if c.startswith("=")
                                            else {"stringValue": c}
                                        )
                                    }
                                    for c in row
                                ]
                            }
                            for row in grid
                        ],
                    }
                ],
            }
        ],
    }
    proc = subprocess.run(
        [exe, "sheets", "spreadsheets", "create", "--json", json.dumps(body)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"[ERROR] gws create failed:\n{proc.stderr[:800]}")
    return json.loads(proc.stdout).get("spreadsheetUrl", "(no url returned)")


# ----- main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--min-plays", type=int, default=2)
    args = ap.parse_args()

    exe = _require_gws()
    rows = _load_rows(IN_CSV, args.min_plays, args.limit)
    if not rows:
        sys.exit(f"[ERROR] no rows with >= {args.min_plays} plays in {IN_CSV}")
    grid = _build_grid(rows)
    url = _create_sheet(exe, grid)

    print(f"[OK] rows: {len(rows)} (>= {args.min_plays} plays, limit {args.limit})")
    print(f"[OK] stores per row: {len(SOURCE_TEMPLATES)}")
    print(f"[OK] sheet: {url}")


if __name__ == "__main__":
    main()
