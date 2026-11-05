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
import json
import os
import threading
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_cache_budget import GIB
from apps.cloud.stem_cache_settings import write_json_atomically
from apps.cloud.stem_hydration import OPEN_DECK_SERVED_TTL_S, OpenDeckRegistry
from apps.stems.bundle_lock import restore_orphaned_swap, swap_backup_path
from tests.cloudsync.stem_cache_rig import (
    FLOOR_BYTES,
    disk_usage,
    enforce,
    make_bundle,
    wav_bytes,
)
from tests.stems import fifo_part


@pytest.mark.requirement("STEM-42")
def test_a_bundle_a_deck_opens_during_the_pass_is_not_evicted(tmp_path: Path):
    """[if] a deck opens a bundle after the pass read ``protected`` [then] the live recheck keeps it, [else stop].

    MUTATION TARGET: drop the ``live_protected`` recheck in ``_evict_lru``
    and ``opened-mid-pass`` is removed.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"opened-mid-pass": make_bundle(stems_dir, "opened-mid-pass", atime=100.0)}
    registry = OpenDeckRegistry()
    snapshot = registry.open_ids()  # read before the scan, as the callers do
    registry.mark_open("opened-mid-pass")  # a deck opens it while the pass hashes

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=0),
        protected=snapshot,
        live_protected=registry.open_ids,
    )

    assert report.evicted_stable_ids == ()
    assert (stems_dir / "opened-mid-pass").is_dir()


@pytest.mark.requirement("STEM-41")
def test_the_live_recheck_still_evicts_a_bundle_nobody_opened(tmp_path: Path):
    """[if] the live registry names nothing [then] eviction proceeds as before, [else stop].

    Overshoot control for the test above: a recheck that refused every
    removal would pass it and leave the volume full.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"idle": make_bundle(stems_dir, "idle", atime=100.0)}
    registry = OpenDeckRegistry()

    report = enforce(
        stems_dir,
        data_dir,
        index=index,
        disk=disk_usage(free=FLOOR_BYTES - 1),
        live_protected=registry.open_ids,
    )

    assert report.evicted_stable_ids == ("idle",)


@pytest.mark.requirement("STEM-40")
def test_concurrent_queue_writes_never_lose_each_other_s_temp_file(tmp_path: Path):
    """[if] two writers save the same JSON file at once [then] both succeed and the file parses, [else stop].

    MUTATION TARGET: go back to one shared ``<name>.tmp`` and writers raise
    FileNotFoundError when one replace consumes the other's temp file.
    """
    target = tmp_path / "queue.json"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(n: int) -> None:
        start.wait()
        try:
            for i in range(200):
                write_json_atomically(target, {"writer": n, "i": i, "pad": "x" * 4096})
        except BaseException as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(json.loads(target.read_text(encoding="utf-8"))) == {"writer", "i", "pad"}
    assert [p.name for p in tmp_path.iterdir()] == ["queue.json"], "no temp file left behind"


