"""Refuse a staged engine payload that contains the GPLv3 rbox package.

The lock audit (NEVER_SHIP and the omitted usb-export extra) is not enough
on its own: a later copy step can still drop the wheel into the tree
``verify()`` is about to bless. Issue #5143.
"""
from __future__ import annotations

from pathlib import Path


class PayloadRboxError(RuntimeError):
    """The staged payload contains the GPLv3 rbox package."""


def assert_rbox_absent(payload_dir: Path) -> None:
    """Fail when staged bytes include the rbox package or its dist-info."""
    offenders = sorted(
        path.relative_to(payload_dir)
        for path in payload_dir.rglob("*")
        if path.is_dir()
        and (
            path.name == "rbox"
            or (path.name.startswith("rbox-") and path.name.endswith(".dist-info"))
        )
    )
    if offenders:
        listing = "\n  ".join(str(path) for path in offenders)
        raise PayloadRboxError(
            "rbox is GPLv3 and must not ship in the Apache-2.0 desktop "
            f"payload; found:\n  {listing}"
        )
