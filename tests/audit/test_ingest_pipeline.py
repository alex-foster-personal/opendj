"""Gated live regression: repaired/ingested tracks actually come up in the library.

Companion to test_library_integrity.py's live guard. These tests read the
applied manifest (data/reconcile/applied.json, written only by a SUCCESSFUL
live or bulk apps.reconcile.apply transaction, never by dry-run previews)
and assert the pipeline's end state end-to-end:
bytes on disk, a Rekordbox row, and a state.db row for every repaired path.

Skipped unless MDT_LIVE_LIBRARY=1 (CI machines have no library). Run:

    MDT_LIVE_LIBRARY=1 .venv/bin/python -m pytest tests/audit/test_ingest_pipeline.py -v

Regression lines:
  - if a repaired path no longer resolves to materialised bytes then broken
  - if a repaired path has no djmdContent row in master.plain.db then broken
  - if a repaired path has no state.db tracks row after ingest-rb then broken
"""
from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from apps.shared import fs_residency, paths

APPLIED_JSON = paths.DATA_DIR / "reconcile" / "applied.json"
PLAIN_DB = paths.DATA_DIR / "master.plain.db"

live = pytest.mark.skipif(
    os.environ.get("MDT_LIVE_LIBRARY") != "1",
    reason="set MDT_LIVE_LIBRARY=1 to run against the real library",
)


def _norm(p: str) -> str:
    return unicodedata.normalize("NFC", p)


def _applied_paths() -> list[str]:
    if not APPLIED_JSON.exists():
        pytest.skip(
            f"no applied manifest at {APPLIED_JSON} - run a live/bulk apply first"
        )
    rows = json.loads(APPLIED_JSON.read_text())
    assert rows, "applied.json exists but is empty"
    return [r["new_path"] for r in rows]


@live
def test_live_repaired_paths_materialised_on_disk():
    missing = [
        p for p in _applied_paths() if not fs_residency.is_materialised(Path(p))
    ]
    assert not missing, (
        f"{len(missing)} repaired paths are gone or dataless again; first: "
        f"{missing[:3]}"
    )


@live
def test_live_repaired_paths_in_rekordbox():
    assert PLAIN_DB.exists(), f"{PLAIN_DB} missing - re-decrypt the working copy"
    rb = {
        _norm(r[0])
        for r in sqlite3.connect(PLAIN_DB).execute(
            "SELECT FolderPath FROM djmdContent WHERE FolderPath IS NOT NULL"
        )
    }
    missing = [p for p in _applied_paths() if _norm(p) not in rb]
    assert not missing, (
        f"{len(missing)} repaired paths lack a Rekordbox row (stale "
        f"master.plain.db? re-decrypt); first: {missing[:3]}"
    )


@live
def test_live_repaired_paths_in_state_db():
    state = {
        _norm(r[0])
        for r in sqlite3.connect(paths.STATE_DB).execute(
            "SELECT file_path FROM tracks WHERE file_path IS NOT NULL"
        )
    }
    missing = [p for p in _applied_paths() if _norm(p) not in state]
    assert not missing, (
        f"{len(missing)} repaired paths lack a state.db track row (run "
        f"ingest-rb --write); first: {missing[:3]}"
    )
