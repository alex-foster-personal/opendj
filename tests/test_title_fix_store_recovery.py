"""The proposal store keeps what a scan paid for and what it auto-applied, across a dead scan and a failed rescan."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.tags import title_fix_store
from apps.tags.title_fix import Proposal, Track, Verdict
from apps.tags.title_fix_store import ProposalStore, StorePaths

FULL_CONFIG = {"enabled": True, "auto_apply_policy": "off", "spend_cap_usd": 0.5, "tag_families": ["version"]}
FUTURE = "9999-01-01T00:00:00+00:00"  # every heartbeat is older than this


def _store(tmp_path: Path) -> ProposalStore:
    return ProposalStore(StorePaths(tmp_path / "data" / "state" / "state.db"))


def _fixed(title: str) -> Verdict:
    return Verdict("fixed", "Pine Ogre", title, "regex", Proposal("Pine Ogre", title))


@pytest.mark.requirement("TFIX-01")
def test_a_scan_that_died_after_its_auto_apply_committed_keeps_the_batch_link(tmp_path: Path) -> None:
    """[if] a scan dies after its auto batch committed [then] stale expiry links that batch to the scan, [else stop]."""
    store = _store(tmp_path)
    scan = store.create_scan(1, 0.5)
    row = store.add_verdict(scan.scan_id, Track("t1", "raw title", ""), [], _fixed("Clean Title"))
    batch = store.create_batch("auto", "safe_tier", 1)
    assert store.claim_pending([row.proposal_id], batch.batch_id, {}) == [row.proposal_id]
    store.mark_batch_committed(batch.batch_id)
    assert store.expire_stale_scans(stale_before=FUTURE) == 1, "control: the scan must actually expire"
    expired = store.get_scan(scan.scan_id)
    assert expired.state == "failed" and expired.auto_batch_id == batch.batch_id, (
        f"if the link is lost then the scan status hides an undoable automatic change: {expired}"
    )


@pytest.mark.requirement("TFIX-01")
def test_an_uncommitted_or_manual_batch_is_never_linked_to_a_dead_scan(tmp_path: Path) -> None:
    """[if] the batch is manual or never committed [then] stale expiry links nothing, [else stop]."""
    store = _store(tmp_path)
    scan = store.create_scan(2, 0.5)
    first = store.add_verdict(scan.scan_id, Track("t1", "a", ""), [], _fixed("A"))
    second = store.add_verdict(scan.scan_id, Track("t2", "b", ""), [], _fixed("B"))
    manual = store.create_batch("manual", "manual", 1)
    store.claim_pending([first.proposal_id], manual.batch_id, {})
    store.mark_batch_committed(manual.batch_id)
    unstamped = store.create_batch("auto", "safe_tier", 1)
    store.claim_pending([second.proposal_id], unstamped.batch_id, {})
    store.expire_stale_scans(stale_before=FUTURE)
    assert store.get_scan(scan.scan_id).auto_batch_id is None, (
        "if a manual or unstamped batch is linked then the link lies"
    )


@pytest.mark.requirement("TFIX-01")
def test_a_failed_rescan_keeps_the_earlier_pending_proposal_and_a_usable_one_replaces_it(tmp_path: Path) -> None:
    """[if] a rescan comes back unknown [then] the earlier pending proposal stays; a usable verdict supersedes it, [else stop]."""
    store = _store(tmp_path)
    first_scan = store.create_scan(1, 0.5)
    pending = store.add_verdict(first_scan.scan_id, Track("t1", "raw title", ""), [], _fixed("Clean Title"))
    store.update_scan(first_scan.scan_id, state="done", finished_at="2026-10-20T00:00:00+00:00")
    second_scan = store.create_scan(1, 0.5)
    unknown = Verdict("unknown", "", "", "none", Proposal("", ""), error="provider timeout")
    store.add_verdict(second_scan.scan_id, Track("t1", "raw title", ""), [], unknown)
    assert store.get_proposals([pending.proposal_id])[0].state == "pending", (
        "if an unknown rescan supersedes it then paid work is lost"
    )
    store.add_verdict(second_scan.scan_id, Track("t1", "raw title", ""), [], _fixed("Cleaner Title"))
    assert store.get_proposals([pending.proposal_id])[0].state == "superseded", (
        "control: a usable rescan still replaces it"
    )


@pytest.mark.requirement("TFIX-01")
def test_a_proposal_lookup_larger_than_the_sqlite_variable_cap_still_returns_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] more proposal ids are read than one IN (...) may carry [then] every row still comes back in order, [else stop]."""
    store = _store(tmp_path)
    scan = store.create_scan(7, 0.5)
    rows = [store.add_verdict(scan.scan_id, Track(f"t{i}", f"raw {i}", ""), [], _fixed(f"Title {i}")) for i in range(7)]
    real_connect = sqlite3.connect

    def capped_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)  # a host build with a tiny cap
        return conn

    monkeypatch.setattr(sqlite3, "connect", capped_connect)
    monkeypatch.setattr(title_fix_store.CFG, "SQL_IN_CHUNK", 3)
    ids = [r.proposal_id for r in rows]
    assert [p.proposal_id for p in store.get_proposals(ids)] == ids, (
        "if the lookup is one IN (...) then a big auto-apply fails"
    )


