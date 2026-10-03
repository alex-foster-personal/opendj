"""NATIVE-19: the grid fitter is a config setting, const_regions by default.

[if] neither MDT_BEATGRID_GRID_FIT nor the config names a mode [then] the lane
serves const_regions; [if] the config names one [then] that one; [if] the env
names one [then] it wins over the config; [if] either names an unknown mode
[then] it raises naming the source, rather than serving a mode nobody chose.

-Claude
"""

from __future__ import annotations

import re
import os
import subprocess
import sys

import pytest

from apps.analysis import config as analysis_config
from apps.analysis.backends.grid_fit_setting import GRID_FIT_ENV, grid_fit_mode


def _in_process(code: str, override: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the production setting in a fresh interpreter with a real environment."""
    environment = dict(os.environ)
    environment.pop(GRID_FIT_ENV, None)
    if override is not None:
        environment[GRID_FIT_ENV] = override
    return subprocess.run(
        [sys.executable, "-c", code], env=environment, capture_output=True,
        text=True, timeout=30, check=False,
    )


def _result(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_shipped_config_serves_const_regions():
    assert analysis_config.load_config()["own_beatgrid"]["grid_fit"] == "const_regions"
    assert _result(_in_process(
        "from apps.analysis.backends.grid_fit_setting import grid_fit_mode; print(grid_fit_mode())"
    )) == "const_regions"


def test_missing_section_falls_back_to_const_regions():
    assert grid_fit_mode({}) == "const_regions"


@pytest.mark.parametrize("mode", ["raw", "line", "const_regions"])
def test_config_names_the_mode(mode):
    assert grid_fit_mode({"own_beatgrid": {"grid_fit": mode}}) == mode


def test_env_overrides_config():
    code = "from apps.analysis.backends.grid_fit_setting import grid_fit_mode; " + (
        "print(grid_fit_mode(" + repr({"own_beatgrid": {"grid_fit": "const_regions"}}) + "))"
    )
    assert _result(_in_process(code, "raw")) == "raw"


def test_unknown_config_value_raises():
    with pytest.raises(ValueError, match=re.escape("config.yaml")):
        grid_fit_mode({"own_beatgrid": {"grid_fit": "constant"}})


def test_unknown_env_value_raises():
    code = "from apps.analysis.backends.grid_fit_setting import grid_fit_mode; " + (
        "grid_fit_mode(" + repr({"own_beatgrid": {"grid_fit": "raw"}}) + ")"
    )
    result = _in_process(code, "lines")
    assert result.returncode != 0
    assert "ValueError:" in result.stderr
    assert GRID_FIT_ENV in result.stderr
