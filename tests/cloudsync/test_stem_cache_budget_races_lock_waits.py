"""Stem cache budget under concurrency and in-place re-renders (STEM-40, STEM-42).

Three blocking findings from Codex's review of #4974 (f202046b4), each with
the test that bites on the defect and a control for its overshoot:

* [if] a deck opens a bundle while an eviction pass is hashing it [then] the
  live registry is asked again before removal and the bundle stays.
* [if] two passes write the upload queue at once [then] neither loses its
  temp file to the other.
* [if] a bundle is re-rendered under the same file names [then] the next
  timer pass queues it for upload even on a healthy disk.

And one from Codex's review of #6820 (2dff9aa9b, STEM-58):

* [if] another writer holds the bundle lock [then] eviction waits for it, and
  removes the bundle only if it is still the one it confirmed against R2; a
  bundle a re-compress swap replaced meanwhile is kept. With no lock held an
  evictable bundle is still evicted.
* [if] a landed swap left its backup [then] eviction discards it with the
  bundle, so no later read restores the older render (Codex on #6821).
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_hydration import OPEN_DECK_SERVED_TTL_S, OpenDeckRegistry
from apps.stems.bundle_lock import restore_orphaned_swap
from tests.cloudsync.stem_cache_budget_races_helpers import (
    _big_bundle,
    _enforce_live_in_thread,
    _hold_bundle_lock,
    _hold_claim_lock,
    _stage_a_lookalike,
    _stale_swap_backup,
    _wait_until,
)
from tests.cloudsync.stem_cache_rig import (
    FLOOR_BYTES,
    disk_usage,
    enforce,
    make_bundle,
)
from tests.stems import fifo_part


@pytest.mark.requirement("STEM-42")
def test_a_deck_that_opens_the_bundle_while_eviction_waits_for_the_lock_keeps_it(tmp_path: Path):
    """[if] a deck opens the bundle while eviction waits for the lock and hashes [then] the in-lock recheck keeps it, [else stop].

    The deck opens after the pre-lock recheck (Codex P1 on #6820 and #6821).

    MUTATION TARGET: drop the ``live_protected`` check inside
    ``claim_if_unchanged`` and the bundle a deck just opened is deleted.
    """
    import time

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"opened": make_bundle(stems_dir, "opened", atime=100.0)}
    registry = OpenDeckRegistry()
    with _hold_bundle_lock(data_dir, "opened"):
        thread, reports = _enforce_live_in_thread(stems_dir, data_dir, index, registry)
        time.sleep(1.0)
        assert thread.is_alive() and (stems_dir / "opened").is_dir(), "eviction did not wait for the lock"
        registry.mark_open("opened")
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert reports[0].evicted_stable_ids == () and reports[0].bytes_freed == 0
    assert (stems_dir / "opened").is_dir()


@pytest.mark.requirement("STEM-42")
def test_a_hydrate_pin_taken_while_eviction_waits_for_the_lock_keeps_the_bundle(tmp_path: Path):
    """[if] a hydrate pins the bundle while eviction waits for the lock [then] the in-lock recheck keeps it, [else stop].

    MUTATION TARGET: drop the ``is_hydrate_pinned`` check inside
    ``claim_if_unchanged`` and the pinned bundle is deleted.
    """
    import time

    from apps.cloud.stem_bundles import pin_for_hydrate, unpin_hydrate

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"pinned": make_bundle(stems_dir, "pinned", atime=100.0)}
    registry = OpenDeckRegistry()
    with _hold_bundle_lock(data_dir, "pinned"):
        thread, reports = _enforce_live_in_thread(stems_dir, data_dir, index, registry)
        time.sleep(1.0)
        assert thread.is_alive() and (stems_dir / "pinned").is_dir(), "eviction did not wait for the lock"
        pin = pin_for_hydrate(stems_dir / "pinned")
    thread.join(timeout=60)
    try:
        assert not thread.is_alive()
        assert reports[0].evicted_stable_ids == () and reports[0].bytes_freed == 0
        assert (stems_dir / "pinned").is_dir()
    finally:
        unpin_hydrate(pin)


@pytest.mark.requirement("STEM-42")
def test_a_deck_hold_marks_at_once_and_waits_out_a_claim_already_under_way(tmp_path: Path):
    """[if] a deck asks for a bundle while a claim holds its lock [then] it is marked at once and reads only after the claim, [else stop].

    The claim already asked the registry, so it cannot see the mark; any
    later claim does and keeps the bundle (Codex P1 on #6820).

    MUTATION TARGETS: skip the lock in ``hold_against_eviction`` and the
    deck reads while the claim still runs; take the lock before marking
    and the mark is not visible while the deck waits.
    """
    import time

    data_dir = tmp_path / "data"
    registry = OpenDeckRegistry()
    returned = threading.Event()

    def deck_asks() -> None:
        budget.hold_against_eviction(lambda: registry.mark_served("mid-claim"), data_dir, "mid-claim")
        returned.set()

    with _hold_claim_lock(data_dir, "mid-claim"):  # a claim between its registry ask and its rename
        deck = threading.Thread(target=deck_asks)
        deck.start()
        _wait_until(lambda: "mid-claim" in registry.open_ids(), what="the deck's mark")
        time.sleep(1.0)  # an uncontended lock takes microseconds
        assert not returned.is_set(), "the deck went on to read while the claim still held the lock"
    deck.join(timeout=60)

    assert returned.is_set()


@pytest.mark.requirement("STEM-42")
def test_a_deck_hold_does_not_wait_on_a_hydration_holding_the_bundle_lock(tmp_path: Path):
    """[if] a hydration holds the bundle lock for its R2 fetch when a deck asks [then] the deck's hold returns at once, [else stop].

    A manifest poll answers hydrating at once and a part read keeps its own
    bounded wait; neither may sit behind the network (Codex P1 on #6821).

    MUTATION TARGET: wait on the bundle lock in ``hold_against_eviction`` and
    the hold blocks until the hydration ends.
    """
    data_dir = tmp_path / "data"
    registry = OpenDeckRegistry()
    returned = threading.Event()

    def deck_asks() -> None:
        budget.hold_against_eviction(lambda: registry.mark_served("hydrating"), data_dir, "hydrating")
        returned.set()

    with _hold_bundle_lock(data_dir, "hydrating"):  # hydrate_one around fetch_bundle_files
        deck = threading.Thread(target=deck_asks)
        deck.start()
        assert returned.wait(timeout=10), "the deck's hold waited on the hydration's bundle lock"
    deck.join(timeout=60)
    assert "hydrating" in registry.open_ids()


@pytest.mark.requirement("STEM-42")
def test_the_claim_holds_the_claim_lock_from_its_registry_ask_through_the_rename(tmp_path: Path):
    """[if] a deck asks while eviction is between its last registry ask and its rename [then] the deck reads only after the rename, [else stop].

    The probe is the claim's own ``live_protected`` ask: while it runs, a
    deck's hold must not get past the claim lock, and once the bundle is
    renamed away the hold returns (Codex P1 on #6821).

    MUTATION TARGET: take the claim lock only around the rename (or not at
    all) and the deck gets through while the claim still asks.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"victim": make_bundle(stems_dir, "victim", atime=50.0)}
    registry = OpenDeckRegistry()
    decks: list[tuple[threading.Thread, threading.Event]] = []
    got_through: list[bool] = []

    def live_protected() -> frozenset[str]:
        # Asked by the scan and again by the claim; only the claim's own
        # (last) ask runs under the claim lock.
        returned = threading.Event()

        def deck_asks() -> None:
            budget.hold_against_eviction(lambda: registry.mark_served("other"), data_dir, "victim")
            returned.set()

        deck = threading.Thread(target=deck_asks)
        decks.append((deck, returned))
        deck.start()
        got_through.append(returned.wait(timeout=1.0))
        return frozenset()  # the deck marked another id, so this claim goes ahead

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=0),
        protected=frozenset(),
        live_protected=live_protected,
    )
    for deck, _returned in decks:
        deck.join(timeout=60)

    assert got_through, "the claim never asked the registry: the probe measured nothing"
    assert got_through[-1] is False, (
        f"the deck got past the claim lock while the claim asked the registry: {got_through}"
    )
    assert all(returned.is_set() for _deck, returned in decks)
    assert "victim" in report.evicted_stable_ids and not (stems_dir / "victim").exists()


@pytest.mark.requirement("STEM-42")
def test_a_bundle_a_deck_held_before_the_pass_asked_is_kept(tmp_path: Path):
    """[if] the deck's hold returned before eviction's in-lock ask [then] the bundle is kept, [else stop].

    Control: with no hold the same pass evicts the other bundle."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {
        "held": make_bundle(stems_dir, "held", atime=100.0),
        "idle": make_bundle(stems_dir, "idle", atime=50.0),
    }
    registry = OpenDeckRegistry()
    budget.hold_against_eviction(lambda: registry.mark_served("held"), data_dir, "held")

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=0),
        protected=frozenset(),
        live_protected=registry.open_ids,
    )

    assert "held" not in report.evicted_stable_ids and (stems_dir / "held").is_dir()
    assert "idle" in report.evicted_stable_ids and not (stems_dir / "idle").exists()


@pytest.mark.requirement("STEM-42")
def test_withdrawing_a_failed_read_spares_a_good_read_that_marked_since():
    """[if] another read marks the bundle after a read that then fails [then] the failed read's undo keeps that mark, [else stop].

    MUTATION TARGET: undo without checking the stamp and a deck that loaded
    the bundle meanwhile loses its protection.
    """
    import time

    registry = OpenDeckRegistry()
    withdraw = registry.mark_served_for_read("shared")
    time.sleep(0.01)
    registry.mark_served("shared")
    withdraw()

    assert "shared" in registry.open_ids()


@pytest.mark.requirement("STEM-42")
def test_two_overlapping_failed_reads_leave_no_protection_behind():
    """[if] two reads of one bundle overlap and both fail [then] the bundle is not held against eviction, [else stop].

    MUTATION TARGET: one shared stamp the undo restores, and the second
    failure puts back the first failure's mark for the whole served TTL
    (Codex on #6821).
    """
    registry = OpenDeckRegistry()
    first = registry.mark_served_for_read("broken", now=10.0)
    second = registry.mark_served_for_read("broken", now=11.0)
    first()
    second()

    assert "broken" not in registry.open_ids(now=12.0)


@pytest.mark.requirement("STEM-42")
def test_control_a_good_read_overlapping_a_failed_one_keeps_its_hold():
    """[if] a read fails while an overlapping read of the bundle succeeded [then] the bundle stays held for the TTL, [else stop].

    MUTATION TARGET: an undo that clears every hold on the id, and a deck
    playing the bundle loses its eviction protection to a neighbor's 404.
    """
    registry = OpenDeckRegistry()
    registry.mark_served_for_read("shared", now=10.0)
    failed = registry.mark_served_for_read("shared", now=11.0)
    failed()

    assert "shared" in registry.open_ids(now=12.0)
    assert "shared" not in registry.open_ids(now=10.0 + OPEN_DECK_SERVED_TTL_S + 1)


@pytest.mark.requirement("STEM-42")
def test_expired_read_holds_are_dropped_when_the_open_set_is_read():
    """[if] a read hold expires [then] reading the open set drops it, not only the next read of that id, [else stop].

    Every served read leaves a token; pruned only by a later read of the
    same id, a track read once kept its token for the life of the engine
    (Codex on #6820 and #6821). Control: a hold still inside its TTL stays.

    MUTATION TARGET: only filter expired holds in ``open_ids``.
    """
    registry = OpenDeckRegistry()
    registry.mark_served_for_read("once", now=10.0)
    registry.mark_served_for_read("fresh", now=10.0 + OPEN_DECK_SERVED_TTL_S)

    assert registry.open_ids(now=11.0 + OPEN_DECK_SERVED_TTL_S) == frozenset({"fresh"})
    assert set(registry._read_holds) == {"fresh"}


@pytest.mark.requirement("STEM-58")
def test_eviction_discards_a_landed_swap_s_backup_so_no_read_restores_it(tmp_path: Path):
    """[if] a landed swap left its backup and eviction removes the bundle [then] the backup goes first, [else stop].

    Otherwise the next deck read sees no bundle and a backup, takes it for an
    interrupted swap, and puts the older render back (Codex on #6821).
    Control: a bundle the pass keeps (a deck holds it) keeps its backup too,
    since only the claim discards it.

    MUTATION TARGET: drop ``discard_landed_swap_backup`` from
    ``claim_if_unchanged``.
    """
    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    index = {
        "landed": make_bundle(stems_dir, "landed", atime=100.0),
        "held": make_bundle(stems_dir, "held", atime=200.0),
    }
    evicted_backup = _stale_swap_backup(stems_dir, "landed")
    kept_backup = _stale_swap_backup(stems_dir, "held")

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=0),
        protected=frozenset({"held"}),
    )

    assert report.evicted_stable_ids == ("landed",)
    assert not evicted_backup.exists()
    assert restore_orphaned_swap(stems_dir / "landed", data_dir) is False
    assert not (stems_dir / "landed").exists(), "the eviction stands"
    assert (kept_backup / "manifest.json").read_bytes() == b"older render", (
        "control: a kept bundle's backup is untouched"
    )


@pytest.mark.requirement("STEM-58")
def test_eviction_clears_a_landed_recompression_journal_so_no_read_restores_its_backup(tmp_path: Path):
    """[if] a recompression landed but died before clearing its journal and eviction removes the bundle [then] the entry goes first, [else stop].

    Otherwise the next deck read sees no bundle and a journal entry, takes it
    for an interrupted recompression and puts the old bundle back from its
    backup, undoing both the eviction and the recompression (Codex on
    #6820). The journal is the real one a pass writes. Control: a bundle the
    pass keeps (a deck holds it) keeps its entry, since only the claim
    clears it; and the evicted track's backup, the operator's kept copy,
    stays.

    MUTATION TARGET: drop ``clear_landed_recompress_journal`` from
    ``claim_if_unchanged``.
    """
    from apps.stems.recompress import _write_swap_journal, restore_orphaned_recompress_swap

    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    index = {
        "landed": make_bundle(stems_dir, "landed", atime=100.0),
        "held": make_bundle(stems_dir, "held", atime=200.0),
    }
    journals = {}
    for sid in index:
        backup = tmp_path / "stems-recompress-backup" / "20261017T080300" / sid
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_bytes(b"old flac bundle")
        journals[sid] = (_write_swap_journal(stems_dir / sid, backup), backup)

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=0),
        protected=frozenset({"held"}),
    )

    assert report.evicted_stable_ids == ("landed",)
    journal, backup = journals["landed"]
    assert not journal.exists()
    assert restore_orphaned_recompress_swap(stems_dir / "landed", data_dir) is False
    assert not (stems_dir / "landed").exists(), "the eviction stands"
    assert (backup / "manifest.json").read_bytes() == b"old flac bundle", "the operator's backup is kept"
    assert journals["held"][0].is_file(), "control: a kept bundle's entry is untouched"


@pytest.mark.requirement("STEM-58")
@pytest.mark.skipif(
    os.name == "nt",
    reason="read-only directory modes are POSIX; Windows refuses the move through an open handle instead",
)
@pytest.mark.skipif(
    os.name != "nt" and os.geteuid() == 0,
    reason="root bypasses directory permissions, so a read-only backup cannot refuse its move under root",
)
def test_eviction_keeps_a_bundle_whose_landed_backup_cannot_be_moved_aside(tmp_path: Path):
    """[if] a landed swap's backup can be neither moved to trash nor removed [then] eviction keeps the bundle, [else stop].

    Removing the bundle anyway would leave the old render at the swap path,
    and the next read would restore it as an interrupted swap (Codex on
    #6820). Read-only directories make the move and the delete fail for
    real, as an open handle does on Windows. Control: once the backup can go,
    the next pass evicts the bundle and no read restores anything.

    MUTATION TARGET: ignore ``discard_landed_swap_backup``'s result in
    ``claim_if_unchanged``.
    """
    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    index = {"landed": make_bundle(stems_dir, "landed", atime=100.0)}
    backup = _stale_swap_backup(stems_dir, "landed")
    backup.chmod(0o500)
    backup.parent.chmod(0o500)
    try:
        report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=0))
        assert report.evicted_stable_ids == ()
        assert (stems_dir / "landed").is_dir(), "the bundle stays while its old render cannot be moved aside"
        assert (backup / "manifest.json").read_bytes() == b"older render"
    finally:
        backup.parent.chmod(0o700)
        backup.chmod(0o700)

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=0))
    assert report.evicted_stable_ids == ("landed",), "control: a removable backup lets the eviction through"
    assert not backup.exists()
    assert restore_orphaned_swap(stems_dir / "landed", data_dir) is False


@pytest.mark.requirement("STEM-40")
@pytest.mark.skipif(not fifo_part.HAS_FIFO, reason=fifo_part.SKIP_REASON)
def test_a_claim_hashes_outside_the_bundle_lock_and_keeps_a_bundle_swapped_mid_hash(tmp_path: Path):
    """[if] a writer swaps the bundle while the claim hashes it [then] the writer is not held up and the new bundle is kept, [else stop].

    Codex on #6821: the claim's R2 hash ran under the striped bundle lock,
    so every writer of an id on that stripe waited the whole read out. A
    real writer swaps in a lookalike (same names, sizes and mtimes, one
    byte of ``bass.wav`` different) the moment the claim first lets go of
    the lock, which lands inside the hash of a 96 MiB ``other.wav``, after
    ``bass.wav`` was hashed (_big_bundle). The hash still matches R2 and
    the metadata fingerprint is as it was, so only ``file_marks`` (new
    inodes, new ctimes) tells the bundles apart.

    MUTATION TARGETS: the pre-fix source (hash under the lock: the writer
    runs only after the claim deleted the bundle, and fails); drop the
    ``file_marks`` re-check, or the second lock hold, and the lookalike
    is deleted.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"mid-hash": _big_bundle(stems_dir, "mid-hash", mib=96)}
    staged = _stage_a_lookalike(stems_dir, "mid-hash")
    swapped_in = {p.name: p.read_bytes() for p in staged.iterdir()}
    assert swapped_in != {p.name: p.read_bytes() for p in (stems_dir / "mid-hash").iterdir()}

    def swap() -> None:
        with _hold_bundle_lock(data_dir, "mid-hash"):
            (stems_dir / "mid-hash").rename(stems_dir.parent / "backup")
            staged.rename(stems_dir / "mid-hash")

    from apps.stems.bundle_lock import bundle_lock_path

    with fifo_part.after_the_lock_is_released(bundle_lock_path(data_dir, "mid-hash"), swap) as served:
        report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert not served.errors, served.errors
    assert served.writers_run == 1
    assert report.evicted_stable_ids == () and report.bytes_freed == 0
    assert {p.name: p.read_bytes() for p in (stems_dir / "mid-hash").iterdir()} == swapped_in


@pytest.mark.requirement("STEM-41")
def test_a_big_bundle_nobody_touches_while_it_is_hashed_is_still_evicted(tmp_path: Path):
    """[if] nothing changes the bundle during the unlocked hash [then] the claim still evicts it, [else stop].

    Overshoot control for the test above: a re-check that saw every bundle
    as changed (a ctime the hash itself moved, say) would keep everything.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"untouched": _big_bundle(stems_dir, "untouched", mib=8)}

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ("untouched",)
    assert not (stems_dir / "untouched").exists()


@pytest.mark.requirement("STEM-40")
@pytest.mark.skipif(not fifo_part.HAS_FIFO, reason=fifo_part.SKIP_REASON)
def test_a_bundle_rewritten_in_place_while_the_claim_hashes_it_is_kept(tmp_path: Path):
    """[if] a writer rewrites a part in place (same inode, size and mtime) during the unlocked hash [then] the bundle is kept, [else stop].

    The rewrite lands in ``bass.wav``, which the hash has already read
    (_big_bundle), so the hash still matches R2; only the ctime the write
    moved, which ``os.utime`` cannot put back, tells the claim the bytes
    changed. (The inode half of ``file_marks`` is for a coarse ctime clock,
    1 s on HFS+, which this host does not have.)

    MUTATION TARGET: leave ctime out of ``file_marks`` and the rewritten,
    not-yet-uploaded bundle is deleted.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"in-place": _big_bundle(stems_dir, "in-place", mib=96)}
    vocals = stems_dir / "in-place" / "bass.wav"

    def rewrite() -> None:
        with _hold_bundle_lock(data_dir, "in-place"):
            before = vocals.stat()
            with vocals.open("r+b") as handle:
                first = handle.read(1)
                handle.seek(0)
                handle.write(bytes([first[0] ^ 0xFF]))
            os.utime(vocals, ns=(before.st_atime_ns, before.st_mtime_ns))

    from apps.stems.bundle_lock import bundle_lock_path

    with fifo_part.after_the_lock_is_released(bundle_lock_path(data_dir, "in-place"), rewrite) as served:
        report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))
    rewritten = vocals.read_bytes() if vocals.exists() else b""

    assert not served.errors, served.errors
    assert served.writers_run == 1
    assert report.evicted_stable_ids == () and report.bytes_freed == 0
    assert rewritten[:1] != b"" and hashlib.sha256(rewritten).hexdigest() != index["in-place"]["bass.wav"]
