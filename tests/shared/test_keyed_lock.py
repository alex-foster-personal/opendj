"""KeyedLocks: one in-process lock per key, isolated per registry instance.

[if] two threads race lock_for on a fresh key [then] both get the same Lock, [else stop].
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apps.shared.keyed_lock import KeyedLocks


def test_lock_for_returns_same_lock_for_same_key() -> None:
    locks = KeyedLocks()
    key = "track-a"
    assert locks.lock_for(key) is locks.lock_for(key)


def test_lock_for_returns_different_locks_for_different_keys() -> None:
    locks = KeyedLocks()
    assert locks.lock_for("a") is not locks.lock_for("b")


def test_separate_instances_do_not_share_locks() -> None:
    key = "shared-key"
    first = KeyedLocks()
    second = KeyedLocks()
    assert first.lock_for(key) is not second.lock_for(key)


def test_str_and_path_keys_are_distinct() -> None:
    locks = KeyedLocks()
    assert locks.lock_for("x") is not locks.lock_for(Path("x"))


def test_concurrent_first_create_returns_one_lock() -> None:
    locks = KeyedLocks()
    key = "race-key"
    ready = threading.Barrier(2)
    seen: list[threading.Lock] = []

    def claim() -> None:
        ready.wait(timeout=5)
        seen.append(locks.lock_for(key))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim), pool.submit(claim)]
        for future in futures:
            future.result(timeout=5)

    assert len(seen) == 2
    assert seen[0] is seen[1]
