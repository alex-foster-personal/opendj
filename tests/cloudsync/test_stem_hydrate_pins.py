"""Hydrate pins and the cross-process upload-queue lock (STEM-40, STEM-42).

Two blocking findings from Codex's review of #4974 (a8c805694) and one from
the follow-up review, each with the test that bites and its overshoot
control:

* [if] an eviction pass in any process runs while a hydrate publishes a
  bundle [then] the hydrate's own pin keeps it until the post-hydrate pass.
* [if] a second hydrate of the same id finds it already local [then] the
  first hydrate's pin stays.
* [if] another process holds the upload queue's file lock [then] a save
  waits and merges what that holder wrote.
* [if] a hydrate fetches and replaces a bundle [then] it holds that id's
  cross-process lock, so a hydrate in another process waits.
"""
from __future__ import annotations

import errno
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_cache_settings import write_json_atomically
from apps.cloud.stem_hydration import OpenDeckRegistry
from tests.cloudsync.stem_cache_rig import FLOOR_BYTES, disk_usage, enforce, make_bundle


@pytest.mark.requirement("STEM-42")
def test_a_bundle_a_hydrate_pinned_is_not_evicted_by_any_pass(tmp_path: Path):
    """[if] a fresh hydrate pin names a bundle [then] eviction skips it, [else stop].

    MUTATION TARGET: drop the ``is_hydrate_pinned`` check in ``_evict_lru``
    and the just-published bundle is removed while no deck registry names it.
    """
    from apps.cloud.stem_bundles import pin_for_hydrate

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"publishing": make_bundle(stems_dir, "publishing", atime=100.0)}
    pin_for_hydrate(stems_dir / "publishing")

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=0))

    assert report.evicted_stable_ids == ()
    assert (stems_dir / "publishing").is_dir()


@pytest.mark.requirement("STEM-42")
def test_a_stale_hydrate_pin_is_removed_and_does_not_block_eviction(tmp_path: Path):
    """[if] a pin outlived its TTL (a crashed hydrate) [then] eviction proceeds and the pin is removed, [else stop].

    Overshoot control: a pin that never expired would make a bundle
    unevictable forever after one crash.
    """
    from apps.cloud.stem_bundles import HYDRATE_PIN_TTL_S, pin_for_hydrate

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"crashed": make_bundle(stems_dir, "crashed", atime=100.0)}
    pin = pin_for_hydrate(stems_dir / "crashed")
    old = pin.stat().st_mtime - HYDRATE_PIN_TTL_S - 1
    os.utime(pin, (old, old))

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ("crashed",)
    assert not pin.exists()


@pytest.mark.requirement("STEM-42")
def test_a_second_hydrate_of_the_same_id_never_drops_the_first_one_s_pin(tmp_path: Path):
    """[if] two processes hydrate one id and the second finds it already local [then] the first one's pin stays, [else stop].

    MUTATION TARGET: share one pin file per id and the second hydrate's
    cleanup unpins the first, leaving its publish window open to eviction.
    """
    from apps.cloud.stem_bundles import is_hydrate_pinned, pin_for_hydrate, unpin_hydrate

    bundle_dir = tmp_path / "stems" / "shared"
    bundle_dir.parent.mkdir(parents=True)
    first = pin_for_hydrate(bundle_dir)
    second = pin_for_hydrate(bundle_dir)
    unpin_hydrate(second)  # the second process gives up: already local

    assert is_hydrate_pinned(bundle_dir)
    unpin_hydrate(first)
    assert not is_hydrate_pinned(bundle_dir), "control: the owner's own unpin releases it"


class _CopySource:
    """A hydration source that copies a prepared bundle into the temp dir."""

    def __init__(self, remote: Path) -> None:
        self._remote = remote

    def fetch_bundle_files(self, *, stable_id: str, file_hashes: object, tmp_dir: Path) -> int:
        total = 0
        for path in sorted((self._remote / stable_id).iterdir()):
            body = path.read_bytes()
            (tmp_dir / path.name).write_bytes(body)
            total += len(body)
        return total


