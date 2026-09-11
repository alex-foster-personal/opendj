"""Concurrent first-mint of the machine id on a fresh data dir.

[if] a racing first call can read a half-written machine-id file [then] fail, [else stop].

Surfaced by the live enrollment race (PR #1993 review, P1): two requests
reached a fresh hub whose own ``machine-id`` file did not exist yet. One
created it with ``O_CREAT | O_EXCL`` and had not written the id yet when the
other lost the ``O_EXCL`` race, read the still-empty file, and raised
``MachineIdentityError``, which the hub answers as HTTP 500
``SYNC_HUB_IDENTITY``. The fix publishes the id atomically (a fsynced temp
file ``os.link``-ed into place), so the final path never exists empty.

Acceptance, one test each:
- if threads racing the first mint on a fresh dir can error or disagree then broken
- if the atomic mint leaves a temp file behind or loosens the 0600 mode then broken
"""

from __future__ import annotations

import stat
import sys
import threading
from pathlib import Path

import pytest

from apps.shared.state import machine_identity

pytestmark = pytest.mark.requirement("INFRA-01")

#: Racers per fresh dir. More than two widens the window a torn read needs.
RACERS: int = 8
#: Fresh dirs raced. Pre-fix, a torn read showed up within this many rounds on
#: every run measured (see the PR body for the red run).
ROUNDS: int = 200
BARRIER_TIMEOUT_S: float = 10.0


def _race_first_mint(data_dir: Path) -> tuple[list[str], list[BaseException]]:
    barrier = threading.Barrier(RACERS, timeout=BARRIER_TIMEOUT_S)
    ids: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def mint() -> None:
        barrier.wait()
        # The declared failures are recorded and asserted on. Anything else
        # kills the thread, and the returned-count assertion reports it.
        try:
            minted = machine_identity.get_or_create_machine_id(data_dir)
        except (machine_identity.MachineIdentityError, OSError) as exc:
            with lock:
                errors.append(exc)
            return
        with lock:
            ids.append(minted)

    threads = [threading.Thread(target=mint) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=BARRIER_TIMEOUT_S * 2)
        assert not thread.is_alive(), "a minting thread hung"
    return ids, errors


def test_racing_first_mints_on_a_fresh_dir_all_return_the_same_id(tmp_path: Path) -> None:
    """if threads racing the first mint on a fresh dir can error or disagree then broken"""
    print("if threads racing the first mint on a fresh dir can error or disagree then broken")
    for round_index in range(ROUNDS):
        data_dir = tmp_path / f"fresh-{round_index}"
        ids, errors = _race_first_mint(data_dir)
        assert not errors, f"round {round_index}: a racing first mint raised: {errors[0]!r}"
        assert len(ids) == RACERS, f"round {round_index}: {len(ids)} of {RACERS} returned"
        assert len(set(ids)) == 1, f"round {round_index}: racers disagree on the id: {set(ids)}"
        on_disk = machine_identity.machine_id_path(data_dir).read_text(encoding="utf-8")
        assert on_disk == ids[0], f"round {round_index}: the file holds {on_disk!r}"


def test_the_atomic_mint_leaves_only_the_id_file_at_0600(tmp_path: Path) -> None:
    """if the atomic mint leaves a temp file behind or loosens the 0600 mode then broken"""
    print("if the atomic mint leaves a temp file behind or loosens the 0600 mode then broken")
    ids, errors = _race_first_mint(tmp_path)
    assert not errors and len(set(ids)) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        machine_identity.MACHINE_ID_FILENAME
    ], "temp files from the losing racers must be removed"
    if sys.platform != "win32":
        mode = stat.S_IMODE(machine_identity.machine_id_path(tmp_path).stat().st_mode)
        assert mode == machine_identity.MACHINE_ID_MODE, f"machine-id mode is {oct(mode)}"
