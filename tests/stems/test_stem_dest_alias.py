"""stem_split_runner names the asset-store dest neutrally; ``bifrost2`` is a deprecated alias (#5639).

A host name is not a CLI value (issue #5639, batch 3): ``--dest asset-store`` is the
scp destination, whose host is ``MDT_B2STORE_SSH_HOST``. The old spelling still
works, with a warning. modal_vocal_farm --stems-dest uses the same neutral values
(its rename rode the perfbatch CLI-value-rename rule), so the runner passes the
dest through unchanged.

Regression one-liners:
  - if the default dest is not asset-store then broken
  - if --dest bifrost2 does not run as asset-store with a warning then broken
  - if the farm command names a dest the farm does not accept then broken
  - if the farm still accepts the old bifrost2 spelling then broken
"""

from __future__ import annotations

from types import ModuleType

import pytest

from scripts import stem_split_runner as ssr

NEUTRAL = "asset-store"
ALIAS = "bifrost2"


@pytest.fixture(scope="module")
def farm() -> ModuleType:
    pytest.importorskip("modal", reason="needs the optional modal package")
    import scripts.modal_vocal_farm as farm_module

    return farm_module


def test_runner_defaults_to_the_neutral_dest() -> None:
    """[if] stem_split_runner's default dest is not asset-store [then] fail, [else stop]."""
    assert ssr.DEFAULT_DEST == NEUTRAL
    assert ALIAS not in ssr.DESTS
    assert ssr.build_parser().parse_args([]).dest == NEUTRAL


def test_alias_runs_as_the_neutral_dest_with_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] --dest bifrost2 is not asset-store plus a warning [then] fail, [else stop]."""
    parser = ssr.build_parser()
    capsys.readouterr()
    assert parser.parse_args(["--dest", ALIAS]).dest == NEUTRAL
    assert f"--dest {ALIAS} is deprecated, use {NEUTRAL}" in capsys.readouterr().err
    assert parser.parse_args(["--dest", NEUTRAL]).dest == NEUTRAL
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit):
        parser.parse_args(["--dest", "somewhere-else"])


@pytest.mark.parametrize("dest", [NEUTRAL, "local", "r2", "none"])
def test_farm_command_passes_the_dest_through(dest: str) -> None:
    """[if] the farm argv names a dest other than the runner's [then] fail, [else stop]."""
    cmd = ssr.farm_command(ssr.ARMS[0], ["sid-1"], dest, dry_run=False)
    assert cmd[cmd.index("--stems-dest") + 1] == dest


def test_runner_and_farm_accept_the_same_dests(farm: ModuleType) -> None:
    """[if] the runner and the farm disagree on a dest or the default [then] fail, [else stop]."""
    assert set(ssr.DESTS) == set(farm.STEM_DESTS)
    assert ssr.DEFAULT_DEST == farm.DEFAULT_STEM_DEST == NEUTRAL
    assert ALIAS not in farm.STEM_DESTS


def test_farm_rejects_the_old_spelling(farm: ModuleType) -> None:
    """[if] modal_vocal_farm --stems-dest bifrost2 still parses [then] fail, [else stop]."""
    parser = farm.build_parser()
    assert parser.parse_args([]).stems_dest == NEUTRAL
    assert parser.parse_args(["--stems-dest", NEUTRAL]).stems_dest == NEUTRAL
    with pytest.raises(SystemExit):
        parser.parse_args(["--stems-dest", ALIAS])