@pytest.mark.requirement("STEM-42")
def test_a_timer_pass_landing_right_after_the_publish_keeps_the_hydrate(tmp_path: Path, monkeypatch):
    """[if] an eviction pass runs the moment the publishing swap lands [then] the hydrate still succeeds, [else stop].

    The fetched copy is verified before it is swapped in (Sol on #6820), so
    the window is between the swap and the hydrate letting go of its pin.

    MUTATION TARGET: pin after the swap (or not at all) in ``hydrate_one``
    and the timer evicts the bundle a valid fetch just published.
    """
    from apps.cloud import stem_hydration
    from apps.cloud.stem_bundles import hydrate_pins

    remote, stems_dir, data_dir = tmp_path / "remote", tmp_path / "stems", tmp_path / "data"
    index = {"fresh": make_bundle(remote, "fresh", atime=100.0)}
    real_replace = stem_hydration.replace_bundle_dir
    timer_reports: list[budget.EnforceReport] = []

    timers: list[threading.Thread] = []

    def replace_then_a_timer_pass(new_dir: Path, dest: Path) -> None:
        # The timer is another process in production: it runs on its own
        # thread, so an eviction claim that must wait for this publish's
        # bundle lock waits instead of deadlocking the test.
        real_replace(new_dir, dest)
        timer = threading.Thread(
            target=lambda: timer_reports.append(enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=0)))
        )
        timers.append(timer)
        timer.start()
        timer.join(2)

    monkeypatch.setattr(stem_hydration, "replace_bundle_dir", replace_then_a_timer_pass)

    outcome = stem_hydration.hydrate_one(
        "fresh", data_dir=data_dir, source=_CopySource(remote), index=index, stems_dir=stems_dir,  # type: ignore[arg-type]
    )

    for timer in timers:
        timer.join(30)
    assert len(timer_reports) == 1, "the timer pass ran inside the publish window"
    assert timer_reports[0].evicted_stable_ids == ()
    assert outcome.status == "hydrated", outcome.reason
    assert (stems_dir / "fresh").is_dir()
    assert hydrate_pins(stems_dir / "fresh") == [], "a drain hydrate leaves it evictable"
    assert "fresh" not in stem_hydration.OPEN_DECKS.open_ids()


@pytest.mark.requirement("STEM-42")
def test_a_deck_load_hydrate_hands_the_bundle_to_the_served_registry(tmp_path: Path):
    """[if] the deck-load path hydrates a bundle [then] it is marked served before the pin is dropped, [else stop].

    MUTATION TARGET: drop the ``hand_off_to_deck`` branch in ``hydrate_one``
    and the bundle is unprotected between this call and the deck's next poll.
    """
    from apps.cloud import stem_hydration
    from apps.cloud.stem_bundles import hydrate_pins

    remote, stems_dir, data_dir = tmp_path / "remote", tmp_path / "stems", tmp_path / "data"
    index = {"for-deck": make_bundle(remote, "for-deck", atime=100.0)}
    registry = OpenDeckRegistry()
    original = stem_hydration.OPEN_DECKS
    stem_hydration.OPEN_DECKS = registry
    try:
        outcome = stem_hydration.hydrate_one(
            "for-deck", data_dir=data_dir, source=_CopySource(remote), index=index,  # type: ignore[arg-type]
            stems_dir=stems_dir, hand_off_to_deck=True,
        )
    finally:
        stem_hydration.OPEN_DECKS = original
    assert outcome.status == "hydrated", outcome.reason
    assert "for-deck" in registry.open_ids()
    assert hydrate_pins(stems_dir / "for-deck") == []


