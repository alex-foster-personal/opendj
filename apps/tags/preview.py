"""Dry-run preview for tag unification.

Writes ``data/tags/unified-preview.csv`` with one row per (track, field)
where the unified value differs from the current file / RB / djay value.
Never mutates any file. Idempotent.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from apps.shared import paths
from apps.shared.tag_writer import TagRead, UnifiedTags

from . import collect as tag_collect
from . import unify as tag_unify


def build_preview_rows(
    files: list[Path],
    *,
    fetch_rb=None,
    fetch_djay=None,
    fetch_mik=None,
) -> list[dict]:
    """Return list of CSV-ready dicts for ``files``."""
    rows: list[dict] = []
    for path in files:
        sources = tag_collect.collect_for(
            path, fetch_rb=fetch_rb, fetch_djay=fetch_djay, fetch_mik=fetch_mik
        )
        plan = tag_unify.unify(sources)
        file_tags = sources.file or TagRead()
        for field, prov in plan.provenance.items():
            new_val = getattr(plan.tags, field)
            cur_file = getattr(file_tags, field)
            rows.append(
                {
                    "path": str(path),
                    "field": field,
                    "current_file_value": "" if cur_file is None else cur_file,
                    "unified_value": "" if new_val is None else new_val,
                    "source": prov.source,
                    "confidence": prov.confidence,
                    "would_change_file": str(cur_file != new_val),
                }
            )
    return rows


def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "path",
        "field",
        "current_file_value",
        "unified_value",
        "source",
        "confidence",
        "would_change_file",
    ]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run_preview(
    *,
    paths_arg: list[Path],
    out: Path | None = None,
    fetch_rb=None,
    fetch_djay=None,
    fetch_mik=None,
) -> Path:
    out_path = out or paths.TAGS_UNIFIED_PREVIEW_CSV
    rows = build_preview_rows(
        paths_arg, fetch_rb=fetch_rb, fetch_djay=fetch_djay, fetch_mik=fetch_mik
    )
    write_csv(rows, out_path)
    return out_path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m apps.tags.preview")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--out", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    out = run_preview(paths_arg=args.paths, out=args.out)
    print(f"preview-written {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
