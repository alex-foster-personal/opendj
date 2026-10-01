"""STANDALONE-01 data-dir artifact guard (dependency-free).

When pytest is invoked with ``--standalone``, the active data directory
(``MDT_DATA_DIR``, else ``<repo>/data``) must not contain rekordbox vendor
artifacts. Violations are reported with absolute paths so negative-control
subprocess tests can assert on them.
"""

from __future__ import annotations

import os
from pathlib import Path

MASTER_PLAIN_DB_NAME = "master.plain.db"
ANLZ_CACHE_REL = Path("state") / "anlz-cache"
VIOLATION_PREFIX = "STANDALONE-01 violation:"


def resolve_data_dir() -> Path:
    """Return the data directory for the current process."""
    override = os.environ.get("MDT_DATA_DIR")
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data"


def find_violations(data_dir: Path) -> list[str]:
    """Return human-readable violation lines for ``data_dir`` (empty if clean).

    ``master.plain.db`` is searched for anywhere beneath ``data_dir``, not
    just at its root: a copy nested under a subdirectory (a stray import
    scratch dir, an old backup) is exactly as much a violation as one sitting
    at the top level, and a root-only check would miss it.
    """
    resolved_dir = data_dir.resolve()

    violations: list[str] = [
        f"{VIOLATION_PREFIX} forbidden rekordbox artifact at {master}"
        for master in sorted(resolved_dir.rglob(MASTER_PLAIN_DB_NAME))
        if master.is_file()
    ]

    anlz_cache = resolved_dir / ANLZ_CACHE_REL
    if anlz_cache.exists():
        violations.append(
            f"{VIOLATION_PREFIX} forbidden ANLZ cache at {anlz_cache}"
        )

    return violations


def assert_data_dir_clean() -> None:
    """Fail the pytest session when ``data_dir`` holds forbidden artifacts."""
    violations = find_violations(resolve_data_dir())
    if violations:
        import pytest

        pytest.exit("\n".join(violations), returncode=1)