@pytest.mark.requirement("STEM-40")
def test_a_save_in_another_process_waits_for_the_queue_file_lock(tmp_path: Path):
    """[if] another process holds the upload queue's file lock [then] a save waits and merges what that holder wrote, [else stop].

    MUTATION TARGET: drop ``upload_queue_file_lock`` from ``save_upload_queue``
    and the child writes at once, then this process's write erases its entry.
    """
    import subprocess
    import sys
    import time

    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    for stable_id in ("child", "holder"):
        (stems_dir / stable_id).mkdir(parents=True)
    entry: dict[str, object] = {"reason": "not_in_index", "bytes": 1, "queued_at": "t"}
    child_code = (
        "import sys; from pathlib import Path\n"
        "from apps.cloud.stem_bundles import current_fingerprint\n"
        "from apps.cloud.stem_upload_queue import save_upload_queue\n"
        "d, s = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "seen = {'child': current_fingerprint(s / 'child') or 'gone'}\n"
        "save_upload_queue(d, {'child': {'reason': 'not_in_index', 'bytes': 1, 'queued_at': 't'}},"
        " {}, stems_dir=s, seen=seen)\n"
    )
    repo_root = Path(__file__).resolve().parents[2]
    from apps.cloud.stem_upload_queue import upload_queue_file_lock

    with upload_queue_file_lock(data_dir):
        child = subprocess.Popen(  # noqa: S603 - our own interpreter on a fixed script
            [sys.executable, "-c", child_code, str(data_dir), str(stems_dir)], cwd=repo_root
        )
        time.sleep(1.5)
        still_waiting = child.poll() is None
        # This holder is another pass mid-transaction: it writes its own entry.
        write_json_atomically(
            budget.upload_queue_path(data_dir), {"schema_version": 1, "bundles": {"holder": entry}}
        )
    assert child.wait(timeout=60) == 0
    assert still_waiting, "the child saved while another process held the lock"
    assert set(budget.load_upload_queue(data_dir)) == {"child", "holder"}


@pytest.mark.requirement("STEM-42")
def test_a_deck_load_that_finds_the_bundle_already_local_still_takes_the_lease(tmp_path: Path):
    """[if] a deck-load hydrate finds another hydrator already published the bundle [then] it is marked served, [else stop].

    MUTATION TARGET: return ``already_local`` without the handoff and the
    bundle is unprotected once the publishing hydrator drops its pin.
    """
    from apps.cloud import stem_hydration

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"raced": make_bundle(stems_dir, "raced", atime=100.0)}
    registry = OpenDeckRegistry()
    original = stem_hydration.OPEN_DECKS
    stem_hydration.OPEN_DECKS = registry
    try:
        deck = stem_hydration.hydrate_one(
            "raced", data_dir=data_dir, source=_CopySource(tmp_path), index=index,  # type: ignore[arg-type]
            stems_dir=stems_dir, hand_off_to_deck=True,
        )
        assert deck.status == "already_local"
        assert "raced" in registry.open_ids()
        # Control: a bulk caller finding it local takes no lease.
        fresh = OpenDeckRegistry()
        stem_hydration.OPEN_DECKS = fresh
        bulk = stem_hydration.hydrate_one(
            "raced", data_dir=data_dir, source=_CopySource(tmp_path), index=index,  # type: ignore[arg-type]
            stems_dir=stems_dir,
        )
        assert bulk.status == "already_local"
        assert "raced" not in fresh.open_ids()
    finally:
        stem_hydration.OPEN_DECKS = original


@pytest.mark.requirement("STEM-42")
def test_arming_hydration_in_the_engine_marks_it_the_deck_holder(tmp_path: Path, monkeypatch):
    """[if] the engine arms stem hydration [then] its registry is the one eviction trusts, [else stop].

    MUTATION TARGET: drop the ``holds_decks`` line in
    ``_install_armed_stem_hydration`` and the engine's own post-hydrate pass
    never frees space.
    """
    from fastapi import FastAPI

    from apps.cloud import stem_hydration
    from apps.webui.server import app_wiring

    registry = OpenDeckRegistry()
    monkeypatch.setattr(stem_hydration, "OPEN_DECKS", registry)
    assert registry.holds_decks is False, "control: a fresh process (a worker) holds no decks"

    app_wiring._install_armed_stem_hydration(
        FastAPI(), data_dir=tmp_path, source=object(), start_refresh_thread=False
    )

    assert registry.holds_decks is True


def _stripe_is_locked(lock_path: Path) -> bool:
    import fcntl

    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


