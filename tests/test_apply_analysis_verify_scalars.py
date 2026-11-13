"""Scalar verify helpers for key and loudness write-back.

[if] _verify_rb_field checks scalars [then] key and loudness match sqlite read-back, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.apply_analysis import _verify_rb_field
from tests.apply_analysis_support import (
    _owned_rollout_directory,  # noqa: F401 (autouse fixture, scoped to this module by the import)
    _write_rb_db,
)


pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]


class TestVerifyRBFieldScalars:
    def test_key_verify_sqlite(self, tmp_path: Path) -> None:
        from apps.sync.analysis_writeback import write_scalar

        rb_path = tmp_path / "rb.db"
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.commit()
        write_scalar(rb_conn, "rb-1", "key", "9A", {})
        rb_conn.commit()
        assert _verify_rb_field(rb_conn, "rb-1", "key", "9A")

    def test_loudness_verify_sqlite(self, tmp_path: Path) -> None:
        from apps.sync.analysis_writeback import write_scalar

        rb_path = tmp_path / "rb.db"
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.commit()
        write_scalar(rb_conn, "rb-1", "loudness_lufs", -8.2, {})
        rb_conn.commit()
        assert _verify_rb_field(rb_conn, "rb-1", "loudness_lufs", -8.2)
