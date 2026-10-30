"""The Cmd K cue-preview fixture builder wipes only its own data dir.

[if] the builder accepts any absolute ``--data-dir`` then a typo, ``/``, a
repository or a home directory reaches ``shutil.rmtree`` and is deleted. The
guard refuses everything but the e2e fixtures dir's own
``cmdk-preview-cue-data``, before anything is removed or downloaded.

Nothing is monkeypatched: the module is imported by name and its own guard
and ``main`` are called on real directories.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

FIXTURE_MODULE = "apps.webui.frontend.tests.e2e.support.cmdk_preview_cue_fixture"


def test_the_guard_accepts_only_the_named_fixture_dir() -> None:
    fixture = importlib.import_module(FIXTURE_MODULE)
    expected = fixture.FIXTURES_ROOT / fixture.DATA_DIR_NAME
    # Control: the path the Playwright config passes is accepted, unchanged.
    assert fixture.checked_data_dir(expected) == expected
    assert expected.parent.name == "fixtures" and expected.parent.parent.name == "e2e"
    for refused in (Path("/"), Path.home(), fixture.FIXTURES_ROOT, fixture.FIXTURES_ROOT / "other-data"):
        with pytest.raises(SystemExit, match="refusing to wipe"):
            fixture.checked_data_dir(refused)
    with pytest.raises(SystemExit, match="must be absolute"):
        fixture.checked_data_dir(Path("cmdk-preview-cue-data"))


def test_a_refused_dir_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    fixture = importlib.import_module(FIXTURE_MODULE)
    target = tmp_path / fixture.DATA_DIR_NAME
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_text("still here", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to wipe"):
        fixture.main(["--data-dir", str(target)])
    assert keep.read_text(encoding="utf-8") == "still here"