@pytest.mark.requirement("STEM-40")
def test_a_same_name_rerender_reaches_the_upload_queue_on_a_healthy_disk(tmp_path: Path):
    """[if] a bundle is re-rendered in place with new bytes [then] the next pass queues it and status stops counting it evictable, [else stop].

    MUTATION TARGET: skip the fingerprint check in ``_refreshed_upload_queue``
    and the re-render never reaches the queue while the disk is healthy.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"rerendered": make_bundle(stems_dir, "rerendered", atime=100.0)}
    healthy = disk_usage(free=200 * GIB)

    first = enforce(stems_dir, data_dir, index=index, disk=healthy)
    assert first.queued_for_upload == ()

    vocals = stems_dir / "rerendered" / "vocals.wav"
    vocals.write_bytes(wav_bytes(seed=7))
    os.utime(vocals, ns=(vocals.stat().st_atime_ns, vocals.stat().st_mtime_ns + 1_000_000_000))

    second = enforce(stems_dir, data_dir, index=index, disk=healthy)
    assert second.queued_for_upload == ("rerendered",)
    assert second.evicted_stable_ids == ()
    state = budget.status(
        stems_dir,
        data_dir=data_dir,
        index=index,
        protected=frozenset(),
        can_rehydrate=True,
        disk=healthy,
    )
    assert state["local_only_stable_ids"] == ["rerendered"]
    assert state["evictable_bundle_count"] == 0


@pytest.mark.requirement("STEM-40")
def test_an_unchanged_bundle_is_hashed_once_not_every_pass(tmp_path: Path, monkeypatch):
    """[if] a verified bundle has not changed [then] later passes do not hash it again, [else stop].

    Overshoot control: re-hashing every bundle on every pass would also find
    the re-render above, at the cost of reading the whole cache each tick.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"steady": make_bundle(stems_dir, "steady", atime=100.0)}
    healthy = disk_usage(free=200 * GIB)
    enforce(stems_dir, data_dir, index=index, disk=healthy)

    hashed: list[str] = []
    real = budget.unconfirmed_reason

    def counting(bundle, idx):
        hashed.append(bundle.stable_id)
        return real(bundle, idx)

    monkeypatch.setattr(budget, "unconfirmed_reason", counting)
    enforce(stems_dir, data_dir, index=index, disk=healthy)
    enforce(stems_dir, data_dir, index=index, disk=healthy)
    assert hashed == []


