#!/usr/bin/env python3
"""Derive catalog status.midi/diagram/checksum from devices/<id>/ on disk.

Device PRs only add files under their own devices/<id>/ directory; the
catalog-sync PR runs `--write` once to fold those files into controllers.json,
so the catalog stops being a single-writer hotspot. `--check` exits 1 on drift.
Offline only: reads local files and runs checksum.py in-process.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import checksum  # noqa: E402

_ROOT = _SCRIPTS.parent
DEFAULT_DEVICES = _ROOT / "devices"
DEFAULT_CATALOG = _ROOT / "catalog" / "controllers.json"
DERIVED_KEYS = ("midi", "diagram", "checksum")


def checksum_status(device_dir: Path) -> str:
    """Return ok/fail from checksum.py, or pending when midi.json or layout.json is missing."""
    if not (device_dir / "midi.json").is_file() or not (device_dir / "layout.json").is_file():
        return "pending"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = checksum.main(device_dir)
    except Exception:  # noqa: BLE001 - a malformed device file is a failed checksum
        return "fail"
    return "ok" if rc == 0 else "fail"


def derive_status(device_dir: Path) -> dict[str, str]:
    """Return the derived midi/diagram/checksum status for one device directory."""
    midi = "ok" if (device_dir / "midi.json").is_file() else "pending"
    has_diagram = (device_dir / "layout.json").is_file() and (device_dir / "deck.html").is_file()
    return {
        "midi": midi,
        "diagram": "ok" if has_diagram else "pending",
        "checksum": checksum_status(device_dir),
    }


def drift(catalog: dict[str, Any], devices_dir: Path) -> list[str]:
    """List `id.key: stored -> derived` lines where the catalog disagrees with disk."""
    out: list[str] = []
    for ctrl in catalog.get("controllers", []):
        status = ctrl.get("status", {})
        for key, value in derive_status(devices_dir / ctrl["id"]).items():
            if status.get(key) != value:
                out.append(f"{ctrl['id']}.{key}: {status.get(key)} -> {value}")
    return out


def apply(catalog: dict[str, Any], devices_dir: Path) -> None:
    """Overwrite only status.midi, status.diagram and status.checksum in place."""
    for ctrl in catalog.get("controllers", []):
        ctrl.setdefault("status", {}).update(derive_status(devices_dir / ctrl["id"]))


def main(argv: list[str] | None = None) -> int:
    """CLI entry: --write rewrites the catalog, --check exits 1 on drift."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite derived status fields")
    mode.add_argument("--check", action="store_true", help="exit 1 when the catalog drifts from disk")
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--devices", type=Path, default=DEFAULT_DEVICES)
    args = ap.parse_args(argv)

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    diffs = drift(catalog, args.devices)
    if args.check:
        for line in diffs:
            print(line)
        print(f"catalog_status: {len(diffs)} drifted field(s)")
        return 1 if diffs else 0
    apply(catalog, args.devices)
    args.catalog.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"catalog_status: wrote {len(diffs)} field change(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
