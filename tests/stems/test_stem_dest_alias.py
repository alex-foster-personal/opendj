"""stem_split_runner names the asset-store dest neutrally; ``bifrost2`` is a deprecated alias (#5639).

A host name is not a CLI value (issue #5639, batch 3): ``--dest asset-store`` is the
scp destination, whose host is ``MDT_B2STORE_SSH_HOST``. The old spelling still
works, with a warning. modal_vocal_farm keeps its own ``bifrost2`` spelling for now
(renaming it there is a stems-pipeline change behind the perfbatch gate), so the
runner translates when it builds the farm command.

Regression one-liners:
  - if the default dest is not asset-store then broken
  - if --dest bifrost2 does not run as asset-store with a warning then broken
  - if the farm command names a dest the farm does not accept then broken
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


@pytest.mark.parametrize(
    ("dest", "farm_dest"),
    [(NEUTRAL, ALIAS), ("local", "local"), ("r2", "r2"), ("none", "none")],
)
def test_farm_command_spells_each_dest_the_farm_way(dest: str, farm_dest: str) -> None:
    """[if] the farm argv names a dest other than the farm's spelling [then] fail, [else stop]."""
    cmd = ssr.farm_command(ssr.ARMS[0], ["sid-1"], dest, dry_run=False)
    assert cmd[cmd.index("--stems-dest") + 1] == farm_dest


def test_every_runner_dest_is_one_the_farm_accepts(farm: ModuleType) -> None:
    """[if] a runner dest maps to a value the farm rejects [then] fail, [else stop]."""
    assert set(ssr.FARM_STEMS_DEST) == set(ssr.DESTS)
    assert set(ssr.FARM_STEMS_DEST.values()) == set(farm.STEM_DESTS)
    assert ssr.FARM_STEMS_DEST[ssr.DEFAULT_DEST] == farm.DEFAULT_STEM_DEST
