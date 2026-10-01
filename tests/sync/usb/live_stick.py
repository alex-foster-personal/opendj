"""The real rekordbox stick the live USB acceptance tests read, fail closed.

A real stick cannot be committed: it is a private library plus third-party
artwork (see ``tests/fixtures/rb-usb-export.extern``). So its absence must be
loud. With ``MDT_USB_STICK_ROOT`` unset the live tests FAIL, the same contract
``tests/fixtures/conftest.py`` holds for every required external fixture. An
explicit opt-out reports the capability UNAVAILABLE as a skip instead, with
the reason printed and in the skip summary:

* ``MDT_ALLOW_MISSING_USB_STICK=1`` opts out of the stick ALONE. CI sets this
  one (``.github/workflows/ci.yml`` and ``full-ci.yml``): a runner has no
  stick, and the repo-wide variable below would also let every other required
  fixture skip there.
* ``MDT_ALLOW_MISSING_FIXTURES=1`` is the repo-wide opt-out and covers the
  stick too.

A stick that is named but has no export is always a failure: that is a wrong
path, not a missing host.

The stick is only ever read.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

STICK_ENV = "MDT_USB_STICK_ROOT"
ALLOW_MISSING_STICK_ENV = "MDT_ALLOW_MISSING_USB_STICK"
ALLOW_MISSING_ENV = "MDT_ALLOW_MISSING_FIXTURES"
EXPORT_PDB_PARTS = ("PIONEER", "rekordbox", "export.pdb")


def live_stick_root(environ: Mapping[str, str]) -> Path:
    """The mounted stick ``environ`` names, or a failure (a skip only on opt-out)."""
    raw = environ.get(STICK_ENV, "")
    if raw:
        root = Path(raw)
        if not root.joinpath(*EXPORT_PDB_PARTS).is_file():
            pytest.fail(f"{STICK_ENV}={raw!r} is set but has no {'/'.join(EXPORT_PDB_PARTS)}")
        return root
    detail = f"{STICK_ENV} is unset: no real rekordbox stick to read (set it to the mount path)."
    for opt_out in (ALLOW_MISSING_STICK_ENV, ALLOW_MISSING_ENV):
        if environ.get(opt_out) == "1":
            reason = f"UNAVAILABLE: {detail} ({opt_out}=1 set)"
            print(f"[SKIP] {reason}")
            pytest.skip(reason)
    pytest.fail(
        f"{detail} Set {ALLOW_MISSING_STICK_ENV}=1 to explicitly skip on a machine that "
        "knowingly has no stick; unset, this fails closed rather than silently dropping the "
        "real-stick acceptance coverage (AGENTS.md: never silently skip acceptance for "
        "missing data)."
    )
    raise AssertionError("pytest.fail returned instead of raising")