@pytest.mark.requirement("STEM-40")
def test_the_post_hydrate_pass_hashes_nothing_to_look_for_rerenders(tmp_path: Path, monkeypatch):
    """[if] a pass is bounded by ``max_evict_bytes`` (a deck is waiting) [then] it hashes no unverified bundle for the queue, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"fresh": make_bundle(stems_dir, "fresh", atime=100.0)}
    hashed: list[str] = []
    monkeypatch.setattr(budget, "unconfirmed_reason", lambda bundle, idx: hashed.append(bundle.stable_id))
    enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=200 * GIB), max_evict_bytes=1)
    assert hashed == []


@pytest.mark.requirement("STEM-41")
def test_a_bundle_another_pass_evicted_first_is_skipped_not_an_error(tmp_path: Path, monkeypatch):
    """[if] a concurrent pass removes the bundle this pass just confirmed [then] this pass skips it and frees nothing, without raising, [else stop].

    MUTATION TARGET: go back to a bare ``shutil.rmtree`` and the second
    remover raises FileNotFoundError, which the post-hydrate pass surfaced as
    a failed deck load (Codex on #4974, 3e9a4592b).
    """
    import shutil

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"contested": make_bundle(stems_dir, "contested", atime=100.0)}
    real = budget.unconfirmed_reason

    def confirm_then_lose_the_race(bundle, idx):
        verdict = real(bundle, idx)
        shutil.rmtree(bundle.path, ignore_errors=True)  # the other pass removes it now
        return verdict

    monkeypatch.setattr(budget, "unconfirmed_reason", confirm_then_lose_the_race)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ()
    assert report.bytes_freed == 0


def test_claim_and_remove_lets_exactly_one_remover_win(tmp_path: Path):
    """[if] two passes remove the same bundle [then] one returns True and the other False, and nothing is left behind, [else stop]."""
    from apps.cloud.stem_bundles import claim_and_remove

    stems_dir = tmp_path / "stems"
    make_bundle(stems_dir, "one", atime=100.0)
    assert claim_and_remove(stems_dir / "one") is True
    assert claim_and_remove(stems_dir / "one") is False
    assert list(stems_dir.iterdir()) == []


def _seen(stems_dir: Path, *ids: str) -> dict[str, str]:
    """``seen`` as a pass that scanned these bundles just now passes it."""
    from apps.cloud.stem_bundles import current_fingerprint

    return {i: current_fingerprint(stems_dir / i) or "gone" for i in ids}


@pytest.mark.requirement("STEM-40")
def test_an_older_pass_does_not_erase_a_bundle_a_newer_pass_queued(tmp_path: Path):
    """[if] a pass that scanned before a bundle appeared saves after a pass that queued it [then] the entry stays, [else stop].

    MUTATION TARGET: write this pass's snapshot without merging the on-disk
    file and ``late`` leaves the queue until some later pass re-queues it.
    """
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    (stems_dir / "late").mkdir(parents=True)
    entry: dict[str, object] = {"reason": "not_in_index", "bytes": 1, "queued_at": "t"}
    # The newer pass saw "late" and queued it, with a verified neighbor.
    budget.save_upload_queue(
        data_dir, {"late": entry}, {"other": "fp"}, stems_dir=stems_dir, seen=_seen(stems_dir, "late")
    )
    (stems_dir / "other").mkdir()
    # The older pass scanned before either existed and found nothing to queue.
    written = budget.save_upload_queue(data_dir, {}, {}, stems_dir=stems_dir, seen={})
    assert "late" in written and "late" in budget.load_upload_queue(data_dir)
    assert budget.load_verified_fingerprints(data_dir) == {"other": "fp"}


@pytest.mark.requirement("STEM-40")
def test_the_merge_does_not_keep_what_a_pass_saw_or_what_is_gone(tmp_path: Path):
    """[if] the pass saw the bundle, or its directory is gone [then] the merge drops the entry, [else stop].

    Overshoot control: the merge cannot pin stale rows forever."""
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    (stems_dir / "seen-clean").mkdir(parents=True)
    entry: dict[str, object] = {"reason": "not_in_index", "bytes": 1, "queued_at": "t"}
    budget.save_upload_queue(
        data_dir,
        {"seen-clean": entry, "deleted": entry},
        {},
        stems_dir=stems_dir,
        seen={**_seen(stems_dir, "seen-clean"), "deleted": "gone"},
    )
    written = budget.save_upload_queue(data_dir, {}, {}, stems_dir=stems_dir, seen=_seen(stems_dir, "seen-clean"))
    assert written == {} and budget.load_upload_queue(data_dir) == {}


def _lose_the_claim_for(budget_mod, lost: str, monkeypatch) -> None:
    """Another pass wins the claim on ``lost``: it removes the bundle, and
    this pass's ``claim_and_remove`` reports False."""
    import shutil

    real = budget_mod.claim_and_remove

    def claim(path: Path) -> bool:
        if path.name == lost:
            shutil.rmtree(path)
            return False
        return real(path)

    monkeypatch.setattr(budget_mod, "claim_and_remove", claim)


@pytest.mark.requirement("STEM-41")
def test_a_lost_claim_counts_toward_the_shortfall(tmp_path: Path, monkeypatch):
    """[if] two passes need one bundle and the other wins the claim on it [then] this pass evicts nothing more, [else stop].

    MUTATION TARGET: credit nothing for a lost claim and this pass walks on
    and evicts ``next``, freeing twice what the disk needed.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    _lose_the_claim_for(budget, "lru", monkeypatch)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))
    assert report.evicted_stable_ids == () and report.blocked_reason is None
    assert (stems_dir / "next").is_dir()


@pytest.mark.requirement("STEM-41")
def test_a_lost_claim_smaller_than_the_shortfall_still_evicts_the_rest(tmp_path: Path, monkeypatch):
    """[if] the bundle another pass took is not enough [then] this pass evicts the next one, [else stop].

    Overshoot control: crediting a lost claim must not stop a pass that
    still has a shortfall.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    one_bundle = sum(p.stat().st_size for p in (stems_dir / "lru").rglob("*") if p.is_file())
    _lose_the_claim_for(budget, "lru", monkeypatch)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - one_bundle - 1))
    assert report.evicted_stable_ids == ("next",)


