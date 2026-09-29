"""The Playwright entries of ci/runner-toolset.yml, probed for real under bash.

Split out of tests/scripts/test_runner_toolset_verify_probe.py, whose probe
helpers these tests share. A Playwright browser is verified by its payload:
the INSTALLATION_COMPLETE marker Playwright writes last, plus the executable
actually run. Tests that need a real install skip UNAVAILABLE without one.

Regression lines:
  - if a Playwright revision dir left by an interrupted install (no
    INSTALLATION_COMPLETE) or missing its executable reports OK then broken
  - if a WebKit MiniBrowser (WPE or GTK) whose binary is truncated but keeps its
    executable bit and the INSTALLATION_COMPLETE marker reports OK then broken
  - if an intact real WebKit install, rebuilt the same way, does not report OK
    then broken
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from scripts.runner_toolset_scan import load_manifest
from tests.scripts.test_runner_toolset_verify_probe import (
    PROBE_NEEDS,
    _dirs_of,
    _probe_locally,
    _runner_with_path,
)

PLAYWRIGHT_TOOLS = (*PROBE_NEEDS, "ls", "test", "grep")
PLAYWRIGHT_DIR_RE = re.compile(r"ms-playwright/([\w-]+-\d+)")


def _playwright_entries() -> list[tuple[dict, str]]:
    """(entry, revision dir name) for every Playwright entry in the live manifest."""
    entries = [e for e in load_manifest()["entries"] if e["kind"] == "playwright"]
    assert entries, "no playwright entries in the manifest: the test checks nothing"
    named = [(e, PLAYWRIGHT_DIR_RE.search(e["verify"])) for e in entries]
    unnamed = [e["name"] for e, found in named if not found]
    assert not unnamed, f"verify names no ms-playwright revision dir: {unnamed}"
    return [(e, found.group(1)) for e, found in named if found]


@pytest.mark.parametrize("state", ["interrupted-install", "payload-deleted"])
def test_a_playwright_revision_dir_without_its_payload_is_missing(
    tmp_path: Path, state: str
) -> None:
    """An interrupted install leaves the revision dir with no INSTALLATION_COMPLETE
    marker; a later cleanup can leave the marker and delete the browser. Neither
    can launch, so neither may verify OK on the dir name alone."""
    home = tmp_path / "home"
    entries = _playwright_entries()
    for _, dirname in entries:
        revision = home / ".cache" / "ms-playwright" / dirname
        revision.mkdir(parents=True)
        if state == "payload-deleted":
            (revision / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
            (revision / "DEPENDENCIES_VALIDATED").write_text("", encoding="utf-8")
    runner = _runner_with_path(tmp_path, _dirs_of(*PLAYWRIGHT_TOOLS))
    results, _ = _probe_locally([e for e, _ in entries], runner, env={"HOME": str(home)})
    assert {r.name: r.status for r in results} == {e["name"]: "MISSING" for e, _ in entries}, (
        results
    )


def test_a_complete_playwright_install_verifies_ok_and_its_marker_is_what_counts(
    tmp_path: Path,
) -> None:
    """On the real cache: a revision Playwright itself marked INSTALLATION_COMPLETE
    verifies OK (the overshoot control), and the same real payload without that
    marker, as an install interrupted after unpacking leaves it, is MISSING."""
    cache = Path.home() / ".cache" / "ms-playwright"
    entries = _playwright_entries()
    absent = [d for _, d in entries if not (cache / d / "INSTALLATION_COMPLETE").is_file()]
    if absent:
        pytest.skip(f"UNAVAILABLE: no completed Playwright install here for {absent}")
    runner = _runner_with_path(tmp_path, _dirs_of(*PLAYWRIGHT_TOOLS))
    results, _ = _probe_locally([e for e, _ in entries], runner)
    assert {r.status for r in results} == {"OK"}, results
    home = tmp_path / "home"
    for _, dirname in entries:
        unmarked = home / ".cache" / "ms-playwright" / dirname
        unmarked.mkdir(parents=True)
        for part in (cache / dirname).iterdir():
            if part.name != "INSTALLATION_COMPLETE":
                (unmarked / part.name).symlink_to(part)
    results, _ = _probe_locally([e for e, _ in entries], runner, env={"HOME": str(home)})
    assert {r.status for r in results} == {"MISSING"}, results


# ----- WebKit: the MiniBrowser wrapper must reach its binary ------------------------------

REAL_CACHE = Path.home() / ".cache" / "ms-playwright"


def _real_webkit() -> tuple[dict, Path]:
    """The manifest's WebKit entry and its completed real install, or skip UNAVAILABLE."""
    webkit = [(e, d) for e, d in _playwright_entries() if d.startswith("webkit-")]
    assert len(webkit) == 1, f"expected one WebKit entry, found {webkit}"
    entry, dirname = webkit[0]
    real = REAL_CACHE / dirname
    if not (real / "INSTALLATION_COMPLETE").is_file():
        pytest.skip(f"UNAVAILABLE: no completed Playwright WebKit install at {real}")
    return entry, real


def _rebuild_webkit(real: Path, home: Path, flavor: str, keep_fraction: float) -> None:
    """Rebuild `real` under `home`, every part a symlink to the real one except
    minibrowser-<flavor>: its wrapper and bin/ are real copies, bin/MiniBrowser cut to
    keep_fraction of its bytes and left executable."""
    copy = home / ".cache" / "ms-playwright" / real.name
    copy.mkdir(parents=True)
    for part in real.iterdir():
        if part.name != f"minibrowser-{flavor}":
            (copy / part.name).symlink_to(part)
    flavor_real, flavor_copy = real / f"minibrowser-{flavor}", copy / f"minibrowser-{flavor}"
    flavor_copy.mkdir()
    for part in flavor_real.iterdir():
        if part.name == "MiniBrowser":
            shutil.copy2(part, flavor_copy / part.name)
        elif part.name != "bin":
            (flavor_copy / part.name).symlink_to(part)
    (flavor_copy / "bin").mkdir()
    for part in (flavor_real / "bin").iterdir():
        if part.name != "MiniBrowser":
            (flavor_copy / "bin" / part.name).symlink_to(part)
    payload = (flavor_real / "bin" / "MiniBrowser").read_bytes()
    binary = flavor_copy / "bin" / "MiniBrowser"
    binary.write_bytes(payload[: int(len(payload) * keep_fraction)])
    binary.chmod(0o755)


@pytest.mark.parametrize("flavor", ["wpe", "gtk"])
def test_a_truncated_webkit_minibrowser_is_missing_and_an_intact_rebuild_is_ok(
    tmp_path: Path, flavor: str
) -> None:
    """A MiniBrowser binary cut short keeps its executable bit and the marker, so a
    `test -x` check passes it; running it cannot. The same rebuild with the binary
    whole is the overshoot control: it must verify OK, so MISSING comes from the
    truncation and not from the rebuild."""
    entry, real = _real_webkit()
    runner = _runner_with_path(tmp_path, _dirs_of(*PLAYWRIGHT_TOOLS))
    for keep_fraction, expected in ((1.0, "OK"), (0.5, "MISSING")):
        home = tmp_path / f"home-{keep_fraction}"
        _rebuild_webkit(real, home, flavor, keep_fraction)
        results, stdout = _probe_locally([entry], runner, env={"HOME": str(home)})
        assert [r.status for r in results] == [expected], (keep_fraction, results, stdout)
