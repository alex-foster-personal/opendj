"""Cross-repository constructor schema binding contract.

[if] repo opens fresh conn [then] default init creates tables, [else stop].
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Protocol

import pytest

from apps.shared.pairings.capture_repo import PairingCaptureRepo
from apps.shared.pairings.repo import PairingsRepo
from apps.smartlists.repo import SmartlistsRepo

pytestmark = [
    pytest.mark.requirement("PAIR-01"),
    pytest.mark.requirement("CAT-03"),
    pytest.mark.requirement("SMART-02"),
]

class _RepoWithConn(Protocol):
    conn: sqlite3.Connection


_RepoFactory = Callable[[sqlite3.Connection, bool], _RepoWithConn]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


@pytest.mark.parametrize(
    ("repo_factory", "table_name"),
    [
        (lambda c, es: PairingCaptureRepo(c, ensure_schema=es), "pairing_sync_snapshots"),
        (lambda c, es: PairingsRepo(c, ensure_schema=es), "pairings"),
        (lambda c, es: SmartlistsRepo(c, ensure_schema=es), "smartlists"),
    ],
    ids=["capture", "pairings", "smartlists"],
)
def test_default_init_binds_conn_and_creates_schema(
    repo_factory: _RepoFactory,
    table_name: str,
) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        repo = repo_factory(conn, True)
        assert repo.conn is conn
        assert _table_exists(conn, table_name)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("repo_factory", "table_name"),
    [
        (lambda c, es: PairingCaptureRepo(c, ensure_schema=es), "pairing_sync_snapshots"),
        (lambda c, es: PairingsRepo(c, ensure_schema=es), "pairings"),
        (lambda c, es: SmartlistsRepo(c, ensure_schema=es), "smartlists"),
    ],
    ids=["capture", "pairings", "smartlists"],
)
def test_ensure_schema_false_binds_conn_without_ddl(
    repo_factory: _RepoFactory,
    table_name: str,
) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        repo = repo_factory(conn, False)
        assert repo.conn is conn
        assert not _table_exists(conn, table_name)
    finally:
        conn.close()