@pytest.mark.requirement("STEM-41")
def test_a_bundle_removed_before_it_was_hashed_counts_toward_the_shortfall(tmp_path: Path, monkeypatch):
    """[if] another pass removes the LRU bundle before this pass hashes it [then] this pass evicts nothing more, [else stop].

    The early window of the lost-claim race. MUTATION TARGET: skip a
    ``REASON_GONE`` bundle without credit and ``next`` is evicted too.
    """
    import shutil

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    real = budget.unconfirmed_reason

    def removed_first(bundle, idx):
        if bundle.stable_id == "lru":
            shutil.rmtree(bundle.path, ignore_errors=True)
        return real(bundle, idx)

    monkeypatch.setattr(budget, "unconfirmed_reason", removed_first)
    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))
    assert report.evicted_stable_ids == () and (stems_dir / "next").is_dir()


def _swap_in_renamed_parts(bundle_dir: Path) -> None:
    """Replace ``bundle_dir`` with a copy whose ``bass.wav`` is named ``bass.mp3``, as a re-render would."""
    import shutil

    staged = bundle_dir.with_name(bundle_dir.name + ".staged")
    shutil.copytree(bundle_dir, staged)
    (staged / "bass.wav").rename(staged / "bass.mp3")
    shutil.rmtree(bundle_dir)
    staged.rename(bundle_dir)


def _swap_on_hash(monkeypatch, stable_id: str, call: int, act) -> None:
    """Run ``act`` on ``stable_id``'s bundle just before its ``call``-th hash (1-based)."""
    real = budget.unconfirmed_reason
    calls: list[str] = []

    def hash_after(bundle, idx):
        if bundle.stable_id == stable_id:
            calls.append(stable_id)
            if len(calls) == call:
                act(bundle.path)
        return real(bundle, idx)

    monkeypatch.setattr(budget, "unconfirmed_reason", hash_after)


@pytest.mark.requirement("STEM-41")
@pytest.mark.parametrize("call", [1, 2], ids=["before-the-scan-hash", "during-the-claim-hash"])
def test_a_bundle_swapped_for_other_file_names_frees_nothing_and_is_kept(tmp_path: Path, monkeypatch, call):
    """[if] the LRU bundle is swapped for one with other file names while hashed [then] it is kept and the next one is evicted, [else stop].

    A missing file reads as REASON_GONE, which used to credit the bundle's
    bytes as freed by another pass, so the pass stopped short of its target
    with the replacement still on disk (Codex on #6820). MUTATION TARGETS:
    credit REASON_GONE in ``_evict_lru`` again (call 1), or return False on
    it in ``claim_if_unchanged`` without looking under the lock (call 2),
    and ``next`` is kept.
    """
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    _swap_on_hash(monkeypatch, "lru", call, _swap_in_renamed_parts)

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ("next",)
    assert sorted(p.name for p in (stems_dir / "lru").iterdir() if p.suffix != ".json") == [
        "bass.mp3",
        "drums.wav",
        "other.wav",
        "vocals.wav",
    ]
    assert not (stems_dir / "next").exists()


@pytest.mark.requirement("STEM-41")
def test_a_bundle_removed_while_the_claim_hashes_it_still_counts_toward_the_shortfall(tmp_path: Path, monkeypatch):
    """[if] another pass removes the LRU bundle during this pass's claim hash [then] this pass evicts nothing more, [else stop].

    Overshoot control for the test above: a claim that kept crediting
    nothing for a missing file would walk on and evict ``next`` too.
    """
    import shutil

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"lru": make_bundle(stems_dir, "lru", atime=100.0)}
    index["next"] = make_bundle(stems_dir, "next", atime=200.0)
    _swap_on_hash(monkeypatch, "lru", 2, shutil.rmtree)

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == () and (stems_dir / "next").is_dir()


@pytest.mark.requirement("STEM-41")
def test_a_bundle_evicted_mid_scan_is_skipped_not_an_error(tmp_path: Path, monkeypatch):
    """[if] another pass removes a bundle between listing a file and stat-ing it [then] the scan skips that bundle and carries on, [else stop].

    MUTATION TARGET: drop the FileNotFoundError guard in ``_scan_one`` and
    the post-hydrate pass raises, failing a deck load that had succeeded.
    """
    import shutil

    from apps.cloud import stem_bundles

    stems_dir = tmp_path / "stems"
    make_bundle(stems_dir, "vanishing", atime=100.0)
    make_bundle(stems_dir, "stays", atime=200.0)
    real_stat = Path.stat

    def stat_after_removal(self: Path, *args, **kwargs):
        if self.parent.name == "vanishing" and (stems_dir / "vanishing").exists():
            shutil.rmtree(stems_dir / "vanishing")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_after_removal)
    assert [b.stable_id for b in stem_bundles.scan_bundles(stems_dir)] == ["stays"]


