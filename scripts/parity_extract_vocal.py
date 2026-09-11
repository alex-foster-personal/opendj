#!/usr/bin/env python3
"""Extract PARITY-01 vocal lane fields from rekordbox ANLZ (.2EX PVDI).

Uses apps.vocals.cli.pvdi_present for the fourcc probe (PMAI seek-walk).
Never calls pyrekordbox for PVDI. Decodes regions via read_pvdi + vocal_regions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from apps.shared.paths import DATA_DIR
from apps.shared.platform_paths import load_path_map, resolve_asset_path
from apps.vocals.cli import Ctx, load_tracks, pvdi_present
from apps.webui.server.rb_vendor_pkg.anlz import _vocal_regions, read_pvdi

_TINY_FIXTURE = Path("/opt/mdt-fixtures/rekordbox/master.plain.db")


def _refuse_master(master: Path, *, allow_tiny: bool) -> None:
    if not master.is_file():
        raise SystemExit(f"error: master db missing: {master}")
    if master.stat().st_size == 0:
        raise SystemExit(f"error: master db is empty (0 bytes): {master}")
    if not allow_tiny and master.resolve() == _TINY_FIXTURE.resolve():
        raise SystemExit(
            "error: refusing pruned fixture master.plain.db; pass --allow-tiny for tests"
        )


def _decode_rb_regions(twoex: Path) -> list[dict[str, float]]:
    decoded = read_pvdi(twoex)
    if decoded is None:
        return []
    fps, envelope = decoded
    regions = _vocal_regions(envelope, fps)
    return [
        {"start_s": float(item["start_s"]), "end_s": float(item["end_s"])}
        for item in regions
    ]


def extract_vocal_row(
    track_row: dict[str, Any],
) -> dict[str, Any]:
    """Build vocal payload fields for one present track."""
    stable_id = track_row["stable_id"]
    present = bool(track_row.get("present"))
    out: dict[str, Any] = {
        "stable_id": stable_id,
        "present": present,
        "rb_pvdi": False,
        "rb_vocal_regions": None,
        "own_vocal_regions": track_row.get("own_vocal_regions"),
        "duration_s": track_row.get("duration_s"),
    }
    if not present:
        return out

    adp = track_row.get("analysis_data_path")
    if not adp:
        return out

    mapped = resolve_asset_path(str(adp))
    if mapped.resolved is None or not mapped.resolved.is_file():
        return out

    twoex = mapped.resolved.with_suffix(".2EX")
    if not twoex.is_file() or not pvdi_present(twoex):
        return out

    out["rb_pvdi"] = True
    out["rb_vocal_regions"] = _decode_rb_regions(twoex)
    return out


def _load_own_outputs(data_dir: Path) -> dict[str, dict[str, Any]]:
    bench = data_dir / ".tmp" / "parity" / "vocal"
    if not bench.is_dir():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for path in bench.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        sid = payload.get("stable_id")
        if isinstance(sid, str):
            out[sid] = payload
    return out


def extract_library(
    *,
    data_dir: Path,
    master: Path,
    limit: int | None,
    allow_tiny: bool,
) -> dict[str, Any]:
    _refuse_master(master, allow_tiny=allow_tiny)
    own_by_id = _load_own_outputs(data_dir)
    load_path_map(data_dir)

    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, playlist=None)
    rows: list[dict[str, Any]] = []
    for tr in tracks:
        audio_present = tr.audio_on_disk
        own_payload = own_by_id.get(tr.stable_id, {})
        row = {
            "stable_id": tr.stable_id,
            "present": audio_present,
            "analysis_data_path": tr.analysis_data_path,
            "own_vocal_regions": own_payload.get("own_vocal_regions"),
            "duration_s": own_payload.get("duration_s"),
        }
        rows.append(extract_vocal_row(row))
        if limit is not None and len(rows) >= limit:
            break

    return {
        "schema": 1,
        "measured_at": "NOT MEASURED",
        "parity_round": 1,
        "tracks": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract PARITY-01 vocal lane fields")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--master", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--allow-tiny", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    data_dir = args.data_dir
    master = args.master or (data_dir / "master.plain.db")
    if args.state and args.state != (data_dir / "state" / "state.db"):
        raise SystemExit(
            "error: --state override is not supported; set --data-dir instead"
        )

    payload = extract_library(
        data_dir=data_dir,
        master=master,
        limit=args.limit,
        allow_tiny=args.allow_tiny,
    )
    text = json.dumps(payload, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
