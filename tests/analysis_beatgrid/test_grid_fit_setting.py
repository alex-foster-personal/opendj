"""NATIVE-19: the grid fitter is a config setting, const_regions by default.

[if] neither MDT_BEATGRID_GRID_FIT nor the config names a mode [then] the lane
serves const_regions; [if] the config names one [then] that one; [if] the env
names one [then] it wins over the config; [if] either names an unknown mode
[then] it raises naming the source, rather than serving a mode nobody chose.

-Claude
"""

from __future__ import annotations

import re

import pytest

from apps.analysis import config as analysis_config
from apps.analysis.backends.own_beatgrid import GRID_FIT_ENV, grid_fit_mode


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(GRID_FIT_ENV, raising=False)


def test_shipped_config_serves_const_regions():
    assert analysis_config.load_config()["own_beatgrid"]["grid_fit"] == "const_regions"
    assert grid_fit_mode() == "const_regions"


def test_missing_section_falls_back_to_const_regions():
    assert grid_fit_mode({}) == "const_regions"


@pytest.mark.parametrize("mode", ["raw", "line", "const_regions"])
def test_config_names_the_mode(mode):
    assert grid_fit_mode({"own_beatgrid": {"grid_fit": mode}}) == mode


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv(GRID_FIT_ENV, "raw")
    assert grid_fit_mode({"own_beatgrid": {"grid_fit": "const_regions"}}) == "raw"


def test_unknown_config_value_raises():
    with pytest.raises(ValueError, match=re.escape("config.yaml")):
        grid_fit_mode({"own_beatgrid": {"grid_fit": "constant"}})


def test_unknown_env_value_raises(monkeypatch):
    monkeypatch.setenv(GRID_FIT_ENV, "lines")
    with pytest.raises(ValueError, match=GRID_FIT_ENV):
        grid_fit_mode({"own_beatgrid": {"grid_fit": "raw"}})
