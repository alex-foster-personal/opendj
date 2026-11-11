"""Shared helpers for library availability resolver tests.

Subprocess env, marker reads, and mapped-track seeds used by more than one
sibling test module. Path(__file__) stays two levels below the repo root,
matching the helpers' original location.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from apps.engine_core.library_availability import _RESOLVER_MARKER_PREFIX
from apps.shared.state import db as state_db
from tests.engine_core.availability_test_support import _stable, _upsert_track


def _resolver_subprocess_env() -> dict[str, str]:
    """Child env with path-map overrides removed, matching the idle probe."""
    env = {key: value for key, value in os.environ.items() if key not in {"MDT_PATH_MAP", "MDT_DATA_DIR"}}
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(repo_root) + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
    return env


def _run_resolver_subprocess(script: str, args: list[str]) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=repo_root,
        env=_resolver_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "ok"


def _resolver_markers(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT marker FROM schema_meta_markers WHERE marker LIKE ?",
            (_RESOLVER_MARKER_PREFIX + "%",),
        )
    ]


_MAPPED_TRACK_COUNT = 4


def _seed_mapped_present_tracks(data_dir: Path, state_db_path: Path) -> tuple[Path, Path, Path, Path]:
    """Four stored-missing tracks whose first sweep is present via path-map."""
    stored_root = data_dir / "stored-missing"
    present_root = data_dir / "mapped-present"
    missing_root = data_dir / "mapped-missing"
    restored_root = data_dir / "mapped-restored"
    present_root.mkdir()
    missing_root.mkdir()
    restored_root.mkdir()
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(_MAPPED_TRACK_COUNT):
            name = f"{index}.mp3"
            (present_root / name).write_bytes(b"audio")
            (restored_root / name).write_bytes(b"restored-audio")
            stored = stored_root / name
            assert not stored.exists()
            _upsert_track(conn, _stable(f"m{index}"), str(stored))
    finally:
        conn.close()
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": str(stored_root), "to": str(present_root)}]}),
        encoding="utf-8",
    )
    return stored_root, present_root, missing_root, restored_root
