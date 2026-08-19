"""Boot recovery under failure: one bad row must not strand the others.

recover() commits the status flip BEFORE it starts killing, because a kill
can take a 10s grace and holding sqlite's write lock that long would stall
every reader. That split is correct, but it means the killing phase is the
part with no transaction protecting it -- so it has to survive both a crash
and a row that blows up mid-loop.

Single-line intent:
  - if one row's reap raises then every row behind it keeps its worker alive
    with the status already committed as 'unknown', i.e. permanently orphaned
  - if a reap that never verified the group is dead still clears the row's
    'still owed' marker then no later boot ever tries again
  - if a verified-dead reap does NOT clear the marker then every future boot
    re-reaps a group that has been gone for weeks
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs import store as store_module
from apps.engine_core.jobs.reap import (
    ReapResult,
    WorkerIdentity,
    group_has_live_member,
)
from apps.engine_core.jobs.store import (
    REAP_RETRY_ERROR,
    RESTART_ERROR,
    JobStore,
)


def _store(db_path: Path, boot_id: str) -> JobStore:
    return JobStore(db_path, boot_id=boot_id, owner_pid=os.getpid())


def _orphan_row(store: JobStore, kind: str, pgid: int, argv: tuple[str, ...]) -> str:
    """A claimed row carrying a recorded worker identity, ready to be orphaned."""
    job = store.enqueue(kind, {})
    store.claim_queued()
    store.record_worker(
        job["id"],
        pid=pgid,
        identity=WorkerIdentity(pgid=pgid, argv=argv, started_at=time.time()),
    )
    return str(job["id"])


def _spawn_orphan() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_one_exploding_reap_does_not_strand_the_rows_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first row raises; the second must still be reaped and recorded.

    Before the per-row isolation, the exception escaped the recovery loop.
    Every row after it kept a live worker while its status was already
    committed as 'unknown' -- unreapable, and never revisited.
    """
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    first = _orphan_row(boot_a, "explodes", 4242, ("a",))
    second = _orphan_row(boot_a, "reaps-fine", 4343, ("b",))
    boot_a.close()

    seen: list[int] = []

    def _reap_group(identity: WorkerIdentity) -> ReapResult:
        seen.append(identity.pgid)
        if identity.pgid == 4242:
            # Exactly the shape the review named: killpg on a macOS zombie
            # group answering PermissionError, uncaught.
            raise PermissionError(13, "Operation not permitted")
        return ReapResult("reap: process group 4343 was already gone", True)

    monkeypatch.setattr(store_module, "reap_group", _reap_group)

    boot_b = _store(db_path, "boot-b")
    recovered = boot_b.recover()

    assert seen == [4242, 4343], f"the loop stopped at the first failure: {seen}"
    by_id = {row["id"]: row for row in recovered}
    assert len(by_id) == 2, recovered

    exploded = by_id[first]
    assert exploded["status"] == "unknown"
    assert "PermissionError" in exploded["error"], exploded["error"]
    assert exploded["worker_pgid"] == 4242, "an unverified group must stay flagged"

    reaped = by_id[second]
    assert reaped["status"] == "unknown"
    assert "already gone" in reaped["error"], reaped["error"]
    assert reaped["worker_pgid"] is None, "a verified reap retires the pgid"
    boot_b.close()


