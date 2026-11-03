"""Every ctypes library load in the engine source is classified before a payload build sees it (INSTALL-11).

A whole-tree guard (listed in ci/pr-always-run.txt): it reads every tracked
``apps/*.py`` module, so a PR that adds a load site anywhere in apps/ is
caught here, in about a second, instead of by the ten-minute payload build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.build_engine_payload import classify_runtime_load_sites, find_runtime_load_sites


@pytest.mark.requirement("INSTALL-11")
def test_engine_source_runtime_loads_are_all_classified(tmp_path: Path) -> None:
    """[if] a tracked apps/ module adds a ctypes load the allowlist does not name [then] this fails before a payload build does, [else stop]."""
    repo = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", "apps/*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    staged = 0
    for relative in filter(None, tracked):
        text = (repo / relative).read_text(encoding="utf-8")
        if "CDLL" in text or "find_library" in text or "LoadLibrary" in text:
            target = tmp_path / "app" / relative  # the payload stages apps/ under app/
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            staged += 1
    assert staged > 0, "no tracked apps/ module loads a library; the scan proved nothing"
    sites = find_runtime_load_sites(tmp_path / "app", tmp_path)
    _, unclassified = classify_runtime_load_sites(sites)
    assert unclassified == [], [site.describe() for site in unclassified]