@pytest.mark.requirement("STEM-41")
def test_a_claim_a_failed_rmtree_left_behind_is_swept_by_the_next_pass(tmp_path: Path):
    """[if] a pass renamed a bundle to ``.evicting-`` but could not delete it [then] the next enforcement pass removes it, [else stop].

    Scans skip that name, so without the sweep its bytes would never be
    counted or freed again. MUTATION TARGET: drop the sweep in ``enforce``.
    """
    from apps.cloud.stem_bundles import EVICTING_MARKER

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    make_bundle(stems_dir, "abandoned", atime=100.0)
    abandoned = stems_dir / f"abandoned{EVICTING_MARKER}deadbeef"
    os.rename(stems_dir / "abandoned", abandoned)
    enforce(stems_dir, data_dir, index={}, disk=disk_usage(free=200 * GIB))
    assert not abandoned.exists()
    # Control: a dry run writes nothing, so it leaves the claim alone.
    make_bundle(stems_dir, "again", atime=100.0)
    os.rename(stems_dir / "again", abandoned)
    enforce(stems_dir, data_dir, index={}, disk=disk_usage(free=200 * GIB), dry_run=True)
    assert abandoned.exists()


@pytest.mark.requirement("STEM-40")
def test_a_pass_that_scanned_an_older_render_keeps_the_newer_verdict(tmp_path: Path):
    """[if] a bundle is re-rendered after a pass scanned it and a newer pass queued the new bytes [then] the older pass's save keeps that entry, [else stop].

    MUTATION TARGET: let every seen id take this pass's verdict again and the
    re-render is reported as R2-backed (Codex, PR #4974).
    """
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    make_bundle(stems_dir, "rerendered", atime=100.0)
    older = _seen(stems_dir, "rerendered")  # pass A scans generation 1
    (stems_dir / "rerendered" / "vocals.wav").write_bytes(b"generation 2, a different length")
    entry: dict[str, object] = {"reason": "content_differs_from_r2_index", "bytes": 1, "queued_at": "t"}
    budget.save_upload_queue(  # pass B judged generation 2
        data_dir, {"rerendered": entry}, {}, stems_dir=stems_dir, seen=_seen(stems_dir, "rerendered")
    )
    # Pass A judged generation 1 clean and saves last.
    written = budget.save_upload_queue(data_dir, {}, {"rerendered": "gen1-fp"}, stems_dir=stems_dir, seen=older)
    assert written == {"rerendered": entry}
    assert budget.load_verified_fingerprints(data_dir) == {}


@pytest.mark.requirement("STEM-40")
def test_a_pass_whose_scan_is_current_still_overrides_the_file(tmp_path: Path):
    """[if] the bundle is unchanged since this pass scanned it [then] this pass's verdict replaces the on-disk one, [else stop].

    Overshoot control: the on-disk entry must not win for every seen id.
    """
    data_dir, stems_dir = tmp_path / "data", tmp_path / "stems"
    make_bundle(stems_dir, "settled", atime=100.0)
    entry: dict[str, object] = {"reason": "not_in_r2_index", "bytes": 1, "queued_at": "t"}
    budget.save_upload_queue(data_dir, {"settled": entry}, {}, stems_dir=stems_dir, seen=_seen(stems_dir, "settled"))
    written = budget.save_upload_queue(
        data_dir, {}, {"settled": "fp"}, stems_dir=stems_dir, seen=_seen(stems_dir, "settled")
    )
    assert written == {}
    assert budget.load_verified_fingerprints(data_dir) == {"settled": "fp"}


