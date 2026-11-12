"""The Cmd K ttx fixture builder wipes only its own data dir.

[if] the builder accepts any absolute ``--data-dir`` then a typo, ``/``, a
repository or a home directory reaches ``shutil.rmtree`` and is deleted.
The guard refuses everything but the e2e fixtures dir's own ``cmdk-ttx-data``,
before anything is removed.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

FIXTURE_MODULE = "apps.webui.frontend.tests.e2e.support.cmdk_ttx_fixture"


def test_the_guard_accepts_only_the_named_fixture_dir() -> None:
    fixture = importlib.import_module(FIXTURE_MODULE)
    expected = fixture.FIXTURES_ROOT / fixture.DATA_DIR_NAME
    assert fixture.checked_data_dir(expected) == expected
    for refused in (Path("/"), Path.home(), fixture.FIXTURES_ROOT, fixture.FIXTURES_ROOT / "other-data"):
        with pytest.raises(SystemExit, match="refusing to wipe"):
            fixture.checked_data_dir(refused)
    with pytest.raises(SystemExit, match="must be absolute"):
        fixture.checked_data_dir(Path("cmdk-ttx-data"))


def test_a_refused_dir_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    fixture = importlib.import_module(FIXTURE_MODULE)
    target = tmp_path / fixture.DATA_DIR_NAME
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_text("still here", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to wipe"):
        fixture.main(["--data-dir", str(target)])
    assert keep.read_text(encoding="utf-8") == "still here"