def test_an_unverified_reap_is_retried_on_the_next_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery is re-enterable: the row stays owed until a reap verifies it."""
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    job_id = _orphan_row(boot_a, "stubborn", 5151, ("c",))
    boot_a.close()

    attempts: list[int] = []

    def _failing(identity: WorkerIdentity) -> ReapResult:
        attempts.append(identity.pgid)
        return ReapResult("reap FAILED: group survived SIGKILL", False)

    monkeypatch.setattr(store_module, "reap_group", _failing)
    boot_b = _store(db_path, "boot-b")
    first_pass = boot_b.recover()
    boot_b.close()

    assert [row["id"] for row in first_pass] == [job_id]
    assert RESTART_ERROR in first_pass[0]["error"]

    # Boot 3 finds the row still 'unknown' AND still carrying a pgid, so it
    # tries again rather than walking past a worker nobody ever killed.
    def _succeeding(identity: WorkerIdentity) -> ReapResult:
        attempts.append(identity.pgid)
        return ReapResult("reap: process group 5151 exited on SIGTERM", True)

    monkeypatch.setattr(store_module, "reap_group", _succeeding)
    boot_c = _store(db_path, "boot-c")
    second_pass = boot_c.recover()

    assert attempts == [5151, 5151], f"the retry never happened: {attempts}"
    assert [row["id"] for row in second_pass] == [job_id]
    assert REAP_RETRY_ERROR in second_pass[0]["error"], second_pass[0]["error"]
    assert second_pass[0]["worker_pgid"] is None

    # Now it is settled: a fourth boot has nothing left to do.
    boot_c.close()
    boot_d = _store(db_path, "boot-d")
    assert boot_d.recover() == []
    boot_d.close()


def test_a_verified_reap_stops_being_retried(tmp_path: Path) -> None:
    """A real orphan, really killed, must not be chased on every future boot.

    The orphan is this process's own child, so once it dies it sits as a
    zombie until the ``finally`` waits on it. That is the macOS shape the
    review called out: the pgid stays allocated, killpg answers EPERM, and the
    kill ladder used to raise PermissionError straight out of recovery. The
    assertion is therefore about LIVE members, which is what 'reaped' means.
    """
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    orphan = _spawn_orphan()
    try:
        job = boot_a.enqueue("real-orphan", {})
        boot_a.claim_queued()
        identity = WorkerIdentity.capture(
            orphan.pid, [sys.executable, "-c", "import time; time.sleep(600)"]
        )
        boot_a.record_worker(job["id"], pid=orphan.pid, identity=identity)
        boot_a.close()

        boot_b = _store(db_path, "boot-b")
        recovered = boot_b.recover()
        assert len(recovered) == 1
        assert "reap:" in recovered[0]["error"], recovered[0]["error"]
        assert not group_has_live_member(orphan.pid), recovered[0]["error"]
        assert recovered[0]["worker_pgid"] is None
        assert boot_b.recover() == []
        boot_b.close()
    finally:
        orphan.kill()
        orphan.wait(timeout=10)


def test_a_row_with_no_worker_is_recovered_and_needs_no_retry(
    tmp_path: Path,
) -> None:
    """A row that never got as far as spawning is settled on the first pass."""
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    job = boot_a.enqueue("never-spawned", {})
    boot_a.claim_queued()
    boot_a.close()

    boot_b = _store(db_path, "boot-b")
    recovered = boot_b.recover()
    assert [row["id"] for row in recovered] == [job["id"]]
    assert recovered[0]["status"] == "unknown"
    assert "nothing to reap" in recovered[0]["error"], recovered[0]["error"]
    assert boot_b.recover() == []
    boot_b.close()


def test_recovery_reports_every_row_it_touched(tmp_path: Path) -> None:
    """Mixed batch: the caller (app.py logs it) must see all of them."""
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    without_worker = boot_a.enqueue("bare", {})
    boot_a.claim_queued()
    with_worker: dict[str, Any] = boot_a.enqueue("identified", {})
    boot_a.claim_queued()
    boot_a.record_worker(
        with_worker["id"],
        pid=6161,
        identity=WorkerIdentity(pgid=6161, argv=("d",), started_at=time.time()),
    )
    boot_a.close()

    boot_b = _store(db_path, "boot-b")
    recovered = boot_b.recover()
    assert {row["id"] for row in recovered} == {
        without_worker["id"],
        with_worker["id"],
    }
    assert all(row["status"] == "unknown" for row in recovered)
    boot_b.close()
