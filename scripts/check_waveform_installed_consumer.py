#!/usr/bin/env python3
"""Prove the installed distribution selects native code on locked real ANLZ."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.shared.hashing import sha256_file
from apps.webui.server import rb_vendor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    installed_module = Path(rb_vendor.__file__).resolve()
    if installed_module.is_relative_to(source_root / "apps"):
        raise SystemExit(f"rb_vendor imported from source tree: {installed_module}")

    status = rb_vendor.waveform_materialization_status()
    if status != {
        "requested": "native",
        "selected": "rust-pyo3",
        "native_available": True,
        "native_import_error": None,
    }:
        raise SystemExit(f"native backend was not activated: {status}")

    manifest = json.loads(args.manifest.read_text())
    if manifest.get("contract_version") != 1:
        raise SystemExit("unsupported waveform fixture manifest")
    expected = manifest["files"]
    actual = {name: sha256_file(args.fixture / name) for name in sorted(expected)}
    if actual != expected:
        raise SystemExit("canonical waveform fixture checksum mismatch")

    tags, unreadable = rb_vendor._first_tags(args.fixture)
    if unreadable:
        raise SystemExit(f"canonical waveform fixture did not parse: {unreadable}")
    for tag_name, points in (("PWV6", 100), ("PWV7", 38_400)):
        bands = rb_vendor._tri_bands(tags[tag_name])
        expected_payload = rb_vendor._bands_payload_python(bands, points)
        actual_payload = rb_vendor._bands_payload(bands, points)
        if actual_payload != expected_payload:
            raise SystemExit(f"native payload differs from Python oracle for {tag_name}")

    native_path = Path(rb_vendor._WAVEFORM_NATIVE.__file__).resolve()
    print(f"installed rb_vendor: {installed_module}")
    print(f"installed native extension: {native_path}")
    print("locked real ANLZ parity: PWV6, PWV7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
