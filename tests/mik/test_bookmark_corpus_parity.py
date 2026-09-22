"""Corpus parity: legacy ad-hoc scanner vs canonical TOC parser on every MIK blob.

[if] legacy and canonical decoders disagree on a real blob [then] fail, [else stop].
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.equivalence.config import CFG
from apps.equivalence.sources import decode_bookmark_path

from .legacy_bookmark_decoder import legacy_decode_bookmark_path

pytestmark = pytest.mark.requirement("META-01")

_EXPECTED_BLOB_COUNT = 7006


@pytest.mark.skipif(
    not CFG.mik_db.exists(),
    reason="MIK store not present on this machine",
)
def test_all_zbookmarkdata_blobs_agree_between_legacy_and_canonical() -> None:
    conn = sqlite3.connect(f"file:{CFG.mik_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT Z_PK, ZBOOKMARKDATA FROM ZSONG WHERE ZBOOKMARKDATA IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == _EXPECTED_BLOB_COUNT, (
        f"expected {_EXPECTED_BLOB_COUNT} populated ZBOOKMARKDATA rows, got {len(rows)}"
    )

    for pk, blob in rows:
        legacy = legacy_decode_bookmark_path(blob)
        canonical = decode_bookmark_path(blob)
        if legacy != canonical:
            pytest.fail(
                f"Z_PK={pk} blob_len={len(blob)} "
                f"legacy={legacy!r} canonical={canonical!r}"
            )
