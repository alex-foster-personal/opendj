"""The real-stick acceptance gate fails closed.

Regression lines:
  - if a missing stick skips without the explicit opt-out then the USB feature gate
    goes green without ever reading a real Pioneer export
  - if the opt-out still fails then a machine that knowingly has no stick cannot run the suite
  - if a mount without an export.pdb is accepted then the live tests read nothing
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.sync.usb.live_stick import ALLOW_MISSING_ENV, STICK_ENV, live_stick_root
from tests.sync.usb.synthetic_stick import write_synthetic_stick


def test_an_unset_stick_fails_instead_of_skipping() -> None:
    with pytest.raises(pytest.fail.Exception) as failure:
        live_stick_root({})
    assert STICK_ENV in str(failure.value)
    assert ALLOW_MISSING_ENV in str(failure.value), "the failure names the explicit opt-out"


def test_the_explicit_opt_out_reports_unavailable_as_a_skip() -> None:
    with pytest.raises(pytest.skip.Exception) as skipped:
        live_stick_root({ALLOW_MISSING_ENV: "1"})
    assert "UNAVAILABLE" in str(skipped.value)


def test_only_the_exact_opt_out_value_skips() -> None:
    for value in ("", "0", "true", "yes"):
        with pytest.raises(pytest.fail.Exception):
            live_stick_root({ALLOW_MISSING_ENV: value})


def test_a_named_mount_without_an_export_fails_even_with_the_opt_out(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception) as failure:
        live_stick_root({STICK_ENV: str(tmp_path), ALLOW_MISSING_ENV: "1"})
    assert "export.pdb" in str(failure.value)


def test_a_mount_with_an_export_is_returned(tmp_path: Path) -> None:
    volumes = tmp_path / "host" / "Volumes"
    volumes.mkdir(parents=True)
    mount = write_synthetic_stick(volumes)
    assert live_stick_root({STICK_ENV: str(mount)}) == mount