def test_chunked_splits_without_losing_or_reordering_ids() -> None:
    """[if] ids are chunked [then] every id appears once, in order, in slices of at most the size, [else stop]."""
    ids = [str(i) for i in range(7)]
    chunks = list(title_fix_store.chunked(ids, 3))
    assert [len(c) for c in chunks] == [3, 3, 1] and [i for c in chunks for i in c] == ids
    assert list(title_fix_store.chunked([], 3)) == []


@pytest.mark.requirement("TFIX-06")
@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ('{"enabled": tr', "not valid JSON"),
        (json.dumps({**FULL_CONFIG, "tag_families": None}), "tag_families must be a list"),
        ("[1, 2]", "not a JSON object"),
        ('{"enabled": true, "bogus": 1}', "unknown keys"),
        ('{"enabled": true}', "missing keys"),
    ],
)
def test_a_malformed_saved_config_raises_config_error_naming_the_file(
    tmp_path: Path, content: str, needle: str
) -> None:
    """[if] the saved config is truncated, mis-shaped or has unknown keys [then] load_config raises ConfigError naming it, [else stop]."""
    paths = title_fix_store.StorePaths(tmp_path / "data" / "state" / "state.db")
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text(content, encoding="utf-8")
    with pytest.raises(title_fix_store.ConfigError, match=needle) as raised:
        title_fix_store.load_config(paths)
    assert paths.config.name in str(raised.value), (
        "if the error names no file then the operator cannot tell what to repair"
    )
    paths.config.unlink()
    assert title_fix_store.load_config(paths) == title_fix_store.TitleFixConfig(), "control: no file means the defaults"


@pytest.mark.requirement("TFIX-05")
def test_a_producer_credit_the_proposal_removes_is_recorded_on_the_proposal(tmp_path: Path) -> None:
    """[if] a proposal removes a producer credit from the title [then] the credit is stored in its notes, [else stop]."""
    store = _store(tmp_path)
    scan = store.create_scan(2, 0.5)
    with_credit = Verdict(
        "fixed", "Pine Ogre", "Clean Title", "regex", Proposal("Pine Ogre", "Clean Title"), producer="Marrow Vell"
    )
    row = store.add_verdict(scan.scan_id, Track("t1", "Clean Title - prod. Marrow Vell", "Pine Ogre"), [], with_credit)
    assert "producer credit removed from the title: Marrow Vell" in row.notes, (
        f"if the credit is not recorded it is lost on apply: {row.notes}"
    )
    plain = store.add_verdict(scan.scan_id, Track("t2", "raw", "Pine Ogre"), [], _fixed("Plain"))
    assert not any("producer" in note for note in plain.notes), "control: no credit, no note"


@pytest.mark.requirement("TFIX-01")
def test_a_scan_records_the_auto_apply_policy_it_started_under(tmp_path: Path) -> None:
    """[if] a scan begins under a policy [then] the scan row keeps that policy whatever the settings say later, [else stop]."""
    store = _store(tmp_path)
    scan = store.begin_scan(1, 0.5, auto_apply_policy="safe_tier", stale_before="0")
    assert scan is not None and scan.auto_apply_policy == "safe_tier", (
        f"if the policy is not recorded the panel shows the wrong one: {scan}"
    )
    title_fix_store.save_config(store.paths, title_fix_store.TitleFixConfig(True, "off", 0.5, ("version",)))
    assert store.get_scan(scan.scan_id).auto_apply_policy == "safe_tier", (
        "a settings change must not rewrite a running scan's policy"
    )


@pytest.mark.requirement("TFIX-01")
def test_a_store_written_before_the_policy_column_is_migrated_with_a_blank_policy(tmp_path: Path) -> None:
    """[if] an older store has no scans.auto_apply_policy column [then] opening it adds the column and old rows read blank, [else stop]."""
    paths = title_fix_store.StorePaths(tmp_path / "data" / "state" / "state.db")
    paths.store_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(paths.store_db)
    conn.executescript(
        "CREATE TABLE scans (scan_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,"
        " requested INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,"
        " spend_cap_usd REAL NOT NULL, error TEXT, auto_batch_id TEXT, heartbeat_at TEXT NOT NULL);"
        "INSERT INTO scans VALUES ('old', '2026-10-01T00:00:00+00:00', NULL, 'done', 1, 1, 0, 0.5, NULL, NULL, '');"
        "PRAGMA user_version = 3;"
    )
    conn.commit()
    conn.close()
    store = ProposalStore(paths)
    assert store.get_scan("old").auto_apply_policy == "", "if the column is not added an older store cannot be opened"


class _CommitFails(sqlite3.Connection):
    """A connection whose COMMIT fails the way a full disk or an I/O error makes it fail."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return super().execute(sql, *args)


@pytest.mark.requirement("TFIX-01")
def test_a_fence_whose_own_commit_fails_raises_the_fence_exit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the fence's own COMMIT fails after the caller's write [then] the store raises FenceExitError, [else stop]."""
    store = _store(tmp_path)
    with store.exclusive():
        pass  # control: a healthy fence exits without raising
    real_connect = sqlite3.connect

    def connect_with_a_failing_commit(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return real_connect(*args, factory=_CommitFails, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_a_failing_commit)
    with pytest.raises(title_fix_store.FenceExitError) as raised, store.exclusive():
        pass
    assert "disk I/O error" in str(raised.value), (
        "if the fence exit raises anything else then the apply and the undo, which catch FenceExitError to keep"
        f" the record of a write that already committed, lose that record: {raised.value!r}"
    )