@pytest.mark.requirement("STEM-42")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="probes the lock with fcntl.flock; Windows takes the msvcrt.locking branch of the same helper",
)
def test_a_hydrate_holds_the_cross_process_lock_for_its_id(tmp_path: Path):
    """[if] a hydrate is fetching and replacing a bundle [then] it holds that id's OS file lock, so another process's hydrate waits, [else stop].

    Codex P1 on 0fbf044dd: with only the in-process lock, the engine and a
    worker could each judge an old bundle invalid and remove the other's
    fresh publish. MUTATION TARGET: drop ``exclusive_file_lock`` from
    ``_hydrate_locks_held`` and the probe finds the stripe free mid-fetch.
    Control: the lock is released when the hydrate returns.
    """
    from apps.cloud.eviction import HydrationError
    from apps.cloud.stem_hydration import hydrate_lock_path, hydrate_one

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    entry = make_bundle(tmp_path / "seed", "contested", atime=100.0)
    lock_path = hydrate_lock_path(data_dir, "contested")
    seen: list[bool] = []

    class _ProbeSource:
        def fetch_bundle_files(self, *, stable_id: str, file_hashes, tmp_dir: Path) -> int:
            seen.append(_stripe_is_locked(lock_path))
            raise HydrationError("probe stops the fetch here")

    outcome = hydrate_one(
        "contested",
        data_dir=data_dir,
        source=_ProbeSource(),  # type: ignore[arg-type]
        index={"contested": entry},
        stems_dir=stems_dir,
    )

    assert outcome.status == "error"
    assert seen == [True], "the fetch ran without the cross-process lock"
    assert not _stripe_is_locked(lock_path), "the lock outlived the hydrate"


@pytest.mark.requirement("STEM-42")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="probes the lock with fcntl.flock; Windows takes the msvcrt.locking branch of the same helper",
)
def test_the_fetch_leaves_the_bundle_writer_lock_free_and_the_publish_takes_it(tmp_path: Path):
    """[if] a hydrate is mid-fetch [then] the bundle writer lock is free, and its publish still waits for it, [else stop].

    Holding the writer lock across the R2 transfer made a stems job's publish
    or a recompress swap of any id on the same stripe wait out the download
    (Codex on #6821). The opposite direction is the control: the publishing
    rename must still wait for a writer that holds the lock, so a thread
    takes it during the fetch, holds it, and checks that no bundle appeared
    under it.

    MUTATION TARGETS: hold the writer lock in ``_hydrate_locks_held`` (the
    probe finds it taken mid-fetch); drop it around the publish (the bundle
    appears while the other writer holds it).
    """
    import threading

    from apps.cloud.stem_hydration import hydrate_lock_path, hydrate_one
    from apps.cloud.stem_upload_queue import exclusive_file_lock
    from apps.stems.bundle_lock import bundle_lock_path

    stems_dir, data_dir, remote = tmp_path / "stems", tmp_path / "data", tmp_path / "remote"
    entry = make_bundle(remote, "striped", atime=100.0)
    writer_lock = bundle_lock_path(data_dir, "striped")
    fetch_lock = hydrate_lock_path(data_dir, "striped")
    assert fetch_lock != writer_lock
    held = threading.Event()
    seen: dict[str, bool] = {}

    def other_writer() -> None:
        with exclusive_file_lock(writer_lock):
            held.set()
            time.sleep(0.3)
            seen["published_under_other_writer"] = (stems_dir / "striped").exists()

    other = threading.Thread(target=other_writer)

    class _ProbeCopySource(_CopySource):
        def fetch_bundle_files(self, *, stable_id: str, file_hashes: object, tmp_dir: Path) -> int:
            seen["fetch_lock_held"] = _stripe_is_locked(fetch_lock)
            seen["writer_lock_held"] = _stripe_is_locked(writer_lock)
            other.start()
            assert held.wait(timeout=10.0)
            return super().fetch_bundle_files(stable_id=stable_id, file_hashes=file_hashes, tmp_dir=tmp_dir)

    try:
        outcome = hydrate_one(
            "striped",
            data_dir=data_dir,
            source=_ProbeCopySource(remote),  # type: ignore[arg-type]
            index={"striped": entry},
            stems_dir=stems_dir,
        )
    finally:
        if other.is_alive() or held.is_set():
            other.join(timeout=10.0)

    assert outcome.status == "hydrated", outcome
    assert seen["fetch_lock_held"] is True, "the fetch ran without the hydrate lock"
    assert seen["writer_lock_held"] is False, "the fetch held the bundle writer lock"
    assert seen["published_under_other_writer"] is False, "the publish did not wait for the writer lock"
    assert (stems_dir / "striped").is_dir()


