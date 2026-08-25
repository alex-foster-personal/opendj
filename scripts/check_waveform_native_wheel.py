#!/usr/bin/env python3
"""Fail closed unless the native companion wheel carries the promised ABI."""

from __future__ import annotations

import importlib.machinery
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_waveform_native_wheel.py WHEEL")
    wheel = Path(sys.argv[1])
    minimum_python_tag = "cp311"
    parts = wheel.stem.split("-")
    if len(parts) < 5:
        raise SystemExit(f"invalid wheel filename: {wheel.name}")
    python_tag, abi_tag, platform_tag = parts[-3:]
    print(f"waveform native wheel tags: python={python_tag} abi={abi_tag} platform={platform_tag}")
    if python_tag != minimum_python_tag or abi_tag != "abi3":
        raise SystemExit(
            f"expected {minimum_python_tag}-abi3 wheel for Python 3.11+; "
            f"got {python_tag}-{abi_tag}: {wheel.name}"
        )
    if platform_tag == "any":
        raise SystemExit(f"native extension wheel cannot be platform-independent: {wheel.name}")

    # A wheel inspection must prove it contains the collision-resistant native
    # module, not merely trust maturin's filename.
    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit(f"wheel must contain exactly one METADATA file: {wheel.name}")
        metadata = archive.read(metadata_names[0]).decode()
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    if not any(
        Path(member).name.startswith("_rb_waveform_native.") and member.endswith(suffixes)
        for member in members
    ):
        raise SystemExit(f"wheel lacks _rb_waveform_native extension: {wheel.name}")
    if "Requires-Dist: numpy<2" not in metadata:
        raise SystemExit(f"wheel does not declare its NumPy runtime dependency: {wheel.name}")
    print(f"waveform native wheel payload: _rb_waveform_native ({wheel.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