def _hold_bundle_lock(data_dir: Path, stable_id: str):
    from apps.cloud.stem_upload_queue import exclusive_file_lock
    from apps.stems.bundle_lock import bundle_lock_path

    return exclusive_file_lock(bundle_lock_path(data_dir, stable_id))


def _hold_claim_lock(data_dir: Path, stable_id: str):
    from apps.cloud.stem_upload_queue import exclusive_file_lock

    return exclusive_file_lock(budget.eviction_claim_lock_path(data_dir, stable_id))


def _enforce_in_thread(stems_dir: Path, data_dir: Path, index: dict) -> tuple[threading.Thread, list]:
    reports: list = []

    def run() -> None:
        reports.append(enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1)))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, reports


def _swap_in_a_new_bundle(stems_dir: Path, stable_id: str) -> dict[str, bytes]:
    """What the STEM-58 swap does under the lock: old dir out, a new one renamed in."""
    import shutil

    staged = stems_dir.parent / "staged"
    make_bundle(staged, stable_id, atime=300.0)
    (staged / stable_id / "vocals.wav").write_bytes(wav_bytes(frames=16, seed=7))
    shutil.move(str(stems_dir / stable_id), str(stems_dir.parent / "backup"))
    (staged / stable_id).rename(stems_dir / stable_id)
    return {p.name: p.read_bytes() for p in (stems_dir / stable_id).iterdir()}


@pytest.mark.requirement("STEM-40")
def test_eviction_waits_for_the_bundle_lock_and_keeps_a_bundle_swapped_meanwhile(tmp_path: Path):
    """[if] the bundle is replaced while eviction waits for its lock [then] the new bundle is kept, [else stop].

    MUTATION TARGET: claim without the lock, or without re-taking the
    fingerprint inside it, and the newly swapped-in bundle is deleted.
    """
    import time

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"swapped": make_bundle(stems_dir, "swapped", atime=100.0)}
    with _hold_bundle_lock(data_dir, "swapped"):
        thread, reports = _enforce_in_thread(stems_dir, data_dir, index)
        time.sleep(1.0)  # an unlocked pass over one small bundle takes milliseconds
        assert thread.is_alive() and (stems_dir / "swapped").is_dir(), "eviction did not wait for the lock"
        swapped_in = _swap_in_a_new_bundle(stems_dir, "swapped")
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert reports[0].evicted_stable_ids == () and reports[0].bytes_freed == 0
    assert {p.name: p.read_bytes() for p in (stems_dir / "swapped").iterdir()} == swapped_in


@pytest.mark.requirement("STEM-41")
def test_eviction_that_waited_for_the_lock_still_evicts_an_unchanged_bundle(tmp_path: Path):
    """[if] the lock holder changed nothing [then] the bundle is evicted once it is released, [else stop].

    Overshoot control: the re-check cannot keep every bundle that was locked.
    """
    import time

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"steady": make_bundle(stems_dir, "steady", atime=100.0)}
    with _hold_bundle_lock(data_dir, "steady"):
        thread, reports = _enforce_in_thread(stems_dir, data_dir, index)
        time.sleep(1.0)
        assert thread.is_alive() and (stems_dir / "steady").is_dir(), "eviction did not wait for the lock"
    thread.join(timeout=60)

    assert reports[0].evicted_stable_ids == ("steady",)
    assert not (stems_dir / "steady").exists()


