"""The real-stick acceptance gate fails closed.

Regression lines:
  - if a missing stick skips without the explicit opt-out then the USB feature gate
    goes green without ever reading a real Pioneer export
  - if the opt-out still fails then a machine that knowingly has no stick cannot run the suite
  - if a mount without an export.pdb is accepted then the live tests read nothing
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.sync.usb.live_stick import (
    ALLOW_MISSING_ENV,
    ALLOW_MISSING_STICK_ENV,
    STICK_ENV,
    live_stick_root,
)
from tests.sync.usb.synthetic_stick import write_synthetic_stick

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
OPT_OUTS = (ALLOW_MISSING_STICK_ENV, ALLOW_MISSING_ENV)


def test_an_unset_stick_fails_instead_of_skipping() -> None:
    with pytest.raises(pytest.fail.Exception) as failure:
        live_stick_root({})
    assert STICK_ENV in str(failure.value)
    assert ALLOW_MISSING_STICK_ENV in str(failure.value), "the failure names the opt-out"


@pytest.mark.parametrize("opt_out", OPT_OUTS)
def test_the_explicit_opt_out_prints_its_reason_and_skips(
    opt_out: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(pytest.skip.Exception) as skipped:
        live_stick_root({opt_out: "1"})
    reason = str(skipped.value)
    assert reason.startswith("UNAVAILABLE: ") and STICK_ENV in reason and opt_out in reason
    assert capsys.readouterr().out == f"[SKIP] {reason}\n", "the reason is printed, not only raised"


@pytest.mark.parametrize("opt_out", OPT_OUTS)
def test_only_the_exact_opt_out_value_skips(opt_out: str) -> None:
    for value in ("", "0", "true", "yes"):
        with pytest.raises(pytest.fail.Exception):
            live_stick_root({opt_out: value})


@pytest.mark.parametrize("workflow", ["ci.yml", "full-ci.yml"])
def test_ci_opts_out_of_the_stick_alone(workflow: str) -> None:
    """if CI drops the stick opt-out then every runner goes red on a stick it cannot have;
    if CI sets the repo-wide opt-out then every other required fixture may skip there"""
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    top_level_env = re.search(r"^env:\n((?:  .*\n|\n)+)", text, re.MULTILINE)
    assert top_level_env is not None, f"{workflow} has no workflow-level env block"
    assert f'  {ALLOW_MISSING_STICK_ENV}: "1"\n' in top_level_env.group(1)
    assigned = re.findall(rf"^\s*{ALLOW_MISSING_ENV}\s*[:=]", text, re.MULTILINE)
    assert assigned == [], f"{workflow} sets the repo-wide fixture opt-out"


def test_a_named_mount_without_an_export_fails_even_with_the_opt_out(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception) as failure:
        live_stick_root({STICK_ENV: str(tmp_path), ALLOW_MISSING_ENV: "1"})
    assert "export.pdb" in str(failure.value)


def test_a_mount_with_an_export_is_returned(tmp_path: Path) -> None:
    volumes = tmp_path / "host" / "Volumes"
    volumes.mkdir(parents=True)
    mount = write_synthetic_stick(volumes)
    assert live_stick_root({STICK_ENV: str(mount)}) == mount
