"""Real-library scratch-copy regression for relink undo.

Split from ``tests/reconcile/test_relink.py`` for the #1390 file-size gate.

[if] undo on the real schema is not byte-reversible [then] live relink is one-way, [else stop].
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.reconcile import relink
from tests.reconcile.conftest import HAS_REAL_LIBRARY
from tests.reconcile.relink_support import (
    _audio,
    _entry,
    _real_state_db,
    _rows,
)

pytestmark = pytest.mark.requirement("RECON-01")

# ----- real library (scratch copy only) --------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    not HAS_REAL_LIBRARY,
    reason="real library not present (state.db missing, unreadable, or has zero track rows)",
)
def test_undo_on_a_scratch_copy_of_the_real_db_restores_byte_identical_rows(
    tmp_path: Path,
) -> None:
    """The proof the maintainer asked for, as a permanent regression test.

    Copies the real state.db, applies the first few auto relinks derived from the
    live plan, undoes them, and asserts every row value is byte-identical
    (``updated_at`` excepted, which is the writer's own stamp).
    """
    scratch = tmp_path / "state.db"
    shutil.copy2(_real_state_db(), scratch)
    conn = sqlite3.connect(f"file:{scratch}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT stable_id, file_path FROM tracks WHERE file_path IS NOT NULL").fetchall()
    finally:
        conn.close()
    # Build entries from real rows, retargeted at throwaway files so the test
    # never depends on the current disk layout.
    entries: list[relink.RelinkEntry] = []
    for sid, old in rows[:5]:
        target = _audio(tmp_path / "disk", f"{sid[:8]}.mp3")
        entries.append(_entry(sid, old, str(target)))
    before = _rows(scratch)
    relink.apply_entries(scratch, entries)
    mid = _rows(scratch)
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert [mid[e.stable_id][idx] for e in entries] == [e.new_path for e in entries]
    relink.apply_entries(scratch, entries, reverse=True, tolerate_conflicts=True)
    after = _rows(scratch)
    assert set(after) == set(before)
    for sid in before:
        for pos, col in enumerate(relink.TRACK_COLUMNS):
            if col == "updated_at":
                continue
            assert after[sid][pos] == before[sid][pos], (
                f"if undo does not restore {col} on the real schema then the live run is not reversible"
            )
    assert json.loads(json.dumps(list(after))) == json.loads(json.dumps(list(before)))