@pytest.mark.requirement("STEM-42")
def test_the_windows_lock_waits_past_msvcrt_ten_second_limit(tmp_path: Path):
    """[if] another process holds the lock longer than ``LK_LOCK``'s ten retries [then] the waiter keeps waiting rather than raising, [else stop].

    A hydrate holds its lock across a whole download, so a deck load that
    hit ``LK_LOCK``'s OSError would fail instead of waiting. MUTATION TARGET:
    give up after ten attempts and this raises. Control: the region is taken
    on the first free attempt, at byte 0, in the non-blocking mode.
    """
    from apps.cloud.stem_upload_queue import lock_region_until_acquired

    calls: list[tuple[int, int, int]] = []

    def busy_for_25(fd: int, mode: int, nbytes: int) -> None:
        calls.append((handle.tell(), mode, nbytes))
        if len(calls) <= 25:
            raise OSError(errno.EACCES, "locked by another process")

    with (tmp_path / "x.lock").open("a+b") as handle:
        lock_region_until_acquired(handle, busy_for_25, 2, retry_s=0)

    assert len(calls) == 26
    assert set(calls) == {(0, 2, 1)}


@pytest.mark.requirement("STEM-42")
def test_the_windows_lock_reraises_an_error_that_is_not_contention(tmp_path: Path):
    """[if] the lock fails for a reason other than contention [then] it is raised, [else stop].

    A permission or invalid-handle error is not another holder. Retrying it
    would spin until a deadline, then read as a lock held elsewhere. MUTATION
    TARGET: retry every OSError and this loops until the test times out.
    """
    from apps.cloud.stem_upload_queue import lock_region_until_acquired

    calls: list[int] = []

    def permission_denied(fd: int, mode: int, nbytes: int) -> None:
        calls.append(mode)
        raise OSError(errno.EBADF, "invalid handle")

    with (tmp_path / "x.lock").open("a+b") as handle, pytest.raises(OSError, match="invalid handle"):
        lock_region_until_acquired(handle, permission_denied, 2, retry_s=0)

    assert calls == [2]


@pytest.mark.requirement("STEM-58")
def test_a_hydrate_replacing_an_invalid_bundle_waits_for_the_read_barrier(tmp_path: Path):
    """[if] a reader holds the read barrier [then] a hydrate leaves the invalid bundle in place until it lets go, [else stop].

    Removing an invalid local bundle and renaming the fetched one in is a
    bundle replacement like any other: a deck read landing between the two
    would find no bundle, and its restore of a stale swap backup would then
    fail this rename (Codex on #6820).

    MUTATION TARGET: drop the ``readers_held_off`` hold in ``hydrate_one``.
    """
    import threading
    import time

    from apps.cloud import stem_hydration
    from apps.cloud.stem_upload_queue import exclusive_file_lock

    remote, data_dir = tmp_path / "remote", tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    index = {"sid": make_bundle(remote, "sid", atime=100.0)}
    (stems_dir / "sid").mkdir(parents=True)
    (stems_dir / "sid" / "vocals.wav").write_bytes(b"truncated")  # no manifest: invalid
    outcomes: list[stem_hydration.HydrationOutcome] = []

    def hydrate() -> None:
        outcomes.append(
            stem_hydration.hydrate_one(
                "sid", data_dir=data_dir, source=_CopySource(remote), index=index, stems_dir=stems_dir,  # type: ignore[arg-type]
            )
        )

    worker = threading.Thread(target=hydrate)
    with exclusive_file_lock(budget.eviction_claim_lock_path(data_dir, "sid")):
        worker.start()
        time.sleep(1.0)  # an unblocked hydrate of a local copy takes milliseconds
        assert worker.is_alive(), "the hydrate replaced the bundle while a reader held the barrier"
        assert (stems_dir / "sid" / "vocals.wav").read_bytes() == b"truncated"
    worker.join(timeout=60)
    # Control: once released, the fetched bundle lands and verifies.
    assert [outcome.status for outcome in outcomes] == ["hydrated"], outcomes
    assert (stems_dir / "sid" / "manifest.json").is_file()
