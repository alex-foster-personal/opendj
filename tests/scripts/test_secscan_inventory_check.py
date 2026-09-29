"""secscan inventory-check: every hash-pinned CI lock is a scanned manifest (PR #4297 review).

- if a CI pylock from scripts/ci_lock.py LOCKS is missing from scan_deps.sh MANIFESTS
  then the dependency scan never reads the packages CI actually installs
- if a new tracked `pylock.<name>.toml` is not listed then inventory-check exits 2 (UNKNOWN)
- if the repo's own MANIFESTS list does not match its tracked lockfiles then inventory-check exits 2
- if a file merely ends in `pylock.toml` without the `pylock.` stem then it is not a manifest
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from scripts.ci_lock import LOCKS
from scripts.security.secscan import LOCKFILE_PATTERN

REPO_ROOT = Path(__file__).resolve().parents[2]
SECSCAN = REPO_ROOT / "scripts" / "security" / "secscan.py"
SCAN_DEPS = REPO_ROOT / "scripts" / "security" / "scan_deps.sh"


def _scan_deps_manifests() -> list[str]:
    text = SCAN_DEPS.read_text(encoding="utf-8")
    block = re.search(r"^MANIFESTS=\(\n(.*?)^\)", text, re.M | re.S)
    assert block, "MANIFESTS=( ... ) block not found in scan_deps.sh"
    manifests = [line.strip() for line in block.group(1).splitlines() if line.strip()]
    assert "uv.lock" in manifests, f"parser presence control failed: {manifests}"
    return manifests


def _inventory_check(root: Path, expect: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SECSCAN), "inventory-check", "--root", str(root), "--expect", *expect],
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_ci_lock_is_a_scanned_manifest() -> None:
    """[if] a lock CI syncs from is absent from MANIFESTS [then] osv never scans it, [else stop]."""
    manifests = _scan_deps_manifests()
    missing = [lock.output for lock in LOCKS if lock.output not in manifests]
    assert LOCKS, "no CI locks declared; this check would pass vacuously"
    assert not missing, f"CI locks not in scan_deps.sh MANIFESTS: {missing}"


def test_named_pylocks_match_the_inventory_pattern() -> None:
    """[if] a PEP 751 `pylock.<name>.toml` is not a manifest [then] one escapes, [else stop]."""
    for lock in LOCKS:
        assert LOCKFILE_PATTERN.search(lock.output), lock.output
    assert LOCKFILE_PATTERN.search("pylock.toml")
    assert not LOCKFILE_PATTERN.search("notpylock.toml")
    assert not LOCKFILE_PATTERN.search("pylock.toml.bak")


def test_repo_inventory_matches_git() -> None:
    """[if] MANIFESTS and tracked lockfiles disagree [then] inventory-check exits 2, [else stop]."""
    proc = _inventory_check(REPO_ROOT, _scan_deps_manifests())
    assert proc.returncode == 0, proc.stderr


def test_unlisted_named_pylock_is_unknown(tmp_path: Path) -> None:
    """[if] a tracked pylock.<name>.toml is not listed [then] exit 2 naming it, [else stop]."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pylock.extra.toml").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    listed = _inventory_check(tmp_path, ["uv.lock", "pylock.extra.toml"])
    assert listed.returncode == 0, listed.stderr
    unlisted = _inventory_check(tmp_path, ["uv.lock"])
    assert unlisted.returncode == 2
    assert "pylock.extra.toml" in unlisted.stderr