@pytest.mark.requirement("STEM-41")
def test_an_evictable_bundle_with_no_lock_held_is_evicted(tmp_path: Path):
    """[if] nobody holds the bundle lock [then] the confirmed LRU bundle is evicted at once, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"free": make_bundle(stems_dir, "free", atime=100.0)}

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=FLOOR_BYTES - 1))

    assert report.evicted_stable_ids == ("free",)
    assert not (stems_dir / "free").exists()


def _rewrite_same_size_same_mtime(bundle_dir: Path) -> dict[str, bytes]:
    """A same-codec republish on a coarse clock (1-2 s on HFS+ or FAT): new
    bytes, same size, and an mtime that did not tick, so name, size and mtime
    all match what the scan saw."""
    part = bundle_dir / "vocals.wav"
    before = part.stat()
    data = bytearray(part.read_bytes())
    data[-1] ^= 0xFF
    part.write_bytes(bytes(data))
    os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert part.stat().st_size == before.st_size and part.stat().st_mtime_ns == before.st_mtime_ns
    return {p.name: p.read_bytes() for p in bundle_dir.iterdir()}


@pytest.mark.requirement("STEM-40")
def test_a_same_size_rewrite_the_clock_did_not_see_is_kept(tmp_path: Path):
    """[if] a bundle's bytes change under the lock while names, sizes and mtimes do not [then] the in-lock R2 hash keeps it, [else stop].

    Codex P1 on #6821.

    MUTATION TARGET: claim on the metadata fingerprint alone and this new,
    not-yet-uploaded render is deleted.
    """
    import time

    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"coarse": make_bundle(stems_dir, "coarse", atime=100.0)}
    with _hold_bundle_lock(data_dir, "coarse"):
        thread, reports = _enforce_in_thread(stems_dir, data_dir, index)
        time.sleep(1.0)
        assert thread.is_alive() and (stems_dir / "coarse").is_dir(), "eviction did not wait for the lock"
        rewritten = _rewrite_same_size_same_mtime(stems_dir / "coarse")
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert reports[0].evicted_stable_ids == () and reports[0].bytes_freed == 0
    assert {p.name: p.read_bytes() for p in (stems_dir / "coarse").iterdir()} == rewritten


def _enforce_live_in_thread(
    stems_dir: Path, data_dir: Path, index: dict, registry: OpenDeckRegistry
) -> tuple[threading.Thread, list]:
    reports: list = []

    def run() -> None:
        reports.append(
            enforce(
                stems_dir,
                data_dir,
                index=index,
                disk=disk_usage(free=FLOOR_BYTES - 1),
                protected=registry.open_ids(),
                live_protected=registry.open_ids,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    return thread, reports


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


def _wait_until(predicate, *, what: str, timeout_s: float = 30.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


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


def _stale_swap_backup(stems_dir: Path, stable_id: str) -> Path:
    """What a writer killed after its new bundle landed and before it discarded
    the old one leaves: the live bundle at ``dest`` and an older render at the
    swap backup."""
    backup = swap_backup_path(stems_dir / stable_id)
    backup.mkdir(parents=True)
    (backup / "manifest.json").write_bytes(b"older render")
    return backup


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


def _big_bundle(stems_dir: Path, stable_id: str, *, mib: int) -> dict[str, str]:
    """A bundle whose ``other.wav`` is ``mib`` MiB; returns its R2 index entry.

    The hash reads parts in name order, so ``bass.wav`` (small, first) is
    hashed within microseconds and ``other.wav`` keeps the hash busy long
    after a writer that changes ``bass.wav`` has landed: the hash then
    matches R2 and only the re-check can see the change."""
    entry = make_bundle(stems_dir, stable_id, atime=100.0)
    vocals = stems_dir / stable_id / "other.wav"
    body = os.urandom(mib * 1024 * 1024)
    vocals.write_bytes(body)
    os.utime(vocals, (100.0, 100.0))
    entry["other.wav"] = hashlib.sha256(body).hexdigest()
    return entry


def _stage_a_lookalike(stems_dir: Path, stable_id: str) -> Path:
    """A new render with every name, size and mtime of the live one and one
    byte changed: what a same-codec republish on a coarse clock produces."""
    import shutil

    staged = stems_dir.parent / "staged" / stable_id
    shutil.copytree(stems_dir / stable_id, staged)
    for path in (stems_dir / stable_id).iterdir():
        os.utime(staged / path.name, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
    bass = staged / "bass.wav"
    before = bass.stat()
    with bass.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        last = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([last[0] ^ 0xFF]))
    os.utime(bass, ns=(before.st_atime_ns, before.st_mtime_ns))
    return staged


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
